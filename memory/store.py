"""SQLite-backed persistent memory store for ORBIT assistant."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Generator, List, Tuple, Union

from memory.safety import validate_memory

# Stopwords dropped from search queries
STOPWORDS = {
    "the", "and", "what", "which", "you", "are", "for", "with",
    "that", "this", "have", "about", "remember", "tell", "please",
    "can", "could", "would", "how", "who", "when", "where", "why",
    "not", "but", "all", "any", "from", "was", "were", "been",
    "has", "had", "will", "our", "your", "they", "them", "his", "her",
}


@dataclass
class Memory:
    """Represents a stored memory record."""
    id: int
    content: str
    category: str
    created_at: str
    last_used_at: str
    use_count: int


class MemoryFull(Exception):
    """Raised when memory store reaches its maximum allowed capacity."""
    pass


def _escape_like(text: str) -> str:
    r"""Escape LIKE wildcards (%, _) and the escape character itself (\)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MemoryStore:
    """Persistent SQLite-backed memory store with validation, deduplication, and word search."""

    def __init__(self, path: Union[str, Path] = "data/memory.db", max_items: int = 500):
        self.path = Path(path).resolve()
        self.max_items = max_items

        # Ensure parent folder exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize table schema on a fresh connection
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    content TEXT NOT NULL,
                    normalized TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    use_count INTEGER DEFAULT 0
                )
                """
            )

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager opening a fresh SQLite connection per operation.

        Ensures no connection is shared across threads or operations.
        """
        conn = sqlite3.connect(str(self.path))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, content: str, category: str = "fact") -> Tuple[Memory, bool]:
        """Validate, deduplicate, and store a memory record.

        Args:
            content: The text content of the memory.
            category: Semantic category ('preference', 'fact', 'project', 'other').

        Returns:
            tuple[Memory, bool]: (Memory object, True if newly created or False if already existed).

        Raises:
            MemoryRejected: If content fails safety heuristics or length limits.
            MemoryFull: If the store has reached max_items and the item does not already exist.
        """
        # Validate and normalize content and category first
        clean_content, clean_category = validate_memory(content, category)
        normalized = clean_content.lower()

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Check if normalized content already exists
            cursor.execute(
                "SELECT id, content, category, created_at, last_used_at, use_count FROM memories WHERE normalized = ?",
                (normalized,),
            )
            existing_row = cursor.fetchone()
            if existing_row:
                return (
                    Memory(
                        id=existing_row[0],
                        content=existing_row[1],
                        category=existing_row[2],
                        created_at=existing_row[3],
                        last_used_at=existing_row[4],
                        use_count=existing_row[5],
                    ),
                    False,
                )

            # Check capacity
            cursor.execute("SELECT COUNT(*) FROM memories")
            current_count = cursor.fetchone()[0]
            if current_count >= self.max_items:
                raise MemoryFull(
                    f"Memory store capacity limit reached ({self.max_items} items)."
                )

            now = datetime.now(timezone.utc).isoformat()
            try:
                cursor.execute(
                    """
                    INSERT INTO memories (content, normalized, category, created_at, last_used_at, use_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (clean_content, normalized, clean_category, now, now, 0),
                )
                new_id = cursor.lastrowid
                return (
                    Memory(
                        id=new_id,
                        content=clean_content,
                        category=clean_category,
                        created_at=now,
                        last_used_at=now,
                        use_count=0,
                    ),
                    True,
                )
            except sqlite3.IntegrityError:
                # Handle concurrent duplicate insertion gracefully
                cursor.execute(
                    "SELECT id, content, category, created_at, last_used_at, use_count FROM memories WHERE normalized = ?",
                    (normalized,),
                )
                row = cursor.fetchone()
                return (
                    Memory(
                        id=row[0],
                        content=row[1],
                        category=row[2],
                        created_at=row[3],
                        last_used_at=row[4],
                        use_count=row[5],
                    ),
                    False,
                )

    def search(self, query: str, limit: int = 5, touch: bool = True) -> List[Memory]:
        """Search memories matching query words, ranked by match count and usage.

        Args:
            query: User search string.
            limit: Maximum number of memories to return.
            touch: When True, increments use_count and updates last_used_at for matched rows.

        Returns:
            list[Memory]: Matched memories ordered by match count, use_count, and last_used_at.
        """
        if not query or not query.strip():
            return []

        q = query.lower()
        # Extract alphanumeric words and % / _ tokens of 3+ characters
        tokens = [t for t in re.findall(r"[a-zA-Z0-9%_]+", q) if len(t) >= 3]

        usable_words: list[str] = []
        for word in tokens:
            if word not in STOPWORDS and word not in usable_words:
                usable_words.append(word)

        if not usable_words:
            return []

        # Escape wildcard characters for LIKE
        patterns = [f"%{_escape_like(w)}%" for w in usable_words]

        # Construct query with ? parameter placeholders strictly
        match_exprs = ["(content LIKE ? ESCAPE '\\')"] * len(usable_words)
        where_clause = " OR ".join(match_exprs)
        score_clause = " + ".join(match_exprs)

        sql = (
            f"SELECT id, content, category, created_at, last_used_at, use_count, "
            f"({score_clause}) AS match_count "
            f"FROM memories WHERE {where_clause} "
            f"ORDER BY match_count DESC, use_count DESC, last_used_at DESC "
            f"LIMIT ?"
        )
        params = tuple(patterns + patterns + [limit])

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()

            if not rows:
                return []

            results = [
                Memory(
                    id=r[0],
                    content=r[1],
                    category=r[2],
                    created_at=r[3],
                    last_used_at=r[4],
                    use_count=r[5],
                )
                for r in rows
            ]

            if touch:
                now = datetime.now(timezone.utc).isoformat()
                for mem in results:
                    cursor.execute(
                        "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                        (now, mem.id),
                    )
                    mem.last_used_at = now
                    mem.use_count += 1

            return results

    def list_all(self, limit: int = 50) -> List[Memory]:
        """Return all memories ordered newest first up to limit."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, content, category, created_at, last_used_at, use_count
                FROM memories
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                Memory(
                    id=r[0],
                    content=r[1],
                    category=r[2],
                    created_at=r[3],
                    last_used_at=r[4],
                    use_count=r[5],
                )
                for r in rows
            ]

    def delete(self, memory_id: int) -> bool:
        """Delete a memory record by its primary key ID.

        Returns:
            bool: True if an existing memory was deleted, False if not found.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0

    def count(self) -> int:
        """Return total number of memories stored."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memories")
            return int(cursor.fetchone()[0])

    def clear(self) -> int:
        """Delete all memory records. Returns the number of items removed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories")
            return cursor.rowcount
