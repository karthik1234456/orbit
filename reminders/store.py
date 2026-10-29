"""SQLite-backed persistent storage for reminders and timers."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Generator, Optional, Union

from config.paths import get_reminders_db_path

ALLOWED_STATUSES = {"active", "fired", "missed", "dismissed", "cancelled"}
ALLOWED_KINDS = {"reminder", "timer"}
MAX_ACTIVE_ITEMS = 20
MAX_SNOOZES = 3


class ReminderError(Exception):
    """Base exception for reminder store errors."""
    pass


class ReminderLimit(ReminderError):
    """Raised when active reminders or snooze count reaches capacity limit."""
    pass


class ReminderValidationError(ReminderError, ValueError):
    """Raised when reminder text or due time fails validation."""
    pass


def sanitize_reminder_text(text: str) -> str:
    """Sanitize reminder text: collapse whitespace, remove <, >, `, and control characters."""
    if not text:
        return ""
    # Remove control characters (< 32, or 127)
    cleaned = "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 127)
    # Remove angle brackets and backticks
    for ch in ("<", ">", "`"):
        cleaned = cleaned.replace(ch, "")
    # Collapse whitespace
    return " ".join(cleaned.split())


def _to_utc_datetime(dt_or_str: Union[datetime, str]) -> datetime:
    """Normalize a datetime or ISO string to timezone-aware UTC datetime."""
    if isinstance(dt_or_str, str):
        s = dt_or_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    elif isinstance(dt_or_str, datetime):
        dt = dt_or_str
    else:
        raise ReminderValidationError(f"Invalid datetime specification: {dt_or_str}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _to_utc_iso(dt_or_str: Union[datetime, str]) -> str:
    """Format datetime or ISO string as canonical UTC ISO string (YYYY-MM-DDTHH:MM:SSZ)."""
    dt = _to_utc_datetime(dt_or_str)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ReminderRecord:
    """Represents a stored reminder or timer record."""

    id: int
    kind: str
    text: str
    due_at: str
    created_at: str
    status: str
    snoozed: int = 0

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "due_at": self.due_at,
            "created_at": self.created_at,
            "status": self.status,
            "snoozed": self.snoozed,
        }


class ReminderStore:
    """SQLite-backed reminder and timer store with safety validation and auto-recovery."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.path = Path(path if path is not None else get_reminders_db_path()).resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_now(self) -> datetime:
        """Return current time from injectable clock normalized to UTC."""
        now = self.clock()
        if isinstance(now, (int, float)):
            return datetime.fromtimestamp(now, timezone.utc)
        if isinstance(now, datetime):
            if now.tzinfo is None:
                return now.replace(tzinfo=timezone.utc)
            return now.astimezone(timezone.utc)
        raise ReminderValidationError(f"Invalid clock return value: {now}")

    def _init_db(self) -> None:
        """Create schema on fresh connection; safely recreate if corrupt."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        text TEXT NOT NULL,
                        due_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        snoozed INTEGER DEFAULT 0
                    )
                    """
                )
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()

    def _recover_corrupt_db(self) -> None:
        """Safely recreate corrupted database file."""
        try:
            if self.path.exists():
                self.path.unlink(missing_ok=True)
        except Exception:
            pass

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snoozed INTEGER DEFAULT 0
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
        except sqlite3.DatabaseError:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add(
        self,
        kind: str,
        text: str,
        due_at: Union[datetime, str],
    ) -> ReminderRecord:
        """Add a new reminder or timer with strict validation.

        Args:
            kind: "reminder" or "timer"
            text: Reminder message (1-100 characters after sanitizing)
            due_at: Due timestamp (5s to 7 days in future)

        Returns:
            ReminderRecord

        Raises:
            ReminderValidationError: If parameters are invalid
            ReminderLimit: If 20 active items already exist
        """
        k = str(kind).strip().lower()
        if k not in ALLOWED_KINDS:
            raise ReminderValidationError(f"Invalid kind '{kind}', must be 'reminder' or 'timer'")

        clean_text = sanitize_reminder_text(text)
        if not (1 <= len(clean_text) <= 100):
            raise ReminderValidationError(
                f"Reminder text must be 1 to 100 characters (got {len(clean_text)} after sanitizing)"
            )

        now = self._get_now()
        try:
            due_dt = _to_utc_datetime(due_at)
        except Exception as exc:
            raise ReminderValidationError(f"Invalid due_at timestamp: {exc}") from exc

        min_due = now + timedelta(seconds=5)
        max_due = now + timedelta(days=7)

        if due_dt < min_due:
            raise ReminderValidationError(
                f"Due time must be at least 5 seconds ahead (due: {due_dt.isoformat()}, now: {now.isoformat()})"
            )
        if due_dt > max_due:
            raise ReminderValidationError(
                f"Due time must be at most 7 days ahead (due: {due_dt.isoformat()}, max: {max_due.isoformat()})"
            )

        due_iso = _to_utc_iso(due_dt)
        now_iso = _to_utc_iso(now)

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM reminders WHERE status = 'active'"
                )
                active_count = cursor.fetchone()[0]
                if active_count >= MAX_ACTIVE_ITEMS:
                    raise ReminderLimit(
                        f"Reminder limit reached (maximum {MAX_ACTIVE_ITEMS} active items)"
                    )

                cursor.execute(
                    """
                    INSERT INTO reminders (kind, text, due_at, created_at, status, snoozed)
                    VALUES (?, ?, ?, ?, 'active', 0)
                    """,
                    (k, clean_text, due_iso, now_iso),
                )
                new_id = cursor.lastrowid
                return ReminderRecord(
                    id=new_id,
                    kind=k,
                    text=clean_text,
                    due_at=due_iso,
                    created_at=now_iso,
                    status="active",
                    snoozed=0,
                )
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return self.add(kind, text, due_at)

    def list_active(self) -> list[ReminderRecord]:
        """Return all active reminders ordered by due_at ascending."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, kind, text, due_at, created_at, status, snoozed
                    FROM reminders
                    WHERE status = 'active'
                    ORDER BY due_at ASC, id ASC
                    """
                )
                rows = cursor.fetchall()
                return [
                    ReminderRecord(
                        id=r[0],
                        kind=r[1],
                        text=r[2],
                        due_at=r[3],
                        created_at=r[4],
                        status=r[5],
                        snoozed=r[6],
                    )
                    for r in rows
                ]
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return []

    def get(self, id: int) -> Optional[ReminderRecord]:
        """Retrieve a single reminder record by ID."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, kind, text, due_at, created_at, status, snoozed
                    FROM reminders
                    WHERE id = ?
                    """,
                    (id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return ReminderRecord(
                    id=row[0],
                    kind=row[1],
                    text=row[2],
                    due_at=row[3],
                    created_at=row[4],
                    status=row[5],
                    snoozed=row[6],
                )
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return None

    def due(self, now: Optional[Union[datetime, str]] = None) -> list[ReminderRecord]:
        """Return active items whose due_at <= now, ordered by due_at ascending."""
        target_now = _to_utc_datetime(now) if now is not None else self._get_now()
        target_iso = _to_utc_iso(target_now)

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, kind, text, due_at, created_at, status, snoozed
                    FROM reminders
                    WHERE status = 'active' AND due_at <= ?
                    ORDER BY due_at ASC, id ASC
                    """,
                    (target_iso,),
                )
                rows = cursor.fetchall()
                return [
                    ReminderRecord(
                        id=r[0],
                        kind=r[1],
                        text=r[2],
                        due_at=r[3],
                        created_at=r[4],
                        status=r[5],
                        snoozed=r[6],
                    )
                    for r in rows
                ]
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return []

    def mark(self, id: int, status: str) -> bool:
        """Update the status of a reminder.

        Args:
            id: Reminder ID
            status: One of ('active', 'fired', 'missed', 'dismissed', 'cancelled')

        Returns:
            bool: True if an existing reminder was updated
        """
        st = str(status).strip().lower()
        if st not in ALLOWED_STATUSES:
            raise ReminderValidationError(f"Invalid status '{status}', must be one of {sorted(ALLOWED_STATUSES)}")

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE reminders SET status = ? WHERE id = ?",
                    (st, id),
                )
                return cursor.rowcount > 0
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return False

    def snooze(self, id: int, minutes: int = 5) -> Optional[ReminderRecord]:
        """Snooze an active reminder by N minutes (max 3 snoozes).

        Reschedules due_at, status remains 'active', snoozed counter increments.

        Returns:
            Optional[ReminderRecord]: Updated record if snoozed successfully, or None

        Raises:
            ReminderLimit: If reminder has already reached the maximum of 3 snoozes
        """
        if minutes <= 0:
            raise ReminderValidationError("Snooze duration must be positive")

        rec = self.get(id)
        if rec is None or rec.status != "active":
            return None

        if rec.snoozed >= MAX_SNOOZES:
            raise ReminderLimit(f"Cannot snooze reminder {id}: reached maximum of {MAX_SNOOZES} snoozes")

        now = self._get_now()
        new_due = now + timedelta(minutes=minutes)
        new_due_iso = _to_utc_iso(new_due)

        updated = False
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE reminders
                    SET due_at = ?, snoozed = snoozed + 1
                    WHERE id = ? AND status = 'active'
                    """,
                    (new_due_iso, id),
                )
                updated = cursor.rowcount > 0
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return None

        if updated:
            return self.get(id)
        return None

    def cancel(self, id: int) -> bool:
        """Cancel an active reminder by marking its status as 'cancelled'."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'active'",
                    (id,),
                )
                return cursor.rowcount > 0
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return False

    def cancel_all(self, kind: Optional[str] = None) -> int:
        """Cancel all active reminders (or all active of given kind). Returns count cancelled."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if kind:
                    k = str(kind).strip().lower()
                    cursor.execute(
                        "UPDATE reminders SET status = 'cancelled' WHERE status = 'active' AND kind = ?",
                        (k,),
                    )
                else:
                    cursor.execute(
                        "UPDATE reminders SET status = 'cancelled' WHERE status = 'active'"
                    )
                return cursor.rowcount
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return 0

    def mark_missed_on_startup(
        self, now: Optional[Union[datetime, str]] = None
    ) -> list[ReminderRecord]:
        """Find items due while the app was closed, transition them to 'missed', and return once."""
        target_now = _to_utc_datetime(now) if now is not None else self._get_now()
        target_iso = _to_utc_iso(target_now)

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, kind, text, due_at, created_at, status, snoozed
                    FROM reminders
                    WHERE status = 'active' AND due_at <= ?
                    ORDER BY due_at ASC, id ASC
                    """,
                    (target_iso,),
                )
                rows = cursor.fetchall()
                if not rows:
                    return []

                missed_records = [
                    ReminderRecord(
                        id=r[0],
                        kind=r[1],
                        text=r[2],
                        due_at=r[3],
                        created_at=r[4],
                        status="missed",
                        snoozed=r[6],
                    )
                    for r in rows
                ]

                ids = [r.id for r in missed_records]
                placeholders = ",".join("?" for _ in ids)
                cursor.execute(
                    f"UPDATE reminders SET status = 'missed' WHERE id IN ({placeholders})",
                    tuple(ids),
                )
                return missed_records
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return []

    def count_by_status(self) -> dict[str, int]:
        """Return counts of reminders by status."""
        counts = {s: 0 for s in ALLOWED_STATUSES}
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status, COUNT(*) FROM reminders GROUP BY status"
                )
                for status_val, cnt in cursor.fetchall():
                    if status_val in counts:
                        counts[status_val] = cnt
                    else:
                        counts[status_val] = cnt
                return counts
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return {s: 0 for s in ALLOWED_STATUSES}
