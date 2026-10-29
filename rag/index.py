"""SQLite-backed full-text search document index using FTS5."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import struct
from typing import Any, Generator, Iterable, Optional, Union

from config.paths import get_rag_db_path


SCHEMA_VERSION = 4

# Conservative patterns detecting obsolete/superseded documents
_OUTDATED_PATTERNS = [
    re.compile(
        r"(?i)\bthis\s+(?:document|policy|version|guideline|procedure|standard)\b[^.\n]*?\b(?:is|has\s+been|was)\s+(?:superseded|replaced|no\s+longer\s+in\s+effect|deprecated|obsolete)\b"
    ),
    re.compile(r"(?i)\bsuperseded\s+by\b"),
    re.compile(
        r"(?im)^\s*(?:>\s*)?(?:#+\s*)?[*_`]*status[*_`]*\s*[:\-][*_`]*\s*[^\n.;]*?\b(?:outdated|superseded|deprecated|archived|obsolete)\b"
    ),
]


def is_document_outdated(text: str) -> bool:
    """Check whether a document text indicates it is obsolete/superseded using conservative patterns."""
    if not text or not isinstance(text, str):
        return False
    for pat in _OUTDATED_PATTERNS:
        if pat.search(text):
            return True
    return False

# Common English stopwords dropped from FTS search queries
STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "aren't", "as", "at", "be", "because", "been",
    "before", "being", "below", "between", "both", "but", "by", "can",
    "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't",
    "doing", "don't", "down", "during", "each", "few", "for", "from",
    "further", "had", "hadn't", "has", "hasn't", "have", "haven't",
    "having", "he", "he'd", "he'll", "he's", "her", "here", "here's",
    "hers", "herself", "him", "himself", "his", "how", "how's", "i",
    "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't",
    "it", "it's", "its", "itself", "let's", "me", "more", "most",
    "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on",
    "once", "only", "or", "other", "ought", "our", "ours", "ourselves",
    "out", "over", "own", "same", "shan't", "she", "she'd", "she'll",
    "she's", "should", "shouldn't", "so", "some", "such", "than", "that",
    "that's", "the", "their", "theirs", "them", "themselves", "then",
    "there", "there's", "these", "they", "they'd", "they'll", "they're",
    "they've", "this", "those", "through", "to", "too", "under", "until",
    "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're",
    "we've", "were", "weren't", "what", "what's", "when", "when's",
    "where", "where's", "which", "while", "who", "who's", "whom",
    "why", "why's", "with", "won't", "would", "wouldn't", "you",
    "you'd", "you'll", "you're", "you've", "your", "yours", "yourself",
    "yourselves",
}


@dataclass
class Hit:
    """Represents a matched chunk from full-text search."""
    path: str
    chunk_no: int
    text: str
    score: float
    flagged: bool
    chunk_id: int = 0
    outdated: bool = False



class IndexUnavailable(Exception):
    """Raised when SQLite FTS5 extension is unavailable."""
    pass


def has_fts5() -> bool:
    """Check if SQLite FTS5 extension with porter tokenizer is available."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _test_fts USING fts5(content, tokenize = 'porter unicode61')")
            return True
        finally:
            conn.close()
    except Exception:
        return False


class DocumentIndex:
    """Persistent SQLite and FTS5-backed index for document chunks."""

    def __init__(self, path: Optional[Union[str, Path]] = None):
        if not has_fts5():
            raise IndexUnavailable(
                "SQLite FTS5 extension is required for DocumentIndex but is not available in this environment."
            )

        self.path = Path(path if path is not None else get_rag_db_path()).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrated: bool = False

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Check if this database already has chunks, documents, or meta tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'")
            has_chunks = cursor.fetchone() is not None
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='documents'")
            has_docs = cursor.fetchone() is not None
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
            has_meta = cursor.fetchone() is not None

            existing_version = 0
            if has_meta:
                cursor.execute("SELECT value FROM meta WHERE key = 'schema_version'")
                row = cursor.fetchone()
                if row:
                    try:
                        existing_version = int(row[0])
                    except (ValueError, TypeError):
                        existing_version = 0

            # If existing database has an older schema version (or unversioned chunks/docs)
            if (has_chunks or has_docs or has_meta) and existing_version < SCHEMA_VERSION:
                cursor.execute("DROP TABLE IF EXISTS chunk_vectors")
                cursor.execute("DROP TABLE IF EXISTS chunks")
                cursor.execute("DROP TABLE IF EXISTS documents")
                self.migrated = True

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    sha256 TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    flagged_count INTEGER NOT NULL,
                    outdated INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cursor.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                    content,
                    doc_id UNINDEXED,
                    chunk_no UNINDEXED,
                    flagged UNINDEXED,
                    tokenize = 'porter unicode61'
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_vectors (
                    chunk_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vec BLOB NOT NULL
                )
                """
            )
            cursor.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager opening a fresh SQLite connection per operation."""
        conn = sqlite3.connect(str(self.path))
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_document(
        self,
        path: str,
        sha256: str,
        chunks: list[tuple[str, bool]],
        outdated: Optional[bool] = None,
        vectors: Optional[list[Optional[list[float]]]] = None,
        embed_model: str = "",
    ) -> str:
        """Add or update a document and its chunks in one transaction.

        Args:
            path: Relative document path.
            sha256: SHA-256 hash of document content.
            chunks: List of (chunk_text, is_flagged) tuples.
            outdated: Whether document is obsolete/superseded (auto-detected if None).

        Returns:
            str: "unchanged", "updated", or "added".
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        flagged_count = sum(1 for _, flagged in chunks if flagged)
        chunk_count = len(chunks)

        if outdated is None:
            full_text = "\n\n".join(t for t, _ in chunks)
            is_outdated = is_document_outdated(full_text)
        else:
            is_outdated = bool(outdated)

        outdated_val = 1 if is_outdated else 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, sha256, outdated FROM documents WHERE path = ?", (path,))
            row = cursor.fetchone()

            if row:
                doc_id, existing_hash, existing_outdated = row
                if existing_hash == sha256 and existing_outdated == outdated_val:
                    # If vectors are provided, check if existing chunks need vector updates
                    if vectors is not None:
                        cursor.execute(
                            "SELECT rowid, chunk_no FROM chunks WHERE doc_id = ? ORDER BY chunk_no ASC",
                            (doc_id,),
                        )
                        chunk_rows = cursor.fetchall()
                        updated_vecs = False
                        for chunk_id, c_no in chunk_rows:
                            if c_no < len(vectors) and vectors[c_no] is not None:
                                vec_floats = vectors[c_no]
                                v_dim = len(vec_floats)
                                blob = struct.pack(f"{v_dim}f", *vec_floats)
                                cursor.execute(
                                    """
                                    INSERT OR REPLACE INTO chunk_vectors (chunk_id, model, dim, vec)
                                    VALUES (?, ?, ?, ?)
                                    """,
                                    (chunk_id, embed_model, v_dim, blob),
                                )
                                updated_vecs = True
                        if updated_vecs:
                            return "updated"
                    return "unchanged"

                # Update existing document: delete old vectors and chunks in same transaction
                cursor.execute(
                    "DELETE FROM chunk_vectors WHERE chunk_id IN (SELECT rowid FROM chunks WHERE doc_id = ?)",
                    (doc_id,),
                )
                cursor.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
                cursor.execute(
                    """
                    UPDATE documents
                    SET sha256 = ?, indexed_at = ?, chunk_count = ?, flagged_count = ?, outdated = ?
                    WHERE id = ?
                    """,
                    (sha256, now_iso, chunk_count, flagged_count, outdated_val, doc_id),
                )
                status = "updated"
            else:
                cursor.execute(
                    """
                    INSERT INTO documents (path, sha256, indexed_at, chunk_count, flagged_count, outdated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (path, sha256, now_iso, chunk_count, flagged_count, outdated_val),
                )
                doc_id = cursor.lastrowid
                status = "added"

            # Insert new chunks and vectors in same transaction
            if chunks:
                for i, (text, flagged) in enumerate(chunks):
                    cursor.execute(
                        "INSERT INTO chunks (content, doc_id, chunk_no, flagged) VALUES (?, ?, ?, ?)",
                        (text, doc_id, i, 1 if flagged else 0),
                    )
                    chunk_id = cursor.lastrowid
                    if vectors is not None and i < len(vectors) and vectors[i] is not None:
                        vec_floats = vectors[i]
                        v_dim = len(vec_floats)
                        blob = struct.pack(f"{v_dim}f", *vec_floats)
                        cursor.execute(
                            """
                            INSERT OR REPLACE INTO chunk_vectors (chunk_id, model, dim, vec)
                            VALUES (?, ?, ?, ?)
                            """,
                            (chunk_id, embed_model, v_dim, blob),
                        )

            return status

    def search(
        self,
        query: str,
        top_k: int = 4,
        include_flagged: bool = False,
    ) -> tuple[list[Hit], int]:
        """Search indexed chunks using FTS5 BM25 ranking.

        Args:
            query: User search query.
            top_k: Maximum number of hits to return.
            include_flagged: Whether to include chunks flagged for safety/injection.

        Returns:
            tuple[list[Hit], int]: (hits, withheld_flagged_count)
        """
        if not query:
            return [], 0

        # Build FTS query safely
        raw_words = re.findall(r"[a-z0-9]+", query.lower())
        usable_words = [w for w in raw_words if len(w) >= 3 and w not in STOPWORDS]
        if not usable_words:
            return [], 0

        fts_query = " OR ".join(f'"{w}"' for w in usable_words)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT c.rowid, d.path, c.chunk_no, c.content, bm25(chunks) AS score, c.flagged, d.outdated
                FROM chunks c
                JOIN documents d ON c.doc_id = d.id
                WHERE chunks MATCH ?
                ORDER BY bm25(chunks) ASC
                """,
                (fts_query,),
            )
            rows = cursor.fetchall()

        hits: list[Hit] = []
        withheld_count = 0

        for chunk_rowid, path, chunk_no, content, score, flagged_val, outdated_val in rows:
            is_flagged = bool(int(flagged_val) if flagged_val is not None else 0)
            is_outdated = bool(int(outdated_val) if outdated_val is not None else 0)

            if is_flagged and not include_flagged:
                withheld_count += 1
                continue

            if len(hits) < top_k:
                hits.append(
                    Hit(
                        path=path,
                        chunk_no=int(chunk_no),
                        text=content,
                        score=float(score),
                        flagged=is_flagged,
                        chunk_id=int(chunk_rowid),
                        outdated=is_outdated,
                    )
                )

        return hits, withheld_count

    def term_hits(self, chunk_id: int, terms: Iterable[str]) -> set[str]:
        """Return the subset of terms that match the specified chunk using FTS5 tokenization.

        Args:
            chunk_id: The SQLite rowid of the chunk in the chunks table.
            terms: Iterable of query terms to check against the chunk.

        Returns:
            set[str]: Terms that match the chunk according to the FTS5 tokenizer.
        """
        try:
            cid = int(chunk_id)
        except (ValueError, TypeError):
            return set()

        if cid <= 0 or not terms:
            return set()

        hits: set[str] = set()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for term in set(terms):
                clean_term = term.strip().replace('"', '""')
                if not clean_term:
                    continue
                match_query = f'"{clean_term}"'
                try:
                    cursor.execute(
                        "SELECT 1 FROM chunks WHERE rowid = ? AND chunks MATCH ? LIMIT 1",
                        (cid, match_query),
                    )
                    if cursor.fetchone() is not None:
                        hits.add(term)
                except sqlite3.OperationalError:
                    continue

        return hits

    def list_documents(self) -> list[tuple[str, int, int, str]]:
        """List all indexed documents as (path, chunk_count, flagged_count, indexed_at)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT path, chunk_count, flagged_count, indexed_at
                FROM documents
                ORDER BY path ASC
                """
            )
            return cursor.fetchall()

    def remove_document(self, path: str) -> bool:
        """Remove a document and all its chunks by path. Returns True if removed, False if not found."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM documents WHERE path = ?", (path,))
            row = cursor.fetchone()
            if not row:
                return False

            doc_id = row[0]
            cursor.execute(
                "DELETE FROM chunk_vectors WHERE chunk_id IN (SELECT rowid FROM chunks WHERE doc_id = ?)",
                (doc_id,),
            )
            cursor.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            return True

    def chunk_count(self) -> int:
        """Return the total number of chunks currently indexed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM chunks")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def count_chunks(self) -> int:
        """Alias for chunk_count() for backward compatibility."""
        return self.chunk_count()

    def term_df(self, terms: Iterable[str]) -> dict[str, int]:
        """Return document frequency (chunk count matching each term) using FTS5 porter tokenizer.

        Args:
            terms: Iterable of query terms.

        Returns:
            dict[str, int]: Mapping of term -> chunk match count.
        """
        if not terms:
            return {}
        result: dict[str, int] = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for term in terms:
                if term in result:
                    continue
                clean_term = str(term).strip().replace('"', '""')
                if not clean_term:
                    result[term] = 0
                    continue
                match_query = f'"{clean_term}"'
                try:
                    cursor.execute(
                        "SELECT count(*) FROM chunks WHERE chunks MATCH ?",
                        (match_query,),
                    )
                    row = cursor.fetchone()
                    result[term] = int(row[0]) if row else 0
                except sqlite3.OperationalError:
                    result[term] = 0
        return result


    def get_vector_stats(self, model: Optional[str] = None, dim: Optional[int] = None) -> dict[str, int]:
        """Return vector inventory statistics: total chunks, total vectors, valid, stale, and missing."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM chunks")
            total_chunks = int(cursor.fetchone()[0])

            cursor.execute("SELECT count(*) FROM chunk_vectors")
            total_vectors = int(cursor.fetchone()[0])

            if model:
                if dim is not None:
                    cursor.execute("SELECT count(*) FROM chunk_vectors WHERE model = ? AND dim = ?", (model, dim))
                else:
                    cursor.execute("SELECT count(*) FROM chunk_vectors WHERE model = ?", (model,))
                valid_vectors = int(cursor.fetchone()[0])
                stale_vectors = max(0, total_vectors - valid_vectors)
            else:
                valid_vectors = total_vectors
                stale_vectors = 0

            missing_vectors = max(0, total_chunks - total_vectors)
            return {
                "total_chunks": total_chunks,
                "total_vectors": total_vectors,
                "valid_vectors": valid_vectors,
                "stale_vectors": stale_vectors,
                "missing_vectors": missing_vectors,
            }

    def get_all_chunk_vectors(self) -> list[dict[str, Any]]:
        """Retrieve all indexed chunks with decoded float vectors if present. Never logs or leaks vectors."""
        results: list[dict[str, Any]] = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT c.rowid, d.path, c.chunk_no, c.content, c.flagged, d.outdated, v.vec, v.dim, v.model
                FROM chunks c
                JOIN documents d ON c.doc_id = d.id
                LEFT JOIN chunk_vectors v ON c.rowid = v.chunk_id
                """
            )
            for rowid, doc_path, chunk_no, content, flagged, outdated, blob, dim, model in cursor.fetchall():
                vec = None
                if blob is not None and dim:
                    try:
                        vec = list(struct.unpack(f"{dim}f", blob))
                    except Exception:
                        vec = None
                results.append({
                    "chunk_id": int(rowid),
                    "path": doc_path,
                    "chunk_no": int(chunk_no),
                    "text": content,
                    "flagged": bool(int(flagged) if flagged is not None else 0),
                    "outdated": bool(int(outdated) if outdated is not None else 0),
                    "vec": vec,
                    "dim": int(dim) if dim else 0,
                    "model": model or "",
                })
        return results
