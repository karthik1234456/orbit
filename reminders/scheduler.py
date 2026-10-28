"""Reminder scheduler for polling due reminders and reporting missed items."""

from datetime import datetime, timezone
import logging
from typing import Any, Callable, Optional, Sequence

from reminders.store import ReminderRecord, ReminderStore

logger = logging.getLogger(__name__)


def format_missed_message(items: Sequence[Any]) -> str:
    """Format missed reminder items into a single line summary."""
    if not items:
        return ""
    texts = [it.text if hasattr(it, "text") else it["text"] for it in items]
    if len(texts) <= 5:
        joined = ", ".join(texts)
        return f"While ORBIT was closed you missed: {joined}"
    first_5 = ", ".join(texts[:5])
    more_count = len(texts) - 5
    return f"While ORBIT was closed you missed: {first_5} (+{more_count} more)"


class ReminderScheduler:
    """Pure polling scheduler for due and missed reminders. Contains no internal threads."""

    def __init__(
        self,
        store: ReminderStore,
        clock: Optional[Callable[[], datetime]] = None,
        on_due: Optional[Callable[[ReminderRecord], None]] = None,
        on_missed: Optional[Callable[[list[ReminderRecord]], None]] = None,
    ):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_due = on_due
        self.on_missed = on_missed
        self._startup_checked = False

    def _get_now(self) -> datetime:
        now = self.clock()
        if isinstance(now, (int, float)):
            return datetime.fromtimestamp(now, timezone.utc)
        if isinstance(now, datetime):
            if now.tzinfo is None:
                return now.replace(tzinfo=timezone.utc)
            return now.astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def startup(self) -> list[ReminderRecord]:
        """Check for items missed while closed, report them once via on_missed, and return them."""
        if self._startup_checked:
            return []
        self._startup_checked = True

        try:
            missed = self.store.mark_missed_on_startup(self._get_now())
            if missed and self.on_missed:
                try:
                    self.on_missed(missed)
                except Exception as exc:
                    logger.exception("Error in on_missed callback: %s", exc)
            return missed
        except Exception as exc:
            logger.exception("Error checking missed reminders on startup: %s", exc)
            return []

    def tick(self) -> list[ReminderRecord]:
        """Fetch due active items, mark them 'fired', and invoke on_due callback. Never raises."""
        try:
            now = self._get_now()
            due_items = self.store.due(now)
            if not due_items:
                return []

            fired_items: list[ReminderRecord] = []
            for item in due_items:
                # Mark fired before callback so failing callback never repeats the item
                self.store.mark(item.id, "fired")
                fired_item = ReminderRecord(
                    id=item.id,
                    kind=item.kind,
                    text=item.text,
                    due_at=item.due_at,
                    created_at=item.created_at,
                    status="fired",
                    snoozed=item.snoozed,
                )
                fired_items.append(fired_item)

                if self.on_due:
                    try:
                        self.on_due(fired_item)
                    except Exception as exc:
                        logger.exception("Error in on_due callback for reminder %s: %s", item.id, exc)

            return fired_items
        except Exception as exc:
            logger.exception("Error during ReminderScheduler.tick: %s", exc)
            return []
