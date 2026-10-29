"""SQLite-backed persistent storage for named routines.

Guarantees:
- One fresh connection per operation with parameterized SQL only.
- Automatic recovery on database corruption.
- Corrupt/unreadable rows are skipped and logged, never crashing the application.
- Enforces routine limits (<= 20) and strict validation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Generator, Optional, Union

from config.paths import get_routines_db_path

from routines.validate import (
    RoutineError,
    RoutineExistsError,
    RoutineLimit,
    RoutineValidationError,
    normalize_routine_name,
    validate_routine_name,
    validate_routine_steps,
)

RoutineStoreError = RoutineExistsError

__all__ = [
    "MAX_ROUTINES",
    "ROUTINES_SCHEMA_VERSION",
    "Routine",
    "RoutineError",
    "RoutineExistsError",
    "RoutineLimit",
    "RoutineStore",
    "RoutineStoreError",
    "RoutineValidationError",
]

logger = logging.getLogger(__name__)

MAX_ROUTINES: int = 20
ROUTINES_SCHEMA_VERSION: int = 1


@dataclass
class Routine:
    """Represents a named, stored routine."""

    name: str
    display_name: str
    steps: list[dict[str, Any]]
    schema_version: int = ROUTINES_SCHEMA_VERSION
    created_at: str = ""
    last_run_at: Optional[str] = None
    run_count: int = 0

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "steps": self.steps,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "run_count": self.run_count,
        }


class RoutineStore:
    """SQLite-backed store managing persistence and retrieval of named routines."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self.path = Path(path if path is not None else get_routines_db_path()).resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _now_iso(self) -> str:
        """Return current time from clock as ISO string."""
        if hasattr(self.clock, "now_iso"):
            return str(self.clock.now_iso())
        if callable(self.clock):
            val = self.clock()
            if isinstance(val, (int, float)):
                return datetime.fromtimestamp(val, timezone.utc).isoformat()
            if isinstance(val, datetime):
                if val.tzinfo is None:
                    return val.replace(tzinfo=timezone.utc).isoformat()
                return val.astimezone(timezone.utc).isoformat()
            return str(val)
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager opening a fresh SQLite connection per operation."""
        conn = sqlite3.connect(str(self.path))
        try:
            yield conn
            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initialize table schema; safely recreate if file is corrupt."""
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS routines (
                        name TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        steps_json TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        last_run_at TEXT,
                        run_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Routine database corrupt, recreating: {exc}")
            self._recover_corrupt_db()

    def _recover_corrupt_db(self) -> None:
        """Safely recreate corrupted database file."""
        try:
            if self.path.exists():
                self.path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to unlink corrupted database: {exc}")

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS routines (
                    name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT,
                    run_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def _parse_row(self, row: tuple) -> Optional[Routine]:
        """Safely parse a database row into a Routine, skipping unreadable rows."""
        try:
            name, display_name, steps_json, schema_version, created_at, last_run_at, run_count = row
            steps = json.loads(steps_json)
            if not isinstance(steps, list):
                logger.warning(f"Skipping corrupt routine row '{name}': steps_json is not a list")
                return None
            return Routine(
                name=str(name),
                display_name=str(display_name),
                steps=steps,
                schema_version=int(schema_version),
                created_at=str(created_at),
                last_run_at=str(last_run_at) if last_run_at is not None else None,
                run_count=int(run_count),
            )
        except Exception as exc:
            logger.warning(f"Skipping unreadable routine row: {exc}")
            return None

    def save(
        self,
        display_name: str,
        steps: list[dict[str, Any]],
        overwrite: bool = False,
    ) -> Routine:
        """Save a new or updated routine.

        Raises:
        - RoutineValidationError if name or steps fail validation.
        - RoutineExistsError if routine exists and overwrite is False.
        - RoutineLimit if max routine limit (20) would be exceeded.
        """
        norm_name = validate_routine_name(display_name)
        validated_steps = validate_routine_steps(steps)
        now_iso = self._now_iso()
        steps_json = json.dumps(validated_steps)

        def _do_save(conn: sqlite3.Connection) -> Routine:
            cur = conn.cursor()
            cur.execute("SELECT created_at, last_run_at, run_count FROM routines WHERE name = ?", (norm_name,))
            existing = cur.fetchone()

            if existing:
                if not overwrite:
                    raise RoutineExistsError(f"Routine '{display_name}' already exists.")
                created_at = existing[0]
                last_run_at = existing[1]
                run_count = existing[2]
                cur.execute(
                    """
                    UPDATE routines
                    SET display_name = ?, steps_json = ?, schema_version = ?
                    WHERE name = ?
                    """,
                    (display_name.strip(), steps_json, ROUTINES_SCHEMA_VERSION, norm_name),
                )
                return Routine(
                    name=norm_name,
                    display_name=display_name.strip(),
                    steps=validated_steps,
                    schema_version=ROUTINES_SCHEMA_VERSION,
                    created_at=created_at,
                    last_run_at=last_run_at,
                    run_count=run_count,
                )
            else:
                cur.execute("SELECT COUNT(*) FROM routines")
                count_row = cur.fetchone()
                current_count = count_row[0] if count_row else 0
                if current_count >= MAX_ROUTINES:
                    raise RoutineLimit(
                        f"Maximum routine limit reached ({MAX_ROUTINES} routines)."
                    )

                cur.execute(
                    """
                    INSERT INTO routines (name, display_name, steps_json, schema_version, created_at, last_run_at, run_count)
                    VALUES (?, ?, ?, ?, ?, NULL, 0)
                    """,
                    (norm_name, display_name.strip(), steps_json, ROUTINES_SCHEMA_VERSION, now_iso),
                )
                return Routine(
                    name=norm_name,
                    display_name=display_name.strip(),
                    steps=validated_steps,
                    schema_version=ROUTINES_SCHEMA_VERSION,
                    created_at=now_iso,
                    last_run_at=None,
                    run_count=0,
                )

        try:
            with self._get_connection() as conn:
                return _do_save(conn)
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Database error on save, attempting recovery: {exc}")
            self._recover_corrupt_db()
            with self._get_connection() as conn:
                return _do_save(conn)

    def get(self, name: str) -> Optional[Routine]:
        """Retrieve a routine by name or None if not found or unreadable."""
        norm_name = normalize_routine_name(name)
        if not norm_name:
            return None

        def _do_get(conn: sqlite3.Connection) -> Optional[Routine]:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT name, display_name, steps_json, schema_version, created_at, last_run_at, run_count
                FROM routines
                WHERE name = ?
                """,
                (norm_name,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._parse_row(row)

        try:
            with self._get_connection() as conn:
                return _do_get(conn)
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Database error on get, attempting recovery: {exc}")
            self._recover_corrupt_db()
            return None

    def list(self) -> list[Routine]:
        """Return all saved routines, skipping unreadable rows."""
        def _do_list(conn: sqlite3.Connection) -> list[Routine]:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT name, display_name, steps_json, schema_version, created_at, last_run_at, run_count
                FROM routines
                ORDER BY name ASC
                """
            )
            rows = cur.fetchall()
            routines: list[Routine] = []
            for r in rows:
                parsed = self._parse_row(r)
                if parsed is not None:
                    routines.append(parsed)
            return routines

        try:
            with self._get_connection() as conn:
                return _do_list(conn)
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Database error on list, attempting recovery: {exc}")
            self._recover_corrupt_db()
            return []

    def delete(self, name: str) -> bool:
        """Delete a routine by name. Returns True if deleted, False if not found."""
        norm_name = normalize_routine_name(name)
        if not norm_name:
            return False

        def _do_delete(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            cur.execute("DELETE FROM routines WHERE name = ?", (norm_name,))
            return cur.rowcount > 0

        try:
            with self._get_connection() as conn:
                return _do_delete(conn)
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Database error on delete, attempting recovery: {exc}")
            self._recover_corrupt_db()
            return False

    def mark_run(self, name: str) -> bool:
        """Update last_run_at and increment run_count for a routine."""
        norm_name = normalize_routine_name(name)
        if not norm_name:
            return False

        now_iso = self._now_iso()

        def _do_mark(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE routines
                SET last_run_at = ?, run_count = run_count + 1
                WHERE name = ?
                """,
                (now_iso, norm_name),
            )
            return cur.rowcount > 0

        try:
            with self._get_connection() as conn:
                return _do_mark(conn)
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Database error on mark_run, attempting recovery: {exc}")
            self._recover_corrupt_db()
            return False
