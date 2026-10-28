"""Tool declarations for reminders and timers."""

from datetime import datetime, timedelta
import math
from typing import Any, Callable, List, Optional

from reminders.store import (
    ReminderLimit,
    ReminderStore,
    ReminderValidationError,
    sanitize_reminder_text,
)
from reminders.timeparse import parse_clock
from security.permissions import PermissionLevel
from tools.registry import Tool


def _format_delay(seconds: float) -> str:
    """Format delay in seconds as human readable string (e.g. '10 minutes', '1 hour', '45 seconds')."""
    total_sec = max(0, int(round(seconds)))
    if total_sec < 60:
        return f"{total_sec} second{'s' if total_sec != 1 else ''}"
    mins = int(round(total_sec / 60))
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''}"
    hrs = int(round(mins / 60))
    return f"{hrs} hour{'s' if hrs != 1 else ''}"


def _get_local_now(clock: Optional[Callable[[], datetime]]) -> datetime:
    """Return current local datetime using injectable clock."""
    if clock:
        now = clock()
        if now.tzinfo is None:
            return now.astimezone()
        return now
    return datetime.now().astimezone()


def make_reminder_tools(
    store: ReminderStore,
    clock: Optional[Callable[[], datetime]] = None,
) -> List[Tool]:
    """Create and return Tool objects for reminder and timer management."""

    def _now() -> datetime:
        return _get_local_now(clock)

    def set_reminder(
        text: str,
        minutes: Optional[float] = None,
        at: Optional[str] = None,
    ) -> str:
        """Schedule a reminder at a relative delay or local 24-hour clock time."""
        clean_text = sanitize_reminder_text(text)
        now_dt = _now()

        if minutes is not None and at is not None:
            return "ERROR: Exactly one of 'minutes' or 'at' must be provided."
        if minutes is None and at is None:
            return "ERROR: Exactly one of 'minutes' or 'at' must be provided."

        if minutes is not None:
            try:
                m_val = float(minutes)
            except (ValueError, TypeError):
                return "ERROR: 'minutes' must be a valid positive number."
            if m_val <= 0:
                return "ERROR: 'minutes' must be greater than zero."
            due_dt = now_dt + timedelta(minutes=m_val)
        else:
            parsed = parse_clock(str(at), now=now_dt)
            if parsed is None:
                return f"ERROR: Invalid clock time '{at}'. Use HH:MM in 24-hour local time."
            due_dt = parsed

        try:
            item = store.add("reminder", clean_text, due_dt)
        except ReminderValidationError as exc:
            return f"ERROR: {exc}"
        except ReminderLimit as exc:
            return f"ERROR: {exc}"
        except Exception as exc:
            return f"ERROR: Failed to set reminder: {exc}"

        # Factual short result string: "Reminder 3 set for 17:30 (in 10 minutes)."
        time_str = due_dt.strftime("%H:%M")
        delay_sec = max(0.0, (due_dt - now_dt).total_seconds())
        delay_str = _format_delay(delay_sec)
        return f"Reminder {item.id} set for {time_str} (in {delay_str})."

    def precheck_set_reminder(args: dict[str, Any]) -> Optional[str]:
        text = args.get("text")
        if not text or not isinstance(text, str):
            return "ERROR: 'text' is required and must be a non-empty string."
        clean = sanitize_reminder_text(text)
        if not (1 <= len(clean) <= 100):
            return "ERROR: Reminder text must be 1 to 100 characters after sanitizing."

        minutes = args.get("minutes")
        at = args.get("at")

        if minutes is not None and at is not None:
            return "ERROR: Exactly one of 'minutes' or 'at' must be provided."
        if minutes is None and at is None:
            return "ERROR: Exactly one of 'minutes' or 'at' must be provided."

        if minutes is not None:
            try:
                m_val = float(minutes)
                if m_val <= 0 or math.isnan(m_val) or math.isinf(m_val):
                    return "ERROR: 'minutes' must be a positive number."
                if m_val * 60 < 5:
                    return "ERROR: Reminder due time must be at least 5 seconds in the future."
                if m_val * 60 > 7 * 86400:
                    return "ERROR: Reminder due time cannot be more than 7 days ahead."
            except (ValueError, TypeError):
                return "ERROR: 'minutes' must be a valid number."
        else:
            at_str = str(at).strip()
            now_dt = _now()
            parsed = parse_clock(at_str, now=now_dt)
            if parsed is None:
                return f"ERROR: Invalid clock time '{at}'. Use 'HH:MM' 24-hour local time."

        return None

    def set_timer(
        minutes: float,
        label: str = "Timer",
    ) -> str:
        """Schedule a timer countdown."""
        now_dt = _now()
        try:
            m_val = float(minutes)
        except (ValueError, TypeError):
            return "ERROR: 'minutes' must be a valid positive number."
        if m_val <= 0:
            return "ERROR: 'minutes' must be greater than zero."

        clean_label = sanitize_reminder_text(label) if label else "Timer"
        if not clean_label:
            clean_label = "Timer"

        due_dt = now_dt + timedelta(minutes=m_val)
        try:
            item = store.add("timer", clean_label, due_dt)
        except ReminderValidationError as exc:
            return f"ERROR: {exc}"
        except ReminderLimit as exc:
            return f"ERROR: {exc}"
        except Exception as exc:
            return f"ERROR: Failed to set timer: {exc}"

        time_str = due_dt.strftime("%H:%M")
        delay_sec = max(0.0, (due_dt - now_dt).total_seconds())
        delay_str = _format_delay(delay_sec)
        return f"Timer {item.id} set for {time_str} (in {delay_str})."

    def precheck_set_timer(args: dict[str, Any]) -> Optional[str]:
        minutes = args.get("minutes")
        if minutes is None:
            return "ERROR: 'minutes' is required for set_timer."
        try:
            m_val = float(minutes)
            if m_val <= 0 or math.isnan(m_val) or math.isinf(m_val):
                return "ERROR: 'minutes' must be a positive number."
            if m_val * 60 < 5:
                return "ERROR: Timer must be at least 5 seconds."
            if m_val * 60 > 7 * 86400:
                return "ERROR: Timer cannot be more than 7 days ahead."
        except (ValueError, TypeError):
            return "ERROR: 'minutes' must be a valid number."

        label = args.get("label")
        if label is not None:
            clean_label = sanitize_reminder_text(str(label))
            if len(clean_label) > 100:
                return "ERROR: Timer label cannot exceed 100 characters."
        return None

    def list_reminders() -> str:
        """List all currently active reminders and timers."""
        items = store.list_active()
        if not items:
            return "No active reminders or timers."

        now_dt = _now()
        target_tz = now_dt.tzinfo if now_dt.tzinfo is not None else datetime.now().astimezone().tzinfo
        lines = []
        for it in items:
            due_iso = it.due_at
            try:
                # Parse ISO into timezone-aware datetime matching caller reference timezone
                dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00")).astimezone(target_tz)
                time_str = dt.strftime("%H:%M")
                delay_sec = max(0.0, (dt - now_dt).total_seconds())
                delay_str = _format_delay(delay_sec)
                lines.append(f"[{it.id}] ({it.kind}) {it.text} - due at {time_str} (in {delay_str})")
            except Exception:
                lines.append(f"[{it.id}] ({it.kind}) {it.text} - due at {due_iso}")
        return "\n".join(lines)

    def cancel_reminder(id: int) -> str:
        """Cancel an active reminder or timer by ID."""
        try:
            cid = int(id)
        except (ValueError, TypeError):
            return f"ERROR: Invalid reminder ID '{id}'."

        ok = store.cancel(cid)
        if not ok:
            return f"ERROR: Reminder {cid} not found or not active."
        return f"Cancelled reminder {cid}."

    def precheck_cancel_reminder(args: dict[str, Any]) -> Optional[str]:
        cid = args.get("id")
        if cid is None:
            return "ERROR: 'id' is required."
        try:
            int(cid)
        except (ValueError, TypeError):
            return "ERROR: 'id' must be an integer."
        return None

    def cancel_all_reminders() -> str:
        """Cancel all active reminders and timers."""
        count = store.cancel_all()
        return f"Cancelled {count} active reminder(s)."

    return [
        Tool(
            name="set_reminder",
            description=(
                "Call this whenever the user asks to set a reminder or be reminded about something at a specific time or delay. "
                "Specify 'text' and exactly one of 'minutes' (relative number) or 'at' ('HH:MM' 24-hour local time). "
                "Requires user confirmation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "What to remind the user about (1-100 characters).",
                    },
                    "minutes": {
                        "type": "number",
                        "description": "Relative delay in minutes from now.",
                    },
                    "at": {
                        "type": "string",
                        "description": "Target clock time in 'HH:MM' 24-hour local format.",
                    },
                },
                "required": ["text"],
            },
            func=set_reminder,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_set_reminder,
        ),
        Tool(
            name="set_timer",
            description=(
                "Call this whenever the user asks to set a timer or start a countdown. "
                "Specify 'minutes' as a positive number and optional 'label'. "
                "Requires user confirmation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "number",
                        "description": "Duration of the timer in minutes.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional label for the timer (defaults to 'Timer').",
                    },
                },
                "required": ["minutes"],
            },
            func=set_timer,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_set_timer,
        ),
        Tool(
            name="list_reminders",
            description="Call this whenever the user asks to list, show, or check active reminders and timers.",
            parameters={
                "type": "object",
                "properties": {},
            },
            func=list_reminders,
            permission=PermissionLevel.SAFE,
        ),
        Tool(
            name="cancel_reminder",
            description=(
                "Call this whenever the user asks to cancel, delete, or dismiss a reminder or timer by its ID. "
                "Requires user confirmation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The unique integer ID of the reminder or timer to cancel.",
                    },
                },
                "required": ["id"],
            },
            func=cancel_reminder,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_cancel_reminder,
        ),
        Tool(
            name="cancel_all_reminders",
            description=(
                "Call this whenever the user asks to cancel all reminders or all timers. "
                "Requires user confirmation."
            ),
            parameters={
                "type": "object",
                "properties": {},
            },
            func=cancel_all_reminders,
            permission=PermissionLevel.CONFIRM,
        ),
    ]
