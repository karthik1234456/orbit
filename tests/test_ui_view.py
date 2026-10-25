"""View and widget tests for OrbitWindow and ConfirmDialog."""

import os
import sys
import time

# Ensure Tcl/Tk libraries are located reliably on Windows
if sys.platform == "win32":
    tcl_path = os.path.join(sys.base_prefix, "tcl", "tcl8.6")
    tk_path = os.path.join(sys.base_prefix, "tcl", "tk8.6")
    if os.path.exists(tcl_path):
        os.environ.setdefault("TCL_LIBRARY", tcl_path)
    if os.path.exists(tk_path):
        os.environ.setdefault("TK_LIBRARY", tk_path)

import tkinter as tk
from typing import Any, Optional
from unittest.mock import patch

import pytest

from ui.controller import ConfirmRequest, UiEvent
from ui.launch import make_provider


def make_test_root() -> tk.Tk:
    """Create a withdrawn Tk instance with retry to handle Windows file lock delays."""
    for _ in range(5):
        try:
            r = tk.Tk()
            r.withdraw()
            return r
        except Exception:
            time.sleep(0.05)
    r = tk.Tk()
    r.withdraw()
    return r


# Skip entire test module if Tkinter display is unavailable
try:
    _test_root = make_test_root()
    _test_root.destroy()
except Exception as exc:
    pytest.skip(f"Tkinter unavailable in environment: {exc}", allow_module_level=True)

from ui.app import OrbitWindow


class FakeController:
    """Mock ChatController for testing OrbitWindow UI interactions."""

    def __init__(self) -> None:
        self.submits: list[str] = []
        self.events: list[UiEvent] = []
        self.is_busy: bool = False
        self.cancel_called: bool = False
        self.shutdown_called: bool = False
        self.resolutions: list[tuple[int, bool]] = []
        self.app: Optional[Any] = None

    def submit(self, text: str) -> bool:
        self.submits.append(text)
        return True

    def cancel(self) -> None:
        self.cancel_called = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def resolve_confirmation(self, request_id: int, allowed: bool) -> None:
        self.resolutions.append((request_id, allowed))

    def drain(self, max_items: int = 100) -> list[UiEvent]:
        res = self.events[:max_items]
        self.events = self.events[max_items:]
        return res


def test_make_provider_mock_and_live():
    """Verify make_provider selects MockProvider or GeminiProvider appropriately."""
    with patch.dict(os.environ, {"ORBIT_MOCK": "1"}):
        provider, mode_label, notice = make_provider()
        assert mode_label == "MOCK"
        assert notice is not None
        assert "GEMINI_API_KEY" in notice

    with patch.dict(os.environ, {"ORBIT_MOCK": "0", "GEMINI_API_KEY": "fake_test_key"}):
        provider, mode_label, notice = make_provider()
        assert mode_label.startswith("LIVE ")
        assert notice is None


def test_send_text_calls_submit_and_shows_user_message():
    """Verify user submission calls controller.submit and records text in history."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        win.input_text.insert("1.0", "Hello Orbit assistant")
        win._on_send()

        assert ctrl.submits == ["Hello Orbit assistant"]
        chat_content = win.history.get("1.0", "end")
        assert "User: Hello Orbit assistant" in chat_content
        assert win.input_text.get("1.0", "end").strip() == ""
    finally:
        root.destroy()


def test_delta_then_reply_shows_text_exactly_once():
    """Verify streamed deltas followed by reply displays the final message once."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(UiEvent("delta", {"delta": "Hello "}))
        ctrl.events.append(UiEvent("delta", {"delta": "world!"}))
        ctrl.events.append(UiEvent("reply", {"text": "Hello world!", "streamed": True}))

        win._poll()
        root.update_idletasks()

        chat_content = win.history.get("1.0", "end")
        assert "ORBIT: Hello world!" in chat_content
        assert chat_content.count("Hello world!") == 1
    finally:
        root.destroy()


def test_non_streamed_reply_shows_text():
    """Verify reply event with streamed=False displays the text once."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(UiEvent("reply", {"text": "One-shot answer", "streamed": False}))
        win._poll()
        root.update_idletasks()

        chat_content = win.history.get("1.0", "end")
        assert "ORBIT: One-shot answer" in chat_content
        assert chat_content.count("One-shot answer") == 1
    finally:
        root.destroy()


def test_send_disabled_while_busy_and_stop_enabled():
    """Verify Send and input are disabled when busy, while Stop remains enabled."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.is_busy = True
        win._poll()
        root.update_idletasks()

        assert str(win.send_btn["state"]) == "disabled"
        assert str(win.input_text["state"]) == "disabled"
        assert str(win.stop_btn["state"]) == "normal"

        ctrl.is_busy = False
        win._poll()
        root.update_idletasks()

        assert str(win.send_btn["state"]) == "normal"
        assert str(win.input_text["state"]) == "normal"
        assert str(win.stop_btn["state"]) == "disabled"
    finally:
        root.destroy()


def test_stop_button_calls_cancel():
    """Verify clicking Stop triggers controller.cancel()."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")
        win._on_stop()
        assert ctrl.cancel_called is True
    finally:
        root.destroy()


def test_confirm_request_opens_dialog_allow_disabled_deny_focused():
    """Verify confirm_request opens modal where Allow is disabled and Deny is focused."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        req = ConfirmRequest(
            id=101,
            tool="format_drive",
            args={"drive": "D:", "password": "secret"},
            warning="Potential data loss",
            timeout_s=30.0,
        )
        ctrl.events.append(UiEvent("confirm_request", {"request": req, "id": 101}))

        win._poll()
        root.update_idletasks()

        assert win._confirm_dialog is not None
        dialog = win._confirm_dialog
        assert dialog.winfo_exists()

        # Allow starts disabled
        assert str(dialog.allow_btn["state"]) == "disabled"
        # Deny receives focus
        assert dialog.focus_get() == dialog.deny_btn
        dialog.destroy()
    finally:
        root.destroy()


def test_closing_dialog_resolves_false_exactly_once():
    """Verify closing dialog resolves with False exactly once."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        req = ConfirmRequest(
            id=102,
            tool="delete_folder",
            args={},
            warning=None,
            timeout_s=30.0,
        )
        ctrl.events.append(UiEvent("confirm_request", {"request": req, "id": 102}))

        win._poll()
        root.update_idletasks()

        dialog = win._confirm_dialog
        assert dialog is not None

        # Resolve via window close protocol
        dialog._resolve(False)
        root.update_idletasks()

        assert ctrl.resolutions == [(102, False)]

        # Subsequent resolution calls must be no-ops
        dialog._resolve(False)
        dialog._resolve(True)
        assert ctrl.resolutions == [(102, False)]
    finally:
        root.destroy()


def test_pressing_enter_in_dialog_does_not_resolve_with_true():
    """Verify Enter key in modal dialog never approves the confirmation request."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        req = ConfirmRequest(
            id=103,
            tool="execute_code",
            args={},
            warning="Dangerous execution",
            timeout_s=30.0,
        )
        ctrl.events.append(UiEvent("confirm_request", {"request": req, "id": 103}))

        win._poll()
        root.update_idletasks()

        dialog = win._confirm_dialog
        assert dialog is not None

        # Press Enter
        dialog._on_enter()
        root.update_idletasks()

        assert (103, True) not in ctrl.resolutions
        assert ctrl.resolutions == [(103, False)]
    finally:
        root.destroy()


def test_destroying_window_calls_controller_shutdown():
    """Verify destroying the Tk root window triggers controller.shutdown()."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        _win = OrbitWindow(root, ctrl, mode_label="MOCK")
        root.destroy()
        assert ctrl.shutdown_called is True
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_activity_tab_updates_on_step_events():
    """Verify step_start and step_end events update the Activity treeview."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "list_files"}))
        ctrl.events.append(
            UiEvent(
                "step_end",
                {"index": 1, "tool": "list_files", "status": "success", "result": "found 3 files"},
            )
        )

        win._poll()
        root.update_idletasks()

        children = win.activity_tree.get_children()
        assert len(children) == 1
        item_vals = win.activity_tree.item(children[0], "values")
        assert "1" in str(item_vals[0])
        assert "✓ list_files" in str(item_vals[1])
        assert "success" in str(item_vals[2])
    finally:
        root.destroy()


def test_error_event_shows_red_error_message():
    """Verify error events append error-tagged text in the chat history."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(UiEvent("error", {"message": "Simulated quota error"}))
        win._poll()
        root.update_idletasks()

        chat_content = win.history.get("1.0", "end")
        assert "Error: Simulated quota error" in chat_content
    finally:
        root.destroy()


def test_activity_tab_shows_auto_retrieval_passages():
    """Verify auto_retrieval event with injected > 0 adds a row in Activity tab."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(UiEvent("auto_retrieval", {"injected": 3, "withheld": 0}))
        win._poll()
        root.update_idletasks()

        children = win.activity_tree.get_children()
        assert len(children) == 1
        item_vals = win.activity_tree.item(children[0], "values")
        assert "Searched your documents: 3 passages" in str(item_vals[3])

        # If injected is 0, no additional item is inserted
        ctrl.events.append(UiEvent("auto_retrieval", {"injected": 0, "withheld": 1}))
        win._poll()
        root.update_idletasks()

        assert len(win.activity_tree.get_children()) == 1
    finally:
        root.destroy()


def test_migrated_doc_index_shows_notice(capsys):
    """Verify that if doc_index was migrated, a rebuild notice is printed and displayed in chat."""
    root = make_test_root()
    try:
        ctrl = FakeController()

        class FakeIndex:
            migrated = True

            def list_documents(self):
                return []

        class FakeApp:
            doc_index = FakeIndex()

        ctrl.app = FakeApp()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")
        root.update_idletasks()

        chat_content = win.history.get("1.0", "end")
        assert "Document index was rebuilt; run /index again." in chat_content
        captured = capsys.readouterr()
        assert "Document index was rebuilt; run /index again." in captured.out
    finally:
        root.destroy()


