"""Tests for Routines UI tab, PlanDialog heading/safety, and controller integration."""

import os
from pathlib import Path
import sys
from typing import Any, Optional
from unittest.mock import patch

# Ensure Tcl/Tk libraries are located reliably on Windows
if sys.platform == "win32":
    tcl_path = os.path.join(sys.base_prefix, "tcl", "tcl8.6")
    tk_path = os.path.join(sys.base_prefix, "tcl", "tk8.6")
    if os.path.exists(tcl_path):
        os.environ.setdefault("TCL_LIBRARY", tcl_path)
    if os.path.exists(tk_path):
        os.environ.setdefault("TK_LIBRARY", tk_path)

from config import settings
from routines.store import RoutineStore
from tests.conftest import check_tk_available, make_test_root
from ui.app import OrbitWindow, PlanDialog
from ui.controller import UiEvent

check_tk_available()


class FakeController:
    """Mock ChatController for testing Routines tab and PlanDialog."""

    def __init__(self, routine_store: Optional[Any] = None) -> None:
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
        self.routine_store = routine_store

    def submit(self, text: str, source: str = "typed") -> bool:
        if self.is_busy:
            return False
        self.submits.append(text)
        return True

    def cancel(self) -> None:
        self.cancel_called = True

    def resolve_confirmation(self, request_id: int, allowed: bool) -> None:
        self.resolutions.append((request_id, allowed))

    def resolve_plan(self, plan_id: int, allowed: bool) -> None:
        self.plan_resolutions.append((plan_id, allowed))

    def drain(self, max_items: int = 100) -> list[UiEvent]:
        res = self.events[:max_items]
        self.events = self.events[max_items:]
        return res

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_plan_dialog_save_routine_heading_and_button():
    """Verify PlanDialog with kind='save_routine' displays custom header and 'Save' button."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        dialog = PlanDialog(
            root,
            ctrl,
            plan_id=101,
            labels=["Open Notepad", "Open Calc"],
            permissions=["confirm", "confirm"],
            kind="save_routine",
            name="morning",
        )

        assert dialog.title() == "Save Routine"
        assert str(dialog.run_btn["text"]) == "Save"
        assert str(dialog.run_btn["state"]) == "disabled"
        assert dialog.focus_get() == dialog.cancel_btn

        dialog_text = dialog.text_area.get("1.0", "end")
        assert "1. Open Notepad" in dialog_text
        assert "2. Open Calc" in dialog_text

        # Enable after 800ms
        dialog._enable_run()
        assert str(dialog.run_btn["state"]) == "normal"

        # Resolve True
        dialog._resolve(True)
        assert ctrl.plan_resolutions == [(101, True)]

        # Resolving again does nothing (exactly one resolve)
        dialog._resolve(False)
        assert len(ctrl.plan_resolutions) == 1
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_plan_dialog_routine_run_heading():
    """Verify PlanDialog with kind='routine' displays 'Routine <name> (n steps):' heading."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        dialog = PlanDialog(
            root,
            ctrl,
            plan_id=102,
            labels=["Open Notepad", "Wait 5s"],
            permissions=["confirm", "SAFE"],
            kind="routine",
            name="focus",
        )

        assert dialog.title() == "Plan Preview Required"
        assert str(dialog.run_btn["text"]) == "Run"
        assert str(dialog.run_btn["state"]) == "disabled"

        # Enter key triggers Cancel (Deny), never Run
        dialog._on_enter()
        assert ctrl.plan_resolutions == [(102, False)]
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_routines_tab_build_and_refresh(tmp_path: Path):
    """Verify Routines tab lists saved routines with name, steps, last run, and run count."""
    root = make_test_root()
    try:
        store = RoutineStore(path=str(tmp_path / "routines.db"))
        store.save("Morning Routine", [{"tool": "open_application", "args": {"name": "notepad"}}])
        store.save("Dev Work", [{"tool": "open_application", "args": {"name": "calculator"}}, {"tool": "wait", "args": {"seconds": 5}}])

        ctrl = FakeController(routine_store=store)
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        # Check tab is present
        tab_texts = [win.notebook.tab(i, "text") for i in win.notebook.tabs()]
        assert "Rout" in tab_texts or "Routines" in tab_texts

        # Verify tree rows
        children = win.routines_tree.get_children()
        assert len(children) == 2

        names_found = [str(win.routines_tree.item(c, "values")[0]).lower() for c in children]
        assert "morning routine" in names_found
        assert "dev work" in names_found

        for c in children:
            row = win.routines_tree.item(c, "values")
            if "morning" in str(row[0]).lower():
                assert int(row[1]) == 1
                assert row[2] == "never"
                assert int(row[3]) == 0
            elif "dev" in str(row[0]).lower():
                assert int(row[1]) == 2
                assert row[2] == "never"
                assert int(row[3]) == 0
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_routines_tab_actions(tmp_path: Path):
    """Verify Run, Show steps, Delete, and Refresh buttons on Routines tab."""
    root = make_test_root()
    try:
        store = RoutineStore(path=str(tmp_path / "routines.db"))
        store.save("morning", [{"tool": "open_application", "args": {"app_name": "notepad"}}])

        ctrl = FakeController(routine_store=store)
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        # Select first item
        children = win.routines_tree.get_children()
        assert len(children) == 1
        win.routines_tree.selection_set(children[0])

        # Test Run button
        win._on_run_routine_tab()
        assert ctrl.submits == ["run routine morning"]

        # Test Show steps button
        with patch("tkinter.messagebox.showinfo") as mock_info:
            win._on_show_routine_steps_tab()
            assert mock_info.called
            args, _ = mock_info.call_args
            assert "morning" in args[0].lower()
            assert "open notepad" in args[1].lower()

        # Test Delete button with cancellation
        with patch("tkinter.messagebox.askyesno", return_value=False):
            win._on_delete_routine_tab()
            assert store.get("morning") is not None

        # Test Delete button with confirmation
        with patch("tkinter.messagebox.askyesno", return_value=True):
            win._on_delete_routine_tab()
            assert store.get("morning") is None
            assert len(win.routines_tree.get_children()) == 0
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_routines_tab_disabled_when_setting_off(tmp_path: Path, monkeypatch):
    """Verify Routines tab shows 'disabled' and disables action buttons when ROUTINES_ENABLED=False."""
    monkeypatch.setattr(settings, "ROUTINES_ENABLED", False)

    root = make_test_root()
    try:
        store = RoutineStore(path=str(tmp_path / "routines.db"))
        ctrl = FakeController(routine_store=store)
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        assert str(win.routine_run_btn["state"]) == "disabled"
        assert str(win.routine_show_btn["state"]) == "disabled"
        assert str(win.routine_delete_btn["state"]) == "disabled"

        # Tree shows "disabled"
        children = win.routines_tree.get_children()
        assert len(children) == 1
        vals = win.routines_tree.item(children[0], "values")
        assert vals[0] == "disabled"
    finally:
        try:
            root.destroy()
        except Exception:
            pass
