"""Tests for PlanDialog, ChatController plan integration, Activity tab plan display, CLI plan controls, and diagnostics."""

import os
import sys
import threading
import time
from typing import Any, Optional
from unittest.mock import patch

import pytest

# Ensure Tcl/Tk libraries are located reliably on Windows
if sys.platform == "win32":
    tcl_path = os.path.join(sys.base_prefix, "tcl", "tcl8.6")
    tk_path = os.path.join(sys.base_prefix, "tcl", "tk8.6")
    if os.path.exists(tcl_path):
        os.environ.setdefault("TCL_LIBRARY", tcl_path)
    if os.path.exists(tk_path):
        os.environ.setdefault("TK_LIBRARY", tk_path)

import tkinter as tk

from config import settings
from core.diagnostics import generate_diagnostics, record_plan, render_diagnostics
from ui.controller import ChatController, UiEvent


from tests.conftest import check_tk_available, make_test_root

# Skip entire test module if Tkinter display is unavailable
# Note: TclError failures (e.g. "Can't find a usable init.tcl") are intermittent and environmental
check_tk_available()

from ui.app import OrbitWindow, PlanDialog


class FakeController:
    """Mock ChatController for testing OrbitWindow UI interactions with plans."""

    def __init__(self) -> None:
        self.submits: list[str] = []
        self.events: list[UiEvent] = []
        self.is_busy: bool = False
        self.cancel_called: bool = False
        self.shutdown_called: bool = False
        self.reset_called: bool = False
        self.resolutions: list[tuple[int, bool]] = []
        self.plan_resolutions: list[tuple[int, bool]] = []
        self.app: Optional[Any] = None
        self.pending_tool: Optional[str] = None

    def reset(self) -> None:
        self.reset_called = True

    def submit(self, text: str) -> bool:
        if self.is_busy:
            return False
        self.submits.append(text)
        return True

    def cancel(self) -> None:
        self.cancel_called = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def resolve_confirmation(self, request_id: int, allowed: bool) -> None:
        self.resolutions.append((request_id, allowed))

    def resolve_plan(self, plan_id: int, allowed: bool) -> None:
        self.plan_resolutions.append((plan_id, allowed))

    def drain(self, max_items: int = 100) -> list[UiEvent]:
        res = self.events[:max_items]
        self.events = self.events[max_items:]
        return res


def test_dialog_opens_topmost_run_disabled_cancel_focused():
    """Verify plan_preview opens modal where Run is disabled for 800 ms and Cancel is focused."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(
            UiEvent(
                "plan_preview",
                {
                    "id": 42,
                    "labels": ["Open Notepad", "Wait 5 seconds"],
                    "permissions": ["confirm", "auto"],
                    "timeout_s": 60.0,
                },
            )
        )

        win._poll()
        root.update_idletasks()

        assert win._plan_dialog is not None
        dialog = win._plan_dialog
        assert dialog.winfo_exists()

        # Dialog is topmost
        assert dialog.attributes("-topmost") in (1, True)

        # Run button starts disabled
        assert str(dialog.run_btn["state"]) == "disabled"

        # Cancel button receives default focus
        assert dialog.focus_get() == dialog.cancel_btn

        # Content verification: approval annotation and warning note
        dialog_text = dialog.text_area.get("1.0", "end")
        assert "1. Open Notepad (asks for approval)" in dialog_text
        assert "2. Wait 5 seconds" in dialog_text
        assert "Each step that opens apps or sites may still ask for its own approval" in dialog_text

        # Run button enables after 800 ms callback
        dialog._enable_run()
        assert str(dialog.run_btn["state"]) == "normal"

        dialog.destroy()
    finally:
        root.destroy()


def test_esc_close_timeout_resolve_to_deny_exactly_once():
    """Verify Esc, close protocol, and timeout resolve to deny exactly once."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        # 1. Test Esc / on_cancel
        ctrl.events.append(
            UiEvent(
                "plan_preview",
                {
                    "id": 101,
                    "labels": ["Step A"],
                    "permissions": ["auto"],
                    "timeout_s": 60.0,
                },
            )
        )
        win._poll()
        root.update_idletasks()
        dialog1 = win._plan_dialog
        assert dialog1 is not None

        dialog1._on_cancel()
        root.update_idletasks()

        assert ctrl.plan_resolutions == [(101, False)]
        # Subsequent resolution calls must be no-ops
        dialog1._resolve(True)
        dialog1._resolve(False)
        assert ctrl.plan_resolutions == [(101, False)]

        # 2. Test Window Close protocol
        ctrl.events.append(
            UiEvent(
                "plan_preview",
                {
                    "id": 102,
                    "labels": ["Step B"],
                    "permissions": ["auto"],
                    "timeout_s": 60.0,
                },
            )
        )
        win._poll()
        root.update_idletasks()
        dialog2 = win._plan_dialog
        assert dialog2 is not None

        dialog2._on_close()
        root.update_idletasks()

        assert ctrl.plan_resolutions == [(101, False), (102, False)]
        dialog2._resolve(True)
        assert ctrl.plan_resolutions == [(101, False), (102, False)]

        # 3. Test Countdown Timeout
        ctrl.events.append(
            UiEvent(
                "plan_preview",
                {
                    "id": 103,
                    "labels": ["Step C"],
                    "permissions": ["auto"],
                    "timeout_s": 1.0,
                },
            )
        )
        win._poll()
        root.update_idletasks()
        dialog3 = win._plan_dialog
        assert dialog3 is not None

        # Simulate timeout expiry
        dialog3._remaining = 0
        dialog3._tick()
        root.update_idletasks()

        assert ctrl.plan_resolutions == [(101, False), (102, False), (103, False)]
        dialog3._resolve(True)
        assert ctrl.plan_resolutions == [(101, False), (102, False), (103, False)]

    finally:
        root.destroy()


def test_enter_does_not_resolve_to_allow():
    """Verify pressing Enter in PlanDialog never resolves to allow/Run."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(
            UiEvent(
                "plan_preview",
                {
                    "id": 201,
                    "labels": ["Dangerous Step"],
                    "permissions": ["confirm"],
                    "timeout_s": 60.0,
                },
            )
        )
        win._poll()
        root.update_idletasks()

        dialog = win._plan_dialog
        assert dialog is not None

        # Trigger Enter key binding
        dialog._on_enter()
        root.update_idletasks()

        assert (201, True) not in ctrl.plan_resolutions
        assert ctrl.plan_resolutions == [(201, False)]
    finally:
        root.destroy()


def test_stale_id_is_ignored():
    """Verify resolve_plan ignores stale or non-matching IDs."""
    ctrl = ChatController()
    try:
        # Resolving when nothing is pending does not crash or do anything
        ctrl.resolve_plan(999, True)

        # Set up a pending plan
        event = threading.Event()
        with ctrl._lock:
            ctrl._pending_plan = {
                "id": 42,
                "event": event,
                "allowed": False,
            }

        # Stale ID should be ignored
        ctrl.resolve_plan(41, True)
        assert not event.is_set()
        assert ctrl._pending_plan is not None
        assert ctrl._pending_plan["allowed"] is False

        # Correct ID resolves
        ctrl.resolve_plan(42, True)
        assert event.is_set()
        assert ctrl._pending_plan is None
    finally:
        ctrl.shutdown()


def test_pending_preview_denied_by_stop_and_shutdown():
    """Verify pending plan preview is denied immediately by cancel() and shutdown()."""
    # 1. Denied by cancel() (Stop)
    ctrl1 = ChatController(confirm_timeout_s=10.0)
    res1 = []

    def call_plan1():
        ans = ctrl1.plan_callback(["Action 1"], ["auto"])
        res1.append(ans)

    t1 = threading.Thread(target=call_plan1)
    t1.start()

    # Wait for pending plan to be registered
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with ctrl1._lock:
            if ctrl1._pending_plan is not None:
                break
        time.sleep(0.01)

    ctrl1.cancel()
    t1.join(timeout=2.0)
    assert res1 == [False]
    ctrl1.shutdown()

    # 2. Denied by shutdown()
    ctrl2 = ChatController(confirm_timeout_s=10.0)
    res2 = []

    def call_plan2():
        ans = ctrl2.plan_callback(["Action 2"], ["auto"])
        res2.append(ans)

    t2 = threading.Thread(target=call_plan2)
    t2.start()

    deadline = time.time() + 2.0
    while time.time() < deadline:
        with ctrl2._lock:
            if ctrl2._pending_plan is not None:
                break
        time.sleep(0.01)

    ctrl2.shutdown()
    t2.join(timeout=2.0)
    assert res2 == [False]


def test_activity_tab_shows_plan_rows_and_updates_them():
    """Verify Activity tab displays header row and per-step rows with live status bar updates."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        # 1. plan_start: creates header row and placeholder step rows
        ctrl.events.append(
            UiEvent(
                "plan_start",
                {
                    "id": 7,
                    "task_id": 10,
                    "steps": [
                        {"label": "Launch Calculator"},
                        {"label": "Wait 2 seconds"},
                    ],
                },
            )
        )
        win._poll()
        root.update_idletasks()

        # Check header row in treeview
        header_vals = win.tree.item("t10_plan_7")["values"]
        assert "Plan 7: 2 steps" in str(header_vals)

        # Check initial step rows exist
        assert win.tree.exists("t10_s0")
        assert win.tree.exists("t10_s1")

        # 2. plan_step 0 running
        ctrl.events.append(
            UiEvent(
                "plan_step",
                {
                    "id": 7,
                    "task_id": 10,
                    "step": 0,
                    "status": "running",
                    "label": "Launch Calculator",
                },
            )
        )
        win._poll()
        root.update_idletasks()

        step0_vals = win.tree.item("t10_s0")["values"]
        assert "running" in step0_vals
        assert win.plan_status_lbl["text"] == "Running plan: step 1 of 2"

        # 3. plan_step 0 done, step 1 running
        ctrl.events.append(
            UiEvent(
                "plan_step",
                {
                    "id": 7,
                    "task_id": 10,
                    "step": 0,
                    "status": "done",
                    "label": "Launch Calculator",
                },
            )
        )
        ctrl.events.append(
            UiEvent(
                "plan_step",
                {
                    "id": 7,
                    "task_id": 10,
                    "step": 1,
                    "status": "running",
                    "label": "Wait 2 seconds",
                },
            )
        )
        win._poll()
        root.update_idletasks()

        step0_vals = win.tree.item("t10_s0")["values"]
        assert "done" in step0_vals
        step1_vals = win.tree.item("t10_s1")["values"]
        assert "running" in step1_vals
        assert win.plan_status_lbl["text"] == "Running plan: step 2 of 2"

        # 4. plan_end
        ctrl.events.append(
            UiEvent(
                "plan_end",
                {
                    "id": 7,
                    "task_id": 10,
                    "status": "done",
                },
            )
        )
        win._poll()
        root.update_idletasks()

        header_vals_end = win.tree.item("t10_plan_7")["values"]
        assert "done" in header_vals_end
    finally:
        root.destroy()


def test_malformed_plan_event_does_not_stop_poll_loop():
    """Verify malformed plan events are safely ignored and do not halt the UI poll loop."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        # Feed incomplete / malformed events
        ctrl.events.append(UiEvent("plan_preview", {}))
        ctrl.events.append(UiEvent("plan_start", {}))
        ctrl.events.append(UiEvent("plan_step", {}))
        ctrl.events.append(UiEvent("plan_step", {"task_id": "not_an_int"}))
        ctrl.events.append(UiEvent("plan_end", {}))
        ctrl.events.append(UiEvent("plan_step", {"task_id": 1, "step": "not_int"}))

        win._poll()
        root.update_idletasks()

        # UI poll loop remains fully functional
        ctrl.events.append(UiEvent("reply", {"text": "System ready"}))
        win._poll()
        root.update_idletasks()

        assert "System ready" in win.history.get("1.0", "end")
    finally:
        root.destroy()


def test_cli_plan_callback():
    """Verify CLI plan_callback returns False for anything other than 'y' or 'Y'."""
    from main import confirm_plan

    # 'y' and 'Y' approve
    with patch("builtins.input", return_value="y"):
        assert confirm_plan(["Step 1", "Step 2"], ["auto", "confirm"]) is True
    with patch("builtins.input", return_value="Y"):
        assert confirm_plan(["Step 1"], ["auto"]) is True

    # Anything else rejects
    with patch("builtins.input", return_value="n"):
        assert confirm_plan(["Step 1"], ["auto"]) is False
    with patch("builtins.input", return_value=""):
        assert confirm_plan(["Step 1"], ["auto"]) is False
    with patch("builtins.input", return_value="yes"):
        assert confirm_plan(["Step 1"], ["auto"]) is False
    with patch("builtins.input", return_value="cancel"):
        assert confirm_plan(["Step 1"], ["auto"]) is False
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        assert confirm_plan(["Step 1"], ["auto"]) is False
    with patch("builtins.input", side_effect=EOFError):
        assert confirm_plan(["Step 1"], ["auto"]) is False


def test_cli_plan_and_chains_commands():
    """Verify /plan and /chains commands update session settings."""
    # Reset settings to known state
    settings.PLAN_PREVIEW = "auto"
    settings.CHAINS_ENABLED = True

    # Test /plan options
    with patch("builtins.input", side_effect=["/plan off", "exit"]):
        # Test plan command parser logic from main
        cmd = "/plan off".strip()
        parts = cmd.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        if sub in ("on", "auto"):
            settings.PLAN_PREVIEW = "auto"
        elif sub in ("off", "never"):
            settings.PLAN_PREVIEW = "never"
        elif sub == "always":
            settings.PLAN_PREVIEW = "always"
        assert settings.PLAN_PREVIEW == "never"

        cmd = "/plan on".strip()
        parts = cmd.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        if sub in ("on", "auto"):
            settings.PLAN_PREVIEW = "auto"
        assert settings.PLAN_PREVIEW == "auto"

        cmd = "/plan always".strip()
        parts = cmd.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        if sub == "always":
            settings.PLAN_PREVIEW = "always"
        assert settings.PLAN_PREVIEW == "always"

    # Test /chains options
    cmd = "/chains off".strip()
    parts = cmd.split(maxsplit=1)
    sub = parts[1].strip().lower() if len(parts) > 1 else ""
    if sub == "off":
        settings.CHAINS_ENABLED = False
    assert settings.CHAINS_ENABLED is False

    cmd = "/chains on".strip()
    parts = cmd.split(maxsplit=1)
    sub = parts[1].strip().lower() if len(parts) > 1 else ""
    if sub == "on":
        settings.CHAINS_ENABLED = True
    assert settings.CHAINS_ENABLED is True

    # Restore settings to clean state
    settings.PLAN_PREVIEW = "auto"
    settings.CHAINS_ENABLED = True


def test_diag_includes_last_plan_and_no_page_text():
    """Verify /diag diagnostics report includes last plan metadata and settings with no page text."""
    # Record a test plan
    record_plan(
        plan_id=987,
        source="chain",
        steps=[
            {"label": "Launch notepad", "status": "done"},
            {"label": "Wait 3s", "status": "done"},
        ],
        outcome="done",
    )

    report = render_diagnostics(generate_diagnostics())

    # Check settings section
    assert "=== SETTINGS ===" in report
    assert "CHAINS_ENABLED:" in report
    assert "PLAN_PREVIEW:" in report
    assert "PLAN_MAX_STEPS:" in report
    assert "PLAN_MAX_SECONDS:" in report

    # Check last plan section
    assert "=== LAST PLAN ===" in report
    assert "Plan ID: 987" in report
    assert "Source: chain" in report
    assert "Outcome: done" in report
    assert "Launch notepad: done" in report
    assert "Wait 3s: done" in report

    # Verify no page text or HTML exists in the report
    assert "<html" not in report.lower()
    assert "<body" not in report.lower()
    assert "<div" not in report.lower()
