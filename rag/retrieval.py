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

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Optional

from config import settings
from rag.index import STOPWORDS, DocumentIndex, Hit
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


def auto_retrieve(
    index: Optional[Any],
    text: str,
    *,
    min_terms: Optional[int] = None,
    min_coverage: Optional[float] = None,
    top_k: Optional[int] = None,
    max_chars: Optional[int] = None,
    enabled: Optional[bool] = None,
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
        chunk_count = index.count_chunks() if hasattr(index, "count_chunks") else 0
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

        hits, withheld = index.search(
            query=text,
            top_k=eff_top_k,
            include_flagged=False,
        )

        distinct_terms = set(terms)
        num_distinct = len(distinct_terms)
        min_required_terms = min(eff_min_terms, num_distinct)

        kept_hits: list[Hit] = []
        for hit in hits:
            cid = getattr(hit, "chunk_id", None)
            if hasattr(index, "term_hits") and cid:
                matched_terms = index.term_hits(cid, distinct_terms)
            else:
                matched_terms = set()
            matched_count = len(matched_terms)
            coverage = matched_count / num_distinct if num_distinct > 0 else 0.0

            if matched_count >= min_required_terms and coverage >= eff_min_coverage:
                kept_hits.append(hit)

        if not kept_hits:
            ar = AutoRetrieval(
                searched=True,
                block="",
                injected=0,
                withheld=withheld,
                terms=terms,
                reason="no_hits_passed_thresholds" if hits else "no_hits_found",
            )
            ar.note = not_found_note(text, index, retrieval=ar)
            return ar

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
