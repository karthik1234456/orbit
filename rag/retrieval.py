"""Automatic document retrieval for user messages in ORBIT.

Heuristics and Architecture Rationale:
1. Why coverage is used instead of a BM25 cutoff:
   In SQLite FTS5, BM25 scores are uncalibrated and corpus-dependent. Scores vary
   wildly depending on total corpus size, document length variations, and term
   frequencies across the index. Setting a static numeric BM25 cutoff (e.g. score < -5.0)
   is notoriously brittle: it can reject relevant chunks in small document sets while
   admitting completely irrelevant long chunks in large document sets.
   In contrast, query term coverage (the fraction of distinct meaningful query terms
   appearing as whole words in the candidate passage) provides a corpus-invariant,
   scale-independent heuristic. Requiring both a minimum number of distinct terms
   (RAG_AUTO_MIN_TERMS) and a minimum matched ratio (RAG_AUTO_MIN_COVERAGE) ensures
   that a passage is only injected if it actually touches the core subject matter of the
   user's question.

2. Why the threshold values are heuristics:
   The defaults (RAG_AUTO_MIN_TERMS = 2, RAG_AUTO_MIN_COVERAGE = 0.5) are empirical
   heuristics designed to prevent unwanted context injection during general conversation
   or small talk (e.g. 'hello there' or 'explain recursion in Python') while reliably
   activating when a user asks a multi-term factual question matching indexed docs.
   For queries with fewer distinct terms than RAG_AUTO_MIN_TERMS (e.g. single-term queries),
   the heuristic adapts to require 100% of the query terms.
"""

from dataclasses import dataclass, field
import math
import re
from typing import Any, Optional

from config import settings
from rag.index import STOPWORDS, Hit
from rag.safety import build_docs_block

# Cues indicating that a user query is scoped to personal/team documents or plans
PERSONAL_SCOPE_CUES: frozenset[str] = frozenset({
    "our",
    "my",
    "we",
    "us",
    "team",
    "company",
    "project",
    "policy",
    "policies",
    "notes",
    "document",
    "documents",
    "file",
    "plan",
    "plans",
    "roadmap",
    "colleague",
    "manager",
})

_NOT_FOUND_NOTE = (
    "ORBIT searched the user's indexed documents for this message and found no matching passages; "
    "if the question is about the user's own documents or team, say the documents do not contain the answer instead of guessing."
)


def query_terms(text: str) -> list[str]:
    """Extract lowercase alphanumeric words of 3+ characters minus STOPWORDS.

    Reuses the exact STOPWORDS definition from DocumentIndex to ensure
    consistent tokenization between query building and coverage checking.
    """
    if not text or not isinstance(text, str):
        return []
    raw_words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in raw_words if len(w) >= 3 and w not in STOPWORDS]


class AutoHit(dict):
    """Candidate hit evaluated during auto-retrieval."""

    def __init__(
        self,
        file: str = "",
        chunk: int = 0,
        matched_terms: Optional[list[str]] = None,
        matched_fraction: float = 0.0,
        kept: bool = False,
        outdated: bool = False,
        **kwargs: Any,
    ):
        terms_list = matched_terms if matched_terms is not None else []
        super().__init__(
            file=file,
            chunk=chunk,
            matched_terms=terms_list,
            matched_fraction=matched_fraction,
            kept=kept,
            outdated=outdated,
            **kwargs,
        )

    @property
    def file(self) -> str:
        return self["file"]

    @property
    def chunk(self) -> int:
        return self["chunk"]

    @property
    def matched_terms(self) -> list[str]:
        return self["matched_terms"]

    @property
    def matched_fraction(self) -> float:
        return self["matched_fraction"]

    @property
    def kept(self) -> bool:
        return self["kept"]

    @property
    def outdated(self) -> bool:
        return self.get("outdated", False)



@dataclass
class AutoRetrieval:
    """Outcome of automatic document retrieval for a user message."""

    searched: bool
    block: str
    injected: int
    withheld: int
    terms: list[str]
    reason: str
    note: str = ""
    hits: list[AutoHit] = field(default_factory=list)


def auto_retrieve(
    index: Optional[Any],
    text: str,
    *,
    min_terms: Optional[int] = None,
    min_coverage: Optional[float] = None,
    top_k: Optional[int] = None,
    max_chars: Optional[int] = None,
    enabled: Optional[bool] = None,
    scoring: Optional[str] = None,
    embedder: Optional[Any] = None,
    embed_threshold: Optional[float] = None,
    embed_margin: Optional[float] = None,
) -> AutoRetrieval:
    """Automatically search indexed documents and construct a bounded context block.

    Args:
        index: DocumentIndex instance, or None if RAG is unavailable.
        text: User query string.
        min_terms: Minimum distinct query terms required in a chunk.
        min_coverage: Minimum fraction of query terms required in a chunk.
        top_k: Maximum candidate hits to evaluate from FTS search.
        max_chars: Maximum overall character budget for the resulting docs block.
        enabled: Toggle override (defaults to settings.RAG_AUTO).
        scoring: Scoring method in {"fraction", "rarest3", "idf"} (defaults to settings.RAG_AUTO_SCORING).

    Returns:
        AutoRetrieval: Object containing search status, formatted context block,
                       counts of injected and withheld chunks, query terms, reason, and note.
                       This function NEVER raises; errors return searched=False.
    """
    try:
        eff_enabled = enabled if enabled is not None else getattr(settings, "RAG_AUTO", True)
        terms = query_terms(text)

        if not eff_enabled:
            return AutoRetrieval(
                searched=False,
                block="",
                injected=0,
                withheld=0,
                terms=terms,
                reason="disabled",
            )

        if index is None:
            return AutoRetrieval(
                searched=False,
                block="",
                injected=0,
                withheld=0,
                terms=terms,
                reason="index_none",
            )

        # Check if index is empty
        chunk_count = index.count_chunks() if hasattr(index, "count_chunks") else (
            index.chunk_count() if hasattr(index, "chunk_count") else 0
        )
        if chunk_count <= 0:
            return AutoRetrieval(
                searched=False,
                block="",
                injected=0,
                withheld=0,
                terms=terms,
                reason="index_empty",
            )

        if not terms:
            ar = AutoRetrieval(
                searched=True,
                block="",
                injected=0,
                withheld=0,
                terms=[],
                reason="no_query_terms",
            )
            ar.note = not_found_note(text, index, retrieval=ar)
            return ar

        eff_min_terms = (
            min_terms
            if min_terms is not None
            else getattr(settings, "RAG_AUTO_MIN_TERMS", 2)
        )
        eff_min_coverage = (
            min_coverage
            if min_coverage is not None
            else getattr(settings, "RAG_AUTO_MIN_COVERAGE", 0.5)
        )
        eff_top_k = (
            top_k
            if top_k is not None
            else getattr(settings, "RAG_AUTO_TOP_K", 3)
        )
        eff_max_chars = (
            max_chars
            if max_chars is not None
            else getattr(settings, "RAG_AUTO_MAX_CHARS", 1500)
        )
        eff_scoring = (
            scoring
            if scoring is not None
            else getattr(settings, "RAG_AUTO_SCORING", "fraction")
        )
        if eff_scoring not in ("fraction", "embed", "hybrid", "rarest3", "idf"):
            eff_scoring = "fraction"

        # --- Embedding and Hybrid Scoring Path ---
        if eff_scoring in ("embed", "hybrid"):
            import rag.embeddings as emb_mod
            from rag.embeddings import EmbeddingError, dot_product, get_cached_query_embedding

            embeddings_enabled = getattr(settings, "RAG_EMBEDDINGS", False) or (embedder is not None)
            if not embeddings_enabled:
                emb_mod.LAST_EMBED_FALLBACK_REASON = "disabled"
                fallback_ar = auto_retrieve(
                    index, text,
                    min_terms=min_terms,
                    min_coverage=min_coverage,
                    top_k=top_k,
                    max_chars=max_chars,
                    enabled=enabled,
                    scoring="fraction",
                )
                fallback_ar.reason = "embedding_unavailable:disabled"
                return fallback_ar

            eff_embed = embedder
            if eff_embed is None:
                if getattr(settings, "ORBIT_MOCK", False):
                    eff_embed = emb_mod.FakeEmbedder()
                else:
                    eff_embed = emb_mod.OllamaEmbedder(
                        model=getattr(settings, "RAG_EMBED_MODEL", "embeddinggemma"),
                        timeout=5.0,
                        batch_size=getattr(settings, "RAG_EMBED_BATCH", 16),
                    )

            if not hasattr(index, "get_all_chunk_vectors"):
                emb_mod.LAST_EMBED_FALLBACK_REASON = "no_vector_support"
                fallback_ar = auto_retrieve(index, text, scoring="fraction", enabled=enabled)
                fallback_ar.reason = "embedding_unavailable:no_vector_support"
                return fallback_ar

            all_chunks = index.get_all_chunk_vectors()
            valid_chunks_with_vec = [c for c in all_chunks if c.get("vec") is not None]
            if not valid_chunks_with_vec:
                emb_mod.LAST_EMBED_FALLBACK_REASON = "empty_vectors"
                fallback_ar = auto_retrieve(index, text, scoring="fraction", enabled=enabled)
                fallback_ar.reason = "embedding_unavailable:empty_vectors"
                return fallback_ar

            try:
                q_vec = get_cached_query_embedding(eff_embed, text, timeout=5.0)
            except EmbeddingError as e_err:
                err_msg = str(e_err).lower()
                if "not found in ollama" in err_msg or "pull" in err_msg:
                    short_reason = "model_not_found"
                elif "timed out" in err_msg:
                    short_reason = "timeout"
                elif "cannot connect" in err_msg:
                    short_reason = "connection_failed"
                else:
                    short_reason = "embedding_error"
                emb_mod.LAST_EMBED_FALLBACK_REASON = short_reason
                fallback_ar = auto_retrieve(index, text, scoring="fraction", enabled=enabled)
                fallback_ar.reason = f"embedding_unavailable:{short_reason}"
                return fallback_ar
            except Exception as exc:
                short_reason = f"error_{type(exc).__name__}"
                emb_mod.LAST_EMBED_FALLBACK_REASON = short_reason
                fallback_ar = auto_retrieve(index, text, scoring="fraction", enabled=enabled)
                fallback_ar.reason = f"embedding_unavailable:{short_reason}"
                return fallback_ar

            q_dim = len(q_vec)
            if any(c.get("dim", len(c["vec"])) != q_dim for c in valid_chunks_with_vec):
                emb_mod.LAST_EMBED_FALLBACK_REASON = "dimension_mismatch"
                fallback_ar = auto_retrieve(index, text, scoring="fraction", enabled=enabled)
                fallback_ar.reason = "embedding_unavailable:dimension_mismatch"
                return fallback_ar

            eff_thresh = (
                embed_threshold
                if embed_threshold is not None
                else getattr(settings, "RAG_EMBED_THRESHOLD", 0.65)
            )
            eff_marg = (
                embed_margin
                if embed_margin is not None
                else getattr(settings, "RAG_EMBED_MARGIN", 0.08)
            )

            distinct_terms = set(terms)
            num_distinct = len(distinct_terms)
            min_req_terms = min(eff_min_terms, num_distinct)

            candidates: list[dict[str, Any]] = []
            withheld_embed_count = 0

            for c in all_chunks:
                vec = c.get("vec")
                cosine = dot_product(q_vec, vec) if vec is not None else 0.0
                cid = c["chunk_id"]
                matched_terms = index.term_hits(cid, distinct_terms) if hasattr(index, "term_hits") else set()
                matched_count = len(matched_terms)
                coverage = matched_count / num_distinct if num_distinct > 0 else 0.0
                frac_passes = bool(matched_count >= min_req_terms and coverage >= eff_min_coverage)
                is_flagged = c.get("flagged", False)

                if is_flagged:
                    if eff_scoring == "embed":
                        if cosine >= eff_thresh:
                            withheld_embed_count += 1
                    elif eff_scoring == "hybrid":
                        if frac_passes or (cosine >= eff_thresh and matched_count >= 1):
                            withheld_embed_count += 1
                    continue

                candidates.append({
                    "chunk": c,
                    "cosine": cosine,
                    "matched_terms": matched_terms,
                    "coverage": coverage,
                    "fraction_passes": frac_passes,
                })

            kept_hits = []
            recorded_hits = []

            if eff_scoring == "embed":
                eligible = [sc for sc in candidates if sc["cosine"] >= eff_thresh]
                if eligible:
                    best_cos = max(sc["cosine"] for sc in eligible)
                    cutoff = best_cos - eff_marg
                    kept_scs = [sc for sc in eligible if sc["cosine"] >= cutoff]
                    kept_scs.sort(key=lambda sc: sc["cosine"], reverse=True)
                    top_kept = kept_scs[:eff_top_k]
                    top_kept_ids = set(id(sc) for sc in top_kept)
                else:
                    top_kept_ids = set()

                for sc in candidates:
                    is_kept = id(sc) in top_kept_ids
                    c = sc["chunk"]
                    hit_obj = Hit(
                        path=c["path"],
                        chunk_no=c["chunk_no"],
                        text=c["text"],
                        score=sc["cosine"],
                        flagged=False,
                        chunk_id=c["chunk_id"],
                        outdated=c.get("outdated", False),
                    )
                    if is_kept:
                        kept_hits.append(hit_obj)
                    recorded_hits.append(
                        AutoHit(
                            file=c["path"],
                            chunk=c["chunk_no"],
                            matched_terms=sorted(sc["matched_terms"]),
                            matched_fraction=sc["cosine"],
                            kept=is_kept,
                            outdated=c.get("outdated", False),
                        )
                    )

            elif eff_scoring == "hybrid":
                embed_cands = [
                    sc for sc in candidates
                    if sc["cosine"] >= eff_thresh and len(sc["matched_terms"]) >= 1
                ]
                if embed_cands:
                    best_embed_cos = max(sc["cosine"] for sc in embed_cands)
                    cutoff = best_embed_cos - eff_marg
                    filtered_embed = [sc for sc in embed_cands if sc["cosine"] >= cutoff]
                else:
                    filtered_embed = []

                filtered_embed_ids = set(id(sc) for sc in filtered_embed)

                qualifying = [
                    sc for sc in candidates
                    if sc["fraction_passes"] or (id(sc) in filtered_embed_ids)
                ]
                qualifying.sort(key=lambda sc: sc["cosine"], reverse=True)
                top_kept = qualifying[:eff_top_k]
                top_kept_ids = set(id(sc) for sc in top_kept)

                for sc in candidates:
                    is_kept = id(sc) in top_kept_ids
                    c = sc["chunk"]
                    hit_obj = Hit(
                        path=c["path"],
                        chunk_no=c["chunk_no"],
                        text=c["text"],
                        score=sc["cosine"],
                        flagged=False,
                        chunk_id=c["chunk_id"],
                        outdated=c.get("outdated", False),
                    )
                    if is_kept:
                        kept_hits.append(hit_obj)
                    recorded_hits.append(
                        AutoHit(
                            file=c["path"],
                            chunk=c["chunk_no"],
                            matched_terms=sorted(sc["matched_terms"]),
                            matched_fraction=sc["cosine"] if id(sc) in filtered_embed_ids else sc["coverage"],
                            kept=is_kept,
                            outdated=c.get("outdated", False),
                        )
                    )

            if not kept_hits:
                ar = AutoRetrieval(
                    searched=True,
                    block="",
                    injected=0,
                    withheld=withheld_embed_count,
                    terms=terms,
                    reason="no_hits_passed_thresholds" if candidates else "no_hits_found",
                    hits=recorded_hits,
                )
                ar.note = not_found_note(text, index, retrieval=ar)
                return ar

            kept_hits = sorted(
                kept_hits,
                key=lambda h: 1 if getattr(h, "outdated", False) else 0,
            )

            header_line = "ORBIT searched the user's documents automatically for this message."
            reserved_chars = len(header_line) + 1
            inner_budget = max(100, eff_max_chars - reserved_chars)

            raw_block = build_docs_block(
                kept_hits,
                withheld_count=withheld_embed_count,
                max_chars=inner_budget,
            )

            if not raw_block:
                ar = AutoRetrieval(
                    searched=True,
                    block="",
                    injected=0,
                    withheld=withheld_embed_count,
                    terms=terms,
                    reason="block_empty",
                    hits=recorded_hits,
                )
                ar.note = not_found_note(text, index, retrieval=ar)
                return ar

            block = f"{header_line}\n{raw_block}"
            if len(block) > eff_max_chars:
                block = block[:eff_max_chars]

            return AutoRetrieval(
                searched=True,
                block=block,
                injected=len(kept_hits),
                withheld=withheld_embed_count,
                terms=terms,
                reason="retrieved",
                hits=recorded_hits,
            )

        # --- Standard Lexical Search Path ---
        hits, withheld = index.search(
            query=text,
            top_k=eff_top_k,
            include_flagged=False,
        )

        distinct_terms = set(terms)
        num_distinct = len(distinct_terms)

        # Pre-compute scoring data for rarest3 and idf
        df_map: dict[str, int] = {}
        active_terms: list[str] = []
        rarest3_set: set[str] = set()
        rarest3_count: int = 0
        idf_weights: dict[str, float] = {}
        idf_total_w: float = 0.0

        if eff_scoring in ("rarest3", "idf"):
            if hasattr(index, "term_df"):
                df_map = index.term_df(distinct_terms)
            active_terms = [t for t in distinct_terms if df_map.get(t, 0) >= 1]
            min_required_terms = min(eff_min_terms, len(active_terms))

            if eff_scoring == "rarest3":
                if active_terms:
                    sorted_active = sorted(active_terms, key=lambda t: (df_map.get(t, 0), t))
                    rarest3_terms = sorted_active[:3]
                    rarest3_set = set(rarest3_terms)
                    rarest3_count = len(rarest3_terms)
            elif eff_scoring == "idf":
                N = (
                    index.chunk_count()
                    if hasattr(index, "chunk_count")
                    else (index.count_chunks() if hasattr(index, "count_chunks") else 0)
                )
                if active_terms and N > 0:
                    idf_weights = {t: math.log(1.0 + N / df_map[t]) for t in active_terms}
                    idf_total_w = sum(idf_weights.values())
        else:
            # "fraction" scoring
            min_required_terms = min(eff_min_terms, num_distinct)

        recorded_hits: list[AutoHit] = []
        kept_hits: list[Hit] = []
        for hit in hits:
            cid = getattr(hit, "chunk_id", None)
            if hasattr(index, "term_hits") and cid:
                matched_terms = index.term_hits(cid, distinct_terms)
            else:
                matched_terms = set()
            matched_count = len(matched_terms)

            if eff_scoring == "rarest3":
                if rarest3_count > 0:
                    matched_rarest_count = len(matched_terms & rarest3_set)
                    coverage = matched_rarest_count / rarest3_count
                else:
                    coverage = 0.0
                is_kept = bool(
                    len(active_terms) > 0
                    and matched_count >= min_required_terms
                    and coverage >= eff_min_coverage
                )
            elif eff_scoring == "idf":
                if idf_total_w > 0.0:
                    matched_w = sum(idf_weights[t] for t in matched_terms if t in idf_weights)
                    coverage = matched_w / idf_total_w
                else:
                    coverage = 0.0
                is_kept = bool(
                    len(active_terms) > 0
                    and matched_count >= min_required_terms
                    and coverage >= eff_min_coverage
                )
            else:
                # Default "fraction" scoring
                coverage = matched_count / num_distinct if num_distinct > 0 else 0.0
                is_kept = bool(matched_count >= min_required_terms and coverage >= eff_min_coverage)

            if is_kept:
                kept_hits.append(hit)

            file_path = getattr(hit, "path", "")
            chunk_no = getattr(hit, "chunk_no", 0)
            is_outdated = getattr(hit, "outdated", False)
            recorded_hits.append(
                AutoHit(
                    file=file_path,
                    chunk=chunk_no,
                    matched_terms=sorted(matched_terms),
                    matched_fraction=coverage,
                    kept=is_kept,
                    outdated=is_outdated,
                )
            )

        if not kept_hits:
            ar = AutoRetrieval(
                searched=True,
                block="",
                injected=0,
                withheld=withheld,
                terms=terms,
                reason="no_hits_passed_thresholds" if hits else "no_hits_found",
                hits=recorded_hits,
            )
            ar.note = not_found_note(text, index, retrieval=ar)
            return ar

        # Sort kept hits so non-outdated passages come first
        kept_hits = sorted(
            kept_hits,
            key=lambda h: 1 if getattr(h, "outdated", False) else 0,
        )


        header_line = "ORBIT searched the user's documents automatically for this message."
        # Reserve room for the header line and newline separator
        reserved_chars = len(header_line) + 1
        inner_budget = max(100, eff_max_chars - reserved_chars)

        raw_block = build_docs_block(
            kept_hits,
            withheld_count=withheld,
            max_chars=inner_budget,
        )

        if not raw_block:
            ar = AutoRetrieval(
                searched=True,
                block="",
                injected=0,
                withheld=withheld,
                terms=terms,
                reason="block_empty",
                hits=recorded_hits,
            )
            ar.note = not_found_note(text, index, retrieval=ar)
            return ar

        block = f"{header_line}\n{raw_block}"
        if len(block) > eff_max_chars:
            block = block[:eff_max_chars]

        return AutoRetrieval(
            searched=True,
            block=block,
            injected=len(kept_hits),
            withheld=withheld,
            terms=terms,
            reason="retrieved",
            hits=recorded_hits,
        )

    except Exception as exc:
        return AutoRetrieval(
            searched=False,
            block="",
            injected=0,
            withheld=0,
            terms=terms if "terms" in locals() else [],
            reason=f"error: {exc}",
        )


def not_found_note(
    text: str,
    index: Optional[Any],
    retrieval: Optional[AutoRetrieval] = None,
) -> str:
    """Generate a single-sentence note when indexed documents do not contain the answer.

    Used ONLY when the index is non-empty, no hits were kept, and the message contains
    a personal-scope cue (e.g. 'our', 'my', 'team', 'project', etc.).

    Args:
        text: User query string.
        index: DocumentIndex instance.
        retrieval: Optional precomputed AutoRetrieval result to avoid redundant searches.

    Returns:
        str: Guidance note if personal-scope cues match and no hits exist; "" otherwise.
    """
    try:
        if index is None:
            return ""

        chunk_count = index.count_chunks() if hasattr(index, "count_chunks") else 0
        if chunk_count <= 0:
            return ""

        if retrieval is None:
            retrieval = auto_retrieve(index, text)

        if retrieval.injected > 0 or bool(retrieval.block):
            return ""

        msg_words = set(re.findall(r"[a-z0-9]+", text.lower()))
        if not (PERSONAL_SCOPE_CUES & msg_words):
            return ""

        return _NOT_FOUND_NOTE

    except Exception:
        return ""
