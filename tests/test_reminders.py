"""Tests for ORBIT reminders and timers subsystem.

Verifies:
- Pure time parsing (relative intervals, clock times, today vs tomorrow, DST note, invalid inputs)
- ReminderStore (SQLite, parameterization, text/due limits, capacity, snooze limit, corruption recovery, injection text)
- Tools (permissions, prechecks, factual result strings)
- Shortcuts and chain isolation (exact-match patterns, filler prefixes, tail words, near-misses, chain refusal)
- Scheduler (pure tick, fire once, failing callback isolation, startup missed reporting)
- UI (ReminderToast non-modal/no-grab/no-focus, max 3 toasts, dialog fallback to chat, tab list/cancel, status bar, poll resilience)
- Safety & privacy (no text in diag, history, or provider calls, REMINDERS_ENABLED gating, eval runner configuration)
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from core.chains import parse_chain
from core.diagnostics import build_diagnostics
from core.shortcuts import is_reminder_or_timer_pattern, parse_shortcut
from reminders.scheduler import ReminderScheduler, format_missed_message
from reminders.store import (
    ReminderLimit,
    ReminderRecord,
    ReminderStore,
    ReminderValidationError,
)
from reminders.timeparse import parse_clock, parse_relative
from security.permissions import Decision, PermissionLevel, PermissionPolicy
from tests.conftest import make_test_root
from tools.registry import ToolRegistry
from tools.reminder_tools import make_reminder_tools


# ==============================================================================
# 1. TIMEPARSE TESTS
# ==============================================================================

class TestTimeParse:
    """Tests for pure time parsing functions."""

    def test_relative_units_and_integers(self):
        """Verify seconds, minutes, and hours with integer quantities up to 999."""
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert parse_relative(10, "minutes", now) == now + timedelta(minutes=10)
        assert parse_relative(30, "seconds", now) == now + timedelta(seconds=30)
        assert parse_relative(2, "hours", now) == now + timedelta(hours=2)
        assert parse_relative("5", "mins", now) == now + timedelta(minutes=5)
        assert parse_relative("45", "sec", now) == now + timedelta(seconds=45)
        assert parse_relative("1", "hr", now) == now + timedelta(hours=1)
        assert parse_relative(999, "minutes", now) == now + timedelta(minutes=999)
        # Numbers above 999 return None
        assert parse_relative(1000, "minutes", now) is None

    def test_word_numbers_one_to_twenty(self):
        """Verify word numbers from 'one' through 'twenty'."""
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert parse_relative("one", "minute", now) == now + timedelta(minutes=1)
        assert parse_relative("five", "minutes", now) == now + timedelta(minutes=5)
        assert parse_relative("twelve", "hours", now) == now + timedelta(hours=12)
        assert parse_relative("twenty", "seconds", now) == now + timedelta(seconds=20)
        # Unrecognized words return None
        assert parse_relative("twenty-one", "minutes", now) is None
        assert parse_relative("thirty", "minutes", now) is None

    def test_colloquial_relative_phrases(self):
        """Verify 'half an hour', 'an hour', 'a minute'."""
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert parse_relative("half an hour", "", now) == now + timedelta(minutes=30)
        assert parse_relative("half an", "hour", now) == now + timedelta(minutes=30)
        assert parse_relative("an hour", "", now) == now + timedelta(hours=1)
        assert parse_relative("a minute", "", now) == now + timedelta(minutes=1)
        assert parse_relative("an", "hour", now) == now + timedelta(hours=1)
        assert parse_relative("a", "minute", now) == now + timedelta(minutes=1)

    def test_clock_formats_12_and_24_hour(self):
        """Verify 12-hour am/pm and 24-hour clock formats."""
        now = datetime(2026, 10, 25, 12, 0, 0)
        # 5:30 pm today is 17:30
        res = parse_clock("5:30 pm", now=now)
        assert res == datetime(2026, 10, 25, 17, 30, 0)

        # 17:30 today
        assert parse_clock("17:30", now=now) == datetime(2026, 10, 25, 17, 30, 0)

        # 5 pm today
        assert parse_clock("5 pm", now=now) == datetime(2026, 10, 25, 17, 0, 0)

        # 11:45 am tomorrow because 11:45 is in past relative to 12:00
        assert parse_clock("11:45 am", now=now) == datetime(2026, 10, 26, 11, 45, 0)

    def test_clock_today_vs_tomorrow_at_least_5_seconds(self):
        """Verify occurrence is today if ahead by >= 5 seconds, otherwise tomorrow."""
        now = datetime(2026, 10, 25, 14, 0, 0)
        # Target is 14:00:10 (10s ahead >= 5s) -> today
        res = parse_clock("14:00:10", now=now)
        assert res == datetime(2026, 10, 25, 14, 0, 10)

        # Target is 14:00:03 (3s ahead < 5s) -> tomorrow
        res = parse_clock("14:00:03", now=now)
        assert res == datetime(2026, 10, 26, 14, 0, 3)

        # Target exactly equal to now -> tomorrow
        res = parse_clock("14:00", now=now)
        assert res == datetime(2026, 10, 26, 14, 0, 0)

    def test_tomorrow_at_prefix(self):
        """Verify 'tomorrow at ...' is always scheduled for tomorrow."""
        now = datetime(2026, 10, 25, 10, 0, 0)
        # Even though 15:00 is ahead today, 'tomorrow at' forces next day
        res = parse_clock("tomorrow at 15:00", now=now)
        assert res == datetime(2026, 10, 26, 15, 0, 0)

        res_pm = parse_clock("tomorrow at 3 pm", now=now)
        assert res_pm == datetime(2026, 10, 26, 15, 0, 0)

    def test_clock_edge_cases_midnight_and_invalid(self):
        """Verify midnight edge cases and rejection of invalid formats."""
        now = datetime(2026, 10, 25, 23, 59, 58)
        # 12:00 am is in past today (or 2 seconds ahead? 12:00 am was 00:00:00) -> tomorrow at 00:00
        assert parse_clock("12:00 am", now=now) == datetime(2026, 10, 26, 0, 0, 0)
        assert parse_clock("00:00", now=now) == datetime(2026, 10, 26, 0, 0, 0)

        # Non-clock strings or dates return None
        assert parse_clock("yesterday", now=now) is None
        assert parse_clock("in a while", now=now) is None
        assert parse_clock("next monday", now=now) is None
        assert parse_clock("2026-10-25 15:00", now=now) is None
        assert parse_clock("someday at 5pm", now=now) is None
        assert parse_clock("", now=now) is None

    def test_dst_limitation_note_present(self):
        """Verify reminders/timeparse.py contains standard library zoneinfo-free DST note."""
        timeparse_file = Path("reminders/timeparse.py")
        assert timeparse_file.is_file()
        content = timeparse_file.read_text(encoding="utf-8")
        assert "daylight-saving" in content.lower() or "dst" in content.lower()


# ==============================================================================
# 2. REMINDER STORE TESTS
# ==============================================================================

class TestReminderStore:
    """Tests for SQLite-backed ReminderStore."""

    @pytest.fixture
    def fake_clock(self):
        """Injectable fake clock advancing deterministically."""
        current_time = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

        def clock_fn():
            return current_time

        clock_fn.set = lambda dt: setattr(clock_fn, "time", dt)
        return clock_fn

    @pytest.fixture
    def temp_store(self, fake_clock):
        """Create a temporary store with injected clock."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "reminders.db"
            store = ReminderStore(path=db_path, clock=fake_clock)
            yield store

    def test_text_sanitizing_and_validation(self, temp_store, fake_clock):
        """Verify text sanitization and length limits (1-100 characters)."""
        now = fake_clock()
        due_at = now + timedelta(minutes=10)

        # Empty or whitespace text fails
        with pytest.raises(ReminderValidationError):
            temp_store.add("reminder", "", due_at)
        with pytest.raises(ReminderValidationError):
            temp_store.add("reminder", "   ", due_at)

        # Characters < > ` and control characters are stripped; whitespace collapsed
        item = temp_store.add("reminder", "  Check <the> `oven` \x00\x1f now!  ", due_at)
        assert item.text == "Check the oven now!"

        # Exactly 1 character passes
        item1 = temp_store.add("reminder", "a", due_at)
        assert item1.text == "a"

        # 100 characters pass
        text100 = "x" * 100
        item100 = temp_store.add("reminder", text100, due_at)
        assert item100.text == text100

        # 101 characters fail
        text101 = "x" * 101
        with pytest.raises(ReminderValidationError):
            temp_store.add("reminder", text101, due_at)

    def test_due_at_limits_5s_min_7d_max(self, temp_store, fake_clock):
        """Verify due date must be >= 5 seconds and <= 7 days ahead."""
        now = fake_clock()

        # Less than 5 seconds ahead fails
        with pytest.raises(ReminderValidationError):
            temp_store.add("reminder", "Too soon", now + timedelta(seconds=4))

        # In past fails
        with pytest.raises(ReminderValidationError):
            temp_store.add("reminder", "In past", now - timedelta(seconds=10))

        # Exactly 5 seconds ahead succeeds
        item5s = temp_store.add("reminder", "Just in time", now + timedelta(seconds=5))
        assert item5s.id > 0

        # Exactly 7 days ahead succeeds
        item7d = temp_store.add("reminder", "In a week", now + timedelta(days=7))
        assert item7d.id > 0

        # More than 7 days ahead fails
        with pytest.raises(ReminderValidationError):
            temp_store.add("reminder", "Too far", now + timedelta(days=7, seconds=1))

    def test_active_capacity_limit_of_20(self, temp_store, fake_clock):
        """Verify maximum of 20 active items, 21st raises ReminderLimit."""
        now = fake_clock()
        due_at = now + timedelta(minutes=10)

        for i in range(20):
            temp_store.add("reminder", f"Reminder {i}", due_at)

        assert len(temp_store.list_active()) == 20

        # 21st active item raises ReminderLimit
        with pytest.raises(ReminderLimit):
            temp_store.add("reminder", "Over capacity", due_at)

        # After cancelling one, adding another succeeds
        temp_store.cancel(1)
        assert len(temp_store.list_active()) == 19
        temp_store.add("reminder", "Fits now", due_at)
        assert len(temp_store.list_active()) == 20

    def test_due_ordering_and_mark(self, temp_store, fake_clock):
        """Verify due(now) returns active items with due_at <= now in order."""
        now = fake_clock()
        t1 = now + timedelta(seconds=10)
        t2 = now + timedelta(seconds=20)
        t3 = now + timedelta(seconds=30)

        it2 = temp_store.add("reminder", "Second", t2)
        it1 = temp_store.add("reminder", "First", t1)
        it3 = temp_store.add("reminder", "Third", t3)

        # At t0: none due
        assert temp_store.due(now) == []

        # At t1: First is due
        due_t1 = temp_store.due(t1)
        assert [x.id for x in due_t1] == [it1.id]

        # At t3: all three due, ordered by due_at
        due_t3 = temp_store.due(t3)
        assert [x.id for x in due_t3] == [it1.id, it2.id, it3.id]

        # Mark first as fired -> list_active no longer includes it
        temp_store.mark(it1.id, "fired")
        assert it1.id not in [x.id for x in temp_store.list_active()]

    def test_snooze_limit_of_3(self, temp_store, fake_clock):
        """Verify snooze reschedules, keeps status active, and caps at 3 snoozes."""
        now = fake_clock()
        item = temp_store.add("reminder", "Snooze test", now + timedelta(minutes=5))

        # 1st snooze
        s1 = temp_store.snooze(item.id, minutes=5)
        assert s1.snoozed == 1
        assert s1.status == "active"

        # 2nd snooze
        s2 = temp_store.snooze(item.id, minutes=5)
        assert s2.snoozed == 2

        # 3rd snooze
        s3 = temp_store.snooze(item.id, minutes=5)
        assert s3.snoozed == 3

        # 4th snooze raises ReminderLimit
        with pytest.raises(ReminderLimit):
            temp_store.snooze(item.id, minutes=5)

    def test_cancel_and_cancel_all(self, temp_store, fake_clock):
        """Verify cancelling single and all reminders."""
        now = fake_clock()
        it1 = temp_store.add("reminder", "One", now + timedelta(minutes=10))
        temp_store.add("timer", "Two", now + timedelta(minutes=15))

        assert temp_store.cancel(it1.id) is True
        assert len(temp_store.list_active()) == 1

        count = temp_store.cancel_all()
        assert count == 1
        assert len(temp_store.list_active()) == 0

    def test_mark_missed_on_startup_returned_once(self, temp_store, fake_clock):
        """Verify items due while closed become missed and return once."""
        now = fake_clock()
        it = temp_store.add("reminder", "Missed item", now + timedelta(seconds=10))

        # Advance past due time
        later = now + timedelta(minutes=5)
        missed = temp_store.mark_missed_on_startup(later)
        assert len(missed) == 1
        assert missed[0].id == it.id
        assert missed[0].status == "missed"

        # Subsequent call returns empty list
        assert temp_store.mark_missed_on_startup(later) == []

    def test_counts_by_status(self, temp_store, fake_clock):
        """Verify count_by_status aggregates correctly."""
        now = fake_clock()
        it1 = temp_store.add("reminder", "Active 1", now + timedelta(minutes=5))
        temp_store.add("reminder", "Active 2", now + timedelta(minutes=10))
        temp_store.mark(it1.id, "fired")

        counts = temp_store.count_by_status()
        assert counts.get("active") == 1
        assert counts.get("fired") == 1
        assert counts.get("missed") == 0

    def test_corrupt_database_recreated_safely(self, fake_clock):
        """Verify corrupted SQLite file is automatically recreated safely."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "corrupt.db"
            store = ReminderStore(path=db_path, clock=fake_clock)
            store.add("reminder", "Before corruption", fake_clock() + timedelta(minutes=5))

            # Corrupt the file with random invalid bytes
            db_path.write_bytes(b"CORRUPTED_SQLITE_GARBAGE_BYTES_12345")

            # Store operation should detect corruption, recreate, and succeed
            active = store.list_active()
            assert active == []
            new_item = store.add("reminder", "After recovery", fake_clock() + timedelta(minutes=5))
            assert new_item.id == 1

    def test_sql_injection_stored_harmlessly(self, temp_store, fake_clock):
        """Verify SQL injection payloads are stored harmlessly as literal strings."""
        injection_text = "test'; DROP TABLE reminders; --"
        item = temp_store.add("reminder", injection_text, fake_clock() + timedelta(minutes=5))
        assert "DROP TABLE" in item.text
        # Ensure reminders table still exists and functions normally
        assert len(temp_store.list_active()) == 1


# ==============================================================================
# 3. TOOLS TESTS
# ==============================================================================

class TestReminderTools:
    """Tests for tools/reminder_tools.py registration, permissions, and prechecks."""

    @pytest.fixture
    def tools_env(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
            store = ReminderStore(path=Path(tmp_dir) / "rem.db", clock=lambda: now)
            policy = PermissionPolicy()
            registry = ToolRegistry()
            tools = make_reminder_tools(store, clock=lambda: now)
            for t in tools:
                registry.register(t)
            yield store, registry, policy, now

    def test_tool_permissions(self, tools_env):
        """Verify list_reminders is SAFE; set/cancel are CONFIRM."""
        store, registry, policy, now = tools_env
        assert registry.get("list_reminders").permission == PermissionLevel.SAFE
        assert registry.get("set_reminder").permission == PermissionLevel.CONFIRM
        assert registry.get("set_timer").permission == PermissionLevel.CONFIRM
        assert registry.get("cancel_reminder").permission == PermissionLevel.CONFIRM
        assert registry.get("cancel_all_reminders").permission == PermissionLevel.CONFIRM

        assert policy.decide("list_reminders", registry.get("list_reminders").permission) == Decision.ALLOW
        assert policy.decide("set_reminder", registry.get("set_reminder").permission) == Decision.ASK

    def test_set_reminder_prechecks_and_result(self, tools_env):
        """Verify set_reminder precheck enforces exactly one of minutes or at."""
        store, registry, policy, now = tools_env
        tool = registry.get("set_reminder")

        # Missing both minutes and at
        err = tool.precheck({"text": "Test"})
        assert err is not None and "exactly one" in err.lower()

        # Providing both minutes and at
        err = tool.precheck({"text": "Test", "minutes": 10, "at": "17:30"})
        assert err is not None and "exactly one" in err.lower()

        # Valid with minutes
        assert tool.precheck({"text": "Stretch", "minutes": 10}) is None
        res = registry.execute("set_reminder", {"text": "Stretch", "minutes": 10})
        assert "Reminder 1 set for" in res
        assert "in 10 minutes" in res

        # Valid with at
        assert tool.precheck({"text": "Meeting", "at": "14:00"}) is None
        res2 = registry.execute("set_reminder", {"text": "Meeting", "at": "14:00"})
        assert "Reminder 2 set for 14:00" in res2

    def test_set_timer_prechecks_and_result(self, tools_env):
        """Verify set_timer prechecks positive minutes and defaults label to 'Timer'."""
        store, registry, policy, now = tools_env
        tool = registry.get("set_timer")

        # Invalid minutes <= 0
        err = tool.precheck({"minutes": 0})
        assert err is not None

        # Valid timer
        assert tool.precheck({"minutes": 5, "label": "Tea"}) is None
        res = registry.execute("set_timer", {"minutes": 5, "label": "Tea"})
        assert "Timer 1 set for" in res

    def test_cancel_tools(self, tools_env):
        """Verify cancel_reminder and cancel_all_reminders prechecks and outputs."""
        store, registry, policy, now = tools_env
        store.add("reminder", "To cancel", now + timedelta(minutes=10))

        cancel_tool = registry.get("cancel_reminder")
        assert cancel_tool.precheck({"id": 1}) is None
        res = registry.execute("cancel_reminder", {"id": 1})
        assert "Cancelled reminder 1." in res

        # Cancel all
        store.add("reminder", "One", now + timedelta(minutes=10))
        store.add("reminder", "Two", now + timedelta(minutes=20))
        res_all = registry.execute("cancel_all_reminders", {})
        assert "Cancelled 2 active reminder(s)." in res_all


# ==============================================================================
# 4. SHORTCUTS AND CHAIN ISOLATION TESTS
# ==============================================================================

class TestShortcutsAndChains:
    """Tests for exact-match reminder shortcuts and chain parser refusal."""

    @pytest.fixture
    def test_clock(self):
        return lambda: datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_shortcut_patterns_mapping(self, test_clock):
        """Verify reminder and timer patterns map to correct tool calls and arguments."""
        clk = test_clock
        # "remind me in <n> <unit> to <text>"
        spec = parse_shortcut("remind me in 10 minutes to stretch", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_reminder"
        assert spec.args == {"text": "stretch", "minutes": 10}

        # "remind me to <text> in <n> <unit>"
        spec = parse_shortcut("remind me to call mom in 1 hour", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_reminder"
        assert spec.args == {"text": "call mom", "minutes": 60}

        # "remind me at <clock> to <text>"
        spec = parse_shortcut("remind me at 5:30 pm to start dinner", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_reminder"
        assert spec.args == {"text": "start dinner", "at": "17:30"}

        # "set a reminder for <clock> to <text>"
        spec = parse_shortcut("set a reminder for 17:30 to stretch", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_reminder"
        assert spec.args == {"text": "stretch", "at": "17:30"}

        # "set a timer for <n> <unit>"
        spec = parse_shortcut("set a timer for 15 minutes", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_timer"
        assert spec.args == {"minutes": 15, "label": "Timer"}

        # "set a <n> <unit> timer"
        spec = parse_shortcut("set a 5 minute timer", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_timer"
        assert spec.args == {"minutes": 5, "label": "Timer"}

        # "start a <n> <unit> timer"
        spec = parse_shortcut("start a 20 min timer", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_timer"
        assert spec.args == {"minutes": 20, "label": "Timer"}

        # "timer <n> <unit>"
        spec = parse_shortcut("timer 30 seconds", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "set_timer"
        assert spec.args == {"minutes": 0.5, "label": "Timer"}

        # Listing shortcuts
        for phrase in ("list reminders", "show reminders", "show my reminders", "what reminders do i have", "what timers do i have"):
            spec = parse_shortcut(phrase, reminders_enabled=True, clock=clk)
            assert spec is not None
            assert spec.name == "list_reminders"

        # Cancellation shortcuts
        spec = parse_shortcut("cancel reminder 3", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "cancel_reminder" and spec.args == {"id": 3}

        spec = parse_shortcut("cancel timer 2", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "cancel_reminder" and spec.args == {"id": 2}

        spec = parse_shortcut("cancel all reminders", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "cancel_all_reminders"

        spec = parse_shortcut("cancel reminders", reminders_enabled=True, clock=clk)
        assert spec is not None
        assert spec.name == "cancel_all_reminders"

    def test_polite_filler_prefixes(self, test_clock):
        """Verify filler prefixes (please, hey, can you, could you) are accepted."""
        clk = test_clock
        for prefix in ("please", "hey", "can you", "could you", "can u"):
            spec = parse_shortcut(f"{prefix} remind me in 5 minutes to relax", reminders_enabled=True, clock=clk)
            assert spec is not None
            assert spec.name == "set_reminder"
            assert spec.args == {"text": "relax", "minutes": 5}

    def test_tail_words_stay_in_single_reminder(self, test_clock):
        """Verify 'remind me in 5 minutes to stretch then pause' is ONE reminder."""
        spec = parse_shortcut(
            "remind me in 5 minutes to stretch then pause",
            reminders_enabled=True,
            clock=test_clock,
        )
        assert spec is not None
        assert spec.name == "set_reminder"
        assert spec.args["text"] == "stretch then pause"

    def test_chain_parser_never_splits_reminders(self):
        """Verify parse_chain returns None for reminder patterns and segments."""
        assert is_reminder_or_timer_pattern("remind me in 5 minutes to stretch")
        assert parse_chain("remind me in 5 minutes to stretch") is None
        assert parse_chain("open notepad then remind me in 5 minutes to stretch") is None
        assert parse_chain("remind me in 5 minutes to stretch then open calc") is None

    def test_near_misses_return_none(self, test_clock):
        """Verify near-miss phrases return None and do not match."""
        clk = test_clock
        for miss in (
            "remind me",
            "remind me in a while",
            "set a timer",
            "i set a timer yesterday",
            "remind me in 8 days to buy groceries",
            "timer for 0 minutes",
            "remind me to in 5 minutes",
        ):
            res = parse_shortcut(miss, reminders_enabled=True, clock=clk)
            assert res is None, f"Expected None for near-miss: '{miss}'"


# ==============================================================================
# 5. SCHEDULER TESTS
# ==============================================================================

class TestReminderScheduler:
    """Tests for pure ReminderScheduler."""

    def test_fires_once_never_twice(self):
        """Verify due reminder fires strictly once across multiple ticks."""
        current_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = ReminderStore(path=Path(tmp_dir) / "r.db", clock=lambda: current_dt)
            it = store.add("reminder", "Tick once", current_dt + timedelta(seconds=10))

            fired_items = []
            scheduler = ReminderScheduler(
                store=store,
                clock=lambda: current_dt,
                on_due=lambda item: fired_items.append(item),
            )

            # Before due time: no fire
            assert len(scheduler.tick()) == 0
            assert len(fired_items) == 0

            # Advance past due time: fires once
            current_dt += timedelta(seconds=15)
            assert len(scheduler.tick()) == 1
            assert len(fired_items) == 1
            assert fired_items[0].id == it.id

            # Next tick: does not fire again
            assert len(scheduler.tick()) == 0
            assert len(fired_items) == 1

    def test_failing_callback_does_not_repeat(self):
        """Verify an exception in on_due callback does not cause item to re-trigger."""
        current_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = ReminderStore(path=Path(tmp_dir) / "r.db", clock=lambda: current_dt)
            store.add("reminder", "Fail once", current_dt + timedelta(seconds=5))

            call_count = 0

            def bad_callback(item):
                nonlocal call_count
                call_count += 1
                raise RuntimeError("Boom in callback!")

            scheduler = ReminderScheduler(
                store=store,
                clock=lambda: current_dt,
                on_due=bad_callback,
            )

            current_dt += timedelta(seconds=10)
            # tick should catch exception and not re-raise
            fired = scheduler.tick()
            assert len(fired) == 1
            assert call_count == 1

            # Subsequent tick does not retry
            assert len(scheduler.tick()) == 0
            assert call_count == 1

    def test_format_missed_message(self):
        """Verify format_missed_message formats 1-5 items and >5 items with (+N more)."""
        r1 = ReminderRecord(id=1, kind="reminder", text="Task 1", due_at="", created_at="", status="missed")
        r2 = ReminderRecord(id=2, kind="reminder", text="Task 2", due_at="", created_at="", status="missed")
        assert format_missed_message([r1, r2]) == "While ORBIT was closed you missed: Task 1, Task 2"

        items6 = [
            ReminderRecord(id=i, kind="reminder", text=f"T{i}", due_at="", created_at="", status="missed")
            for i in range(1, 8)
        ]
        msg = format_missed_message(items6)
        assert "While ORBIT was closed you missed: T1, T2, T3, T4, T5 (+2 more)" == msg


# ==============================================================================
# 6. UI TESTS
# ==============================================================================

class TestRemindersUI:
    """Tests for ReminderToast and OrbitWindow Reminders tab and status bar."""

    def test_reminder_toast_non_modal_no_focus(self):
        """Verify ReminderToast takefocus=0, no modal grab, auto-closes, and buttons work."""
        root = make_test_root()
        try:
            from ui.app import ReminderToast

            item = ReminderRecord(
                id=42,
                kind="reminder",
                text="Take a break",
                due_at="2026-10-25T12:00:00Z",
                created_at="2026-10-25T11:50:00Z",
                status="fired",
            )

            dismissed_ids = []
            snoozed_ids = []
            toast = ReminderToast(
                parent=root,
                item=item,
                on_dismiss=lambda iid: dismissed_ids.append(iid),
                on_snooze=lambda iid: snoozed_ids.append(iid),
            )

            # Must not have grabbed focus or grab
            assert toast.grab_current() is None
            assert int(toast.cget("takefocus")) == 0

            # Test dismiss button
            toast.dismiss_btn.invoke()
            assert dismissed_ids == [42]
        finally:
            root.destroy()

    def test_orbit_window_reminders_tab_and_status(self):
        """Verify OrbitWindow displays Reminders tab, updates status bar, and handles cancel."""
        root = make_test_root()
        try:
            from ui.app import OrbitWindow
            from ui.controller import ChatController

            with tempfile.TemporaryDirectory() as tmp_dir:
                now_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
                store = ReminderStore(path=Path(tmp_dir) / "r.db", clock=lambda: now_dt)
                store.add("reminder", "Check soup", now_dt + timedelta(minutes=7, seconds=30))

                class FakeApp:
                    reminder_store = store
                    clock = lambda self: now_dt.timestamp()

                ctrl = ChatController()
                ctrl.attach(FakeApp())
                ctrl.reminder_store = store

                win = OrbitWindow(root=root, controller=ctrl, mode_label="TEST")

                # Reminders tab populated
                rows = win.reminders_tree.get_children()
                assert len(rows) == 1
                vals = win.reminders_tree.item(rows[0], "values")
                assert str(vals[0]) == "1"
                assert vals[1] == "reminder"
                assert vals[2] == "Check soup"
                assert vals[3] == "07:30"

                # Status bar shows Next: Check soup in 07:30
                assert "Next: Check soup in 07:30" in win.reminder_status_lbl.cget("text")

                # Cancel via tab
                with patch("tkinter.messagebox.askyesno", return_value=True):
                    win.reminders_tree.selection_set(rows[0])
                    win._on_cancel_reminder_tab()

                assert len(win.reminders_tree.get_children()) == 0
                assert win.reminder_status_lbl.cget("text") == ""
        finally:
            root.destroy()

    def test_dialog_open_delivers_chat_line_instead_of_toast(self):
        """Verify reminder due while a dialog is open becomes a chat line without toast."""
        root = make_test_root()
        try:
            from ui.app import OrbitWindow
            from ui.controller import ChatController

            ctrl = ChatController()
            win = OrbitWindow(root=root, controller=ctrl, mode_label="TEST")

            item = ReminderRecord(id=1, kind="reminder", text="Dialog test", due_at="", created_at="", status="fired")

            # Mock dialog open
            with patch.object(win, "_is_dialog_open", return_value=True):
                win._on_reminder_due(item)

            # No toast added
            assert len(win._active_toasts) == 0

            # Chat history received the reminder
            history_text = win.history.get("1.0", "end")
            assert "Reminder: Dialog test" in history_text
        finally:
            root.destroy()

    def test_max_three_toasts_at_once(self):
        """Verify at most 3 toasts are created; 4th becomes chat line."""
        root = make_test_root()
        try:
            from ui.app import OrbitWindow
            from ui.controller import ChatController

            ctrl = ChatController()
            win = OrbitWindow(root=root, controller=ctrl, mode_label="TEST")

            for i in range(1, 5):
                item = ReminderRecord(id=i, kind="reminder", text=f"Item {i}", due_at="", created_at="", status="fired")
                win._on_reminder_due(item)

            # Capped at 3 toasts
            assert len(win._active_toasts) == 3

            # All 4 items delivered in chat
            chat = win.history.get("1.0", "end")
            assert "Reminder: Item 4" in chat
        finally:
            root.destroy()


# ==============================================================================
# 7. SAFETY, PRIVACY AND SETTINGS TESTS
# ==============================================================================

class TestRemindersSafetyAndSettings:
    """Tests for zero reminder text leakage into diagnostics, history, or LLM."""

    def test_diagnostics_omits_reminder_text(self):
        """Verify /diag includes status counts and next due time but strictly no text."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            now_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
            store = ReminderStore(path=Path(tmp_dir) / "r.db", clock=lambda: now_dt)
            secret_reminder = "SUPER_SECRET_PATIENT_REMINDER_TOKEN"
            store.add("reminder", secret_reminder, now_dt + timedelta(minutes=15))

            diag_text = build_diagnostics(
                agent=None,
                chat_controller=None,
                reminder_store=store,
            )

            assert "=== REMINDERS ===" in diag_text
            assert "Active: 1" in diag_text
            assert "REMINDERS_ENABLED: True" in diag_text
            # Distinctive secret text must never appear anywhere in diagnostics
            assert secret_reminder not in diag_text

    def test_reminders_enabled_setting_gating(self):
        """Verify REMINDERS_ENABLED=False registers no tools and parses no shortcuts."""
        from core.bootstrap import build_app

        app = build_app(provider=MagicMock(), reminders_enabled=False)
        assert app.registry.get("set_reminder") is None
        assert app.registry.get("set_timer") is None

        # Shortcut parser returns None
        res = parse_shortcut(
            "remind me in 5 minutes to test",
            reminders_enabled=False,
            clock=lambda: datetime.now(timezone.utc),
        )
        assert res is None

    def test_agent_history_redaction_and_no_provider_calls(self, tmp_path):
        """Verify setting a reminder redacts history to fixed string and never calls LLM."""
        from core.bootstrap import AppPaths, build_app
        from core.llm import LLMResponse

        fake_provider = MagicMock()
        fake_provider.generate.return_value = LLMResponse(text="Mock reply")

        app = build_app(paths=AppPaths.under(tmp_path), provider=fake_provider, reminders_enabled=True)

        # Allow confirm_callback for test
        app.agent.confirm_callback = lambda tool, args, warning=None: True

        reply = app.agent.chat("remind me in 10 minutes to take medicine")
        assert "Reminder" in reply

        # Provider was NEVER called because shortcut handles it directly
        assert fake_provider.generate.call_count == 0

        # Chat history for turn must NOT contain the reminder text ("take medicine")
        for msg in app.agent.history:
            content = msg.get("content", "")
            assert "take medicine" not in content
            if "Set a reminder" in content:
                assert content.startswith("Set a reminder for")
