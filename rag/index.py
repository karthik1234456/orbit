"""SQLite-backed full-text search document index using FTS5."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Generator, Union

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


class IndexUnavailable(Exception):
    """Raised when SQLite FTS5 extension is unavailable."""
    pass


def has_fts5() -> bool:
    """Check if SQLite FTS5 extension is available."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _test_fts USING fts5(content)")
            return True
        finally:
            conn.close()
    except Exception:
        return False


class DocumentIndex:
    """Persistent SQLite and FTS5-backed index for document chunks."""

    def __init__(self, path: Union[str, Path] = "data/rag.db"):
        if not has_fts5():
            raise IndexUnavailable(
                "SQLite FTS5 extension is required for DocumentIndex but is not available in this environment."
            )

        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    sha256 TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    flagged_count INTEGER NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                    content,
                    doc_id UNINDEXED,
                    chunk_no UNINDEXED,
                    flagged UNINDEXED
                )
                """
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
    ) -> str:
        """Add or update a document and its chunks in one transaction.

        Args:
            path: Relative document path.
            sha256: SHA-256 hash of document content.
            chunks: List of (chunk_text, is_flagged) tuples.

        Returns:
            str: "unchanged", "updated", or "added".
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        flagged_count = sum(1 for _, flagged in chunks if flagged)
        chunk_count = len(chunks)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, sha256 FROM documents WHERE path = ?", (path,))
            row = cursor.fetchone()

            if row:
                doc_id, existing_hash = row
                if existing_hash == sha256:
                    return "unchanged"

                # Update existing document: delete old chunks first
                cursor.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
                cursor.execute(
                    """
                    UPDATE documents
                    SET sha256 = ?, indexed_at = ?, chunk_count = ?, flagged_count = ?
                    WHERE id = ?
                    """,
                    (sha256, now_iso, chunk_count, flagged_count, doc_id),
                )
                status = "updated"
            else:
                cursor.execute(
                    """
                    INSERT INTO documents (path, sha256, indexed_at, chunk_count, flagged_count)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (path, sha256, now_iso, chunk_count, flagged_count),
                )
                doc_id = cursor.lastrowid
                status = "added"

            # Insert new chunks
            if chunks:
                chunk_rows = [
                    (text, doc_id, i, 1 if flagged else 0)
                    for i, (text, flagged) in enumerate(chunks)
                ]
                cursor.executemany(
                    "INSERT INTO chunks (content, doc_id, chunk_no, flagged) VALUES (?, ?, ?, ?)",
                    chunk_rows,
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
                SELECT d.path, c.chunk_no, c.content, bm25(chunks) AS score, c.flagged
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

        for path, chunk_no, content, score, flagged_val in rows:
            is_flagged = bool(int(flagged_val) if flagged_val is not None else 0)

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
                    )
                )

        return hits, withheld_count

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
            cursor.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            return True

    def count_chunks(self) -> int:
        """Return the total number of chunks currently indexed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM chunks")
            row = cursor.fetchone()
            return int(row[0]) if row else 0
