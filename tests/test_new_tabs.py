"""Tests for Today, System, Contacts, Files, and Verify tab views."""

from datetime import datetime, timezone, timedelta
from tkinter import ttk
import pytest

from ui.tab_views import (
    TodayTabView,
    SystemTabView,
    ContactsTabView,
    FilesTabView,
    VerifyTabView,
    format_audit_event_sentence,
    mask_phone_number,
)
from ui.theme import get_palette


from tests.conftest import make_test_root


@pytest.fixture
def tk_root():
    root = make_test_root()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


class FakeApp:
    def __init__(self):
        self.controller = FakeController()
        self.reminder_store = FakeReminderStore()
        self.contacts_store = FakeContactStore()
        self.audit_logger = FakeAuditLogger()
        self.verified_opened = False

    def _on_contacts_click(self):
        self.contacts_opened = True

    def _on_verify_click(self):
        self.verified_opened = True


class FakeController:
    def __init__(self):
        self.submitted = []
        self.session_state = FakeSessionState()
        self.routine_store = FakeRoutineStore()

    def submit(self, text, source="ui"):
        self.submitted.append((text, source))
        return True


class FakeSessionState:
    def __init__(self):
        self._files = []

    def get_found_files(self):
        return list(self._files)


class FakeReminder:
    def __init__(self, text, due_at):
        self.text = text
        self.due_at = due_at


class FakeReminderStore:
    def __init__(self):
        self._items = []

    def list_active(self):
        return list(self._items)


class FakeRoutine:
    def __init__(self, name):
        self.name = name


class FakeRoutineStore:
    def __init__(self):
        self._items = [FakeRoutine("Quick Backup"), FakeRoutine("Clean Temp")]

    def list(self):
        return list(self._items)


class FakeContact:
    def __init__(self, name, phone, notes=""):
        self.name = name
        self.phone = phone
        self.notes = notes


class FakeContactStore:
    def __init__(self):
        self._items = [FakeContact("Alice", "15550100123", "Colleague")]

    def list_all(self):
        return list(self._items)


class FakeAuditLogger:
    def __init__(self):
        self._entries = []

    def read_all(self):
        return list(self._entries)


def test_format_audit_event_sentence_privacy():
    # Test that sentence uses ONLY tool, decision, outcome, omitting secret, args, title, etc.
    entry = {
        "tool": "open_application",
        "decision": "allow",
        "outcome": "success",
        "args": {"name": "secret_doc.txt"},
        "result": "sensitive payload",
        "extra": {"phone": "15550100999", "message": "classified"},
    }
    sentence = format_audit_event_sentence(entry)
    assert sentence == "Success: open_application"
    assert "secret_doc" not in sentence
    assert "sensitive" not in sentence
    assert "15550100999" not in sentence
    assert "classified" not in sentence

    blocked_entry = {"tool": "sleep", "decision": "tainted", "outcome": "blocked"}
    assert format_audit_event_sentence(blocked_entry) == "Blocked: sleep (tainted)"


def test_mask_phone_number():
    assert mask_phone_number("15550100123") == "155-***-0123"
    assert mask_phone_number("5550123") == "***-0123"
    assert mask_phone_number("123") == "****"
    assert mask_phone_number("") == ""


def test_today_tab_view(tk_root):
    palette = get_palette("cyan")
    frame = ttk.Frame(tk_root)
    frame.pack()
    app = FakeApp()

    now_utc = datetime.now(timezone.utc)
    app.reminder_store._items.append(FakeReminder("Drink water", (now_utc + timedelta(minutes=5)).isoformat()))
    app.audit_logger._entries.append({"tool": "get_time", "decision": "allow", "outcome": "success"})

    fake_bat = type("FakeBat", (), {"percent": 88, "power_plugged": True})
    view = TodayTabView(frame, app, palette=palette, battery_probe=lambda: fake_bat)

    view.refresh()
    assert "88%" in view.battery_lbl.cget("text")
    assert "Plugged in" in view.battery_lbl.cget("text")
    assert "Drink water" in view.next_due_lbl.cget("text")
    assert view.events_list.size() == 1
    assert "Success: get_time" in view.events_list.get(0)

    # Click a routine button
    children = view.routine_btn_box.winfo_children()
    assert len(children) >= 1
    children[0].invoke()
    assert ("run routine Quick Backup", "ui") in app.controller.submitted


def test_system_tab_view(tk_root):
    palette = get_palette("cyan")
    frame = ttk.Frame(tk_root)
    frame.pack()
    app = FakeApp()

    cpu_probe = lambda: 35.5
    mem_probe = lambda: 62.0
    disk_probe = lambda: 120.5
    gpu_probe = lambda: {"available": True, "name": "FakeRTX", "utilization": 45.0, "temperature": 55.0}
    proc_probe = lambda: {
        "cpu": [{"pid": 101, "name": "fake_orbit.exe", "cpu": 12.0, "mem": 4.5}],
        "mem": [{"pid": 101, "name": "fake_orbit.exe", "cpu": 12.0, "mem": 4.5}],
    }

    view = SystemTabView(
        frame,
        app,
        palette=palette,
        cpu_probe=cpu_probe,
        mem_probe=mem_probe,
        disk_probe=disk_probe,
        gpu_probe=gpu_probe,
        process_probe=proc_probe,
    )
    view.sample()

    assert "35.5%" in view.stats_lbl.cget("text")
    assert "62.0%" in view.stats_lbl.cget("text")
    assert "120.5 GB" in view.stats_lbl.cget("text")

    procs = view.proc_tree.get_children()
    assert len(procs) == 1
    vals = view.proc_tree.item(procs[0], "values")
    assert vals[0] == "fake_orbit.exe"
    assert vals[1] == "12.0%"
    view.stop()


def test_contacts_tab_view(tk_root):
    palette = get_palette("cyan")
    frame = ttk.Frame(tk_root)
    frame.pack()
    app = FakeApp()

    view = ContactsTabView(frame, app, palette=palette)
    view.refresh()

    assert "Total Contacts: 1" in view.count_lbl.cget("text")
    items = view.tree.get_children()
    assert len(items) == 1
    vals = view.tree.item(items[0], "values")
    assert vals[0] == "Alice"
    assert vals[1] == "155-***-0123"


def test_files_tab_view(tk_root):
    palette = get_palette("cyan")
    frame = ttk.Frame(tk_root)
    frame.pack()
    app = FakeApp()

    view = FilesTabView(frame, app, palette=palette)
    view.refresh()
    # When empty, empty label is packed
    assert view.empty_lbl.winfo_manager() == "pack"

    # Add a file candidate
    app.controller.session_state._files = [r"C:\fake\sample.pdf"]
    view.refresh()
    assert view.empty_lbl.winfo_manager() != "pack"
    items = view.tree.get_children()
    assert len(items) == 1
    vals = view.tree.item(items[0], "values")
    assert vals[0] == "sample.pdf"

    # Select and trigger open
    view.tree.selection_set(items[0])
    view._on_open()
    assert any("sample.pdf" in sub[0] for sub in app.controller.submitted)


def test_verify_tab_view(tk_root):
    palette = get_palette("cyan")
    frame = ttk.Frame(tk_root)
    frame.pack()
    app = FakeApp()

    view = VerifyTabView(frame, app, palette=palette)
    assert "Verification" in view.summary_lbl.cget("text")
    view.app._on_verify_click()
    assert app.verified_opened is True
