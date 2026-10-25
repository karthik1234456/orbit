"""View and widget tests for OrbitWindow and ConfirmDialog."""

import os
import sys

# Ensure Tcl/Tk libraries are located reliably on Windows
if sys.platform == "win32":
    tcl_path = os.path.join(sys.base_prefix, "tcl", "tcl8.6")
    tk_path = os.path.join(sys.base_prefix, "tcl", "tk8.6")
    if os.path.exists(tcl_path):
        os.environ.setdefault("TCL_LIBRARY", tcl_path)
    if os.path.exists(tk_path):
        os.environ.setdefault("TK_LIBRARY", tk_path)

from typing import Any, Optional
from unittest.mock import patch


from ui.controller import ConfirmRequest, UiEvent
from ui.launch import make_provider


from tests.conftest import check_tk_available, make_test_root

# Skip entire test module if Tkinter display is unavailable
# Note: TclError failures (e.g. "Can't find a usable init.tcl") are intermittent and environmental
check_tk_available()

from ui.app import OrbitWindow


class FakeController:
    """Mock ChatController for testing OrbitWindow UI interactions."""

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
        if self.app is not None and hasattr(self.app, "agent") and self.app.agent is not None:
            if hasattr(self.app.agent, "reset"):
                self.app.agent.reset()

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
        assert "1" in str(item_vals[1])
        assert "✓ list_files" in str(item_vals[2])
        assert "success" in str(item_vals[3])
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
        assert "Searched your documents: 3 passages" in str(item_vals[4])

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


def test_two_consecutive_tasks_with_same_step_index_produce_two_rows():
    """Verify two consecutive tasks starting steps at idx 1 produce separate rows without TclError."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        # Task 1
        ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "search", "task": 1}))
        ctrl.events.append(UiEvent("step_end", {"index": 1, "tool": "search", "status": "success", "result": "found", "task": 1}))
        ctrl.events.append(UiEvent("task_done", {"task": 1}))

        # Task 2 also starts at index 1
        ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "fetch", "task": 2}))
        ctrl.events.append(UiEvent("step_end", {"index": 1, "tool": "fetch", "status": "success", "result": "fetched", "task": 2}))
        ctrl.events.append(UiEvent("task_done", {"task": 2}))

        win._poll()
        root.update_idletasks()

        children = win.activity_tree.get_children()
        assert len(children) == 3
        assert "t1_s1" in children
        assert "sep_t2" in children
        assert "t2_s1" in children

        row1 = win.activity_tree.item("t1_s1", "values")
        row2 = win.activity_tree.item("t2_s1", "values")
        assert "1" in str(row1[0])
        assert "search" in str(row1[2])
        assert "2" in str(row2[0])
        assert "fetch" in str(row2[2])
    finally:
        root.destroy()


def test_handler_error_on_malformed_event_reschedules_poll_and_processes_later_events(capsys):
    """Verify a handler error on a malformed event prints traceback to stderr, shows UI error once, and does not stop later events."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        class BadEvent:
            kind = "step_start"
            @property
            def data(self):
                raise ValueError("Simulated malformed event")

        good_event = UiEvent("reply", {"text": "Good event text", "task": 1})
        ctrl.events.append(BadEvent())  # type: ignore[arg-type]
        ctrl.events.append(good_event)

        with patch.object(root, "after", wraps=root.after) as mock_after:
            win._poll()
            root.update_idletasks()
            assert any(call.args and call.args[0] == 50 and call.args[1] == win._poll for call in mock_after.call_args_list)

        chat_content = win.history.get("1.0", "end")
        assert "ORBIT: Good event text" in chat_content
        assert "UI error: Simulated malformed event" in chat_content

        captured = capsys.readouterr()
        assert "Simulated malformed event" in captured.err
    finally:
        root.destroy()


def test_busy_state_follows_controller_is_busy_after_handler_error():
    """Verify busy UI state follows controller.is_busy even after an event handler error."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        class BadEvent:
            kind = "error"
            @property
            def data(self):
                raise RuntimeError("Handler failure")

        ctrl.is_busy = True
        ctrl.events.append(BadEvent())  # type: ignore[arg-type]

        win._poll()
        root.update_idletasks()

        assert str(win.send_btn["state"]) == "disabled"
        assert str(win.stop_btn["state"]) == "normal"
        assert win.busy_lbl.cget("text") == "● Busy"

        ctrl.is_busy = False
        win._poll()
        root.update_idletasks()

        assert str(win.send_btn["state"]) == "normal"
        assert str(win.stop_btn["state"]) == "disabled"
        assert win.busy_lbl.cget("text") == "● Idle"
    finally:
        root.destroy()


def test_confirm_dialog_topmost_allow_disabled_and_single_deny_resolution():
    """Verify confirm dialog sets -topmost, starts with Allow disabled, and closing resolves to deny once."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        req = ConfirmRequest(
            id=201,
            tool="danger_tool",
            args={},
            warning="Danger",
            timeout_s=30.0,
        )
        ctrl.events.append(UiEvent("confirm_request", {"request": req, "id": 201}))

        win._poll()
        root.update_idletasks()

        dialog = win._confirm_dialog
        assert dialog is not None
        assert dialog.winfo_exists()
        assert dialog.attributes("-topmost") in (1, True)
        assert str(dialog.allow_btn["state"]) == "disabled"

        dialog.destroy()
        root.update_idletasks()

        assert ctrl.resolutions == [(201, False)]

        dialog._resolve(False)
        dialog._resolve(True)
        assert ctrl.resolutions == [(201, False)]
    finally:
        root.destroy()


def test_submitting_during_pending_confirmation_gives_waiting_message():
    """Verify submitting while a confirmation is pending shows waiting for approval message and brings dialog to front."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        req = ConfirmRequest(
            id=301,
            tool="dangerous_rm",
            args={},
            warning="Warning",
            timeout_s=30.0,
        )
        ctrl.events.append(UiEvent("confirm_request", {"request": req, "id": 301}))
        win._poll()
        root.update_idletasks()

        ctrl.is_busy = True
        ctrl.pending_tool = "dangerous_rm"

        win.input_text.insert("1.0", "New task while pending")
        win._on_send()
        root.update_idletasks()

        chat_content = win.history.get("1.0", "end")
        assert "ORBIT is still working on your previous request" in chat_content
        assert "waiting for your approval" in chat_content.lower()
        assert "dangerous_rm" in chat_content
    finally:
        root.destroy()


def test_denial_reply_is_friendly_while_activity_row_keeps_raw_error():
    """Verify user denial produces friendly chat text while Activity tree keeps the raw error."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "exec_cmd", "task": 1}))
        raw_error = "ERROR: user denied permission for exec_cmd"
        ctrl.events.append(
            UiEvent(
                "step_end",
                {"index": 1, "tool": "exec_cmd", "status": "denied", "result": raw_error, "task": 1},
            )
        )
        ctrl.events.append(UiEvent("reply", {"text": raw_error, "task": 1}))
        ctrl.events.append(UiEvent("task_done", {"task": 1}))

        win._poll()
        root.update_idletasks()

        chat_content = win.history.get("1.0", "end")
        assert "Okay, I didn't do that because you didn't approve it." in chat_content
        assert raw_error not in chat_content

        row = win.activity_tree.item("t1_s1", "values")
        assert row[3] == "denied"
        assert raw_error in row[4]
    finally:
        root.destroy()


def test_stress_100_scripted_tasks_with_random_event_orders():
    """Stress test of 100 scripted tasks with random event orders leaves consistent rows and idle window."""
    import random
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        rng = random.Random(42)

        for task_id in range(1, 101):
            pattern = rng.choice(["normal", "missing_step_end", "duplicated_idx", "error_mid_task", "task_done_no_steps"])
            if pattern == "normal":
                ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "tool_a", "task": task_id}))
                ctrl.events.append(UiEvent("step_end", {"index": 1, "tool": "tool_a", "status": "success", "result": "ok", "task": task_id}))
                ctrl.events.append(UiEvent("task_done", {"task": task_id}))
            elif pattern == "missing_step_end":
                ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "tool_b", "task": task_id}))
                ctrl.events.append(UiEvent("task_done", {"task": task_id}))
            elif pattern == "duplicated_idx":
                ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "tool_c", "task": task_id}))
                ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "tool_c", "task": task_id}))
                ctrl.events.append(UiEvent("step_end", {"index": 1, "tool": "tool_c", "status": "success", "result": "ok", "task": task_id}))
                ctrl.events.append(UiEvent("task_done", {"task": task_id}))
            elif pattern == "error_mid_task":
                ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "tool_d", "task": task_id}))
                ctrl.events.append(UiEvent("error", {"message": "Crash mid-task", "task": task_id}))
            elif pattern == "task_done_no_steps":
                ctrl.events.append(UiEvent("task_done", {"task": task_id}))

            if len(ctrl.events) >= 10:
                win._poll()

        while ctrl.events:
            win._poll()

        ctrl.is_busy = False
        win._poll()
        root.update_idletasks()

        children = win.activity_tree.get_children()
        for child in children:
            vals = win.activity_tree.item(child, "values")
            assert vals[3] != "running...", f"Row {child} stuck in running...: {vals}"

        assert win.busy_lbl.cget("text") == "● Idle"
        assert str(win.send_btn["state"]) == "normal"
        assert str(win.stop_btn["state"]) == "disabled"
    finally:
        root.destroy()


def test_context_tab_displays_state_and_one_line_status():
    """Verify Context tab and status bar label render session state correctly."""
    from core.session_state import SessionState

    root = make_test_root()
    try:
        ctrl = FakeController()
        state = SessionState()
        state.update_from_tool("search_web", {"query": "free fire videos"}, "success")
        state.last_site = "youtube"

        class DummyAgent:
            def __init__(self, s):
                self.session_state = s

        class DummyApp:
            def __init__(self, ag):
                self.agent = ag

        ctrl.app = DummyApp(DummyAgent(state))
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        win._poll()
        root.update_idletasks()

        assert win.context_vars["site"].get() == "youtube"
        assert win.context_vars["query"].get() == "free fire videos"
        assert win.context_lbl.winfo_manager() == "pack"
        assert win.context_lbl.cget("text") == "Context: youtube · 'free fire videos'"

        # Clear state
        state.clear()
        win._poll()
        root.update_idletasks()

        assert win.context_vars["site"].get() == "—"
        assert win.context_vars["query"].get() == "—"
        assert win.context_lbl.winfo_manager() == ""
    finally:
        root.destroy()


def test_new_conversation_clears_history_state_and_tab():
    """Verify New conversation button prompts for confirmation and clears history and state."""
    from core.session_state import SessionState
    from unittest.mock import patch

    root = make_test_root()
    try:
        ctrl = FakeController()
        state = SessionState()
        state.update_from_tool("open_application", {"name": "notepad"}, "success")

        class DummyAgent:
            def __init__(self, s):
                self.session_state = s
                self.history = ["some prior turn"]

            def reset(self):
                self.history = []
                self.session_state.clear()

        agent = DummyAgent(state)

        class DummyApp:
            def __init__(self, ag):
                self.agent = ag

        ctrl.app = DummyApp(agent)
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        win._append_chat("User: Hello\n\n", ("user",))
        win._poll()
        root.update_idletasks()

        assert "Hello" in win.history.get("1.0", "end")
        assert win.context_vars["app"].get() == "notepad"

        # User denies confirmation -> nothing cleared
        with patch("tkinter.messagebox.askyesno", return_value=False):
            win._on_new_conversation()
        assert "Hello" in win.history.get("1.0", "end")
        assert win.context_vars["app"].get() == "notepad"
        assert len(agent.history) == 1

        # User confirms -> cleared
        with patch("tkinter.messagebox.askyesno", return_value=True):
            win._on_new_conversation()

        assert win.history.get("1.0", "end").strip() == ""
        assert win.context_vars["app"].get() == "—"
        assert state.is_empty()
        assert len(agent.history) == 0
    finally:
        root.destroy()


def test_activity_tab_shows_task_numbers_and_separator_between_tasks():
    """Verify Activity tab displays task column numbers and a separator row between tasks."""
    root = make_test_root()
    try:
        ctrl = FakeController()
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        # Task 1
        ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "search", "task": 1}))
        ctrl.events.append(UiEvent("step_end", {"index": 1, "tool": "search", "status": "success", "task": 1}))
        ctrl.events.append(UiEvent("task_done", {"task": 1}))

        # Task 2
        ctrl.events.append(UiEvent("step_start", {"index": 1, "tool": "open_app", "task": 2}))
        ctrl.events.append(UiEvent("step_end", {"index": 1, "tool": "open_app", "status": "success", "task": 2}))
        ctrl.events.append(UiEvent("task_done", {"task": 2}))

        win._poll()
        root.update_idletasks()

        children = win.activity_tree.get_children()
        assert "t1_s1" in children
        assert "sep_t2" in children
        assert "t2_s1" in children

        # Row 1 has Task=1
        r1 = win.activity_tree.item("t1_s1", "values")
        assert str(r1[0]) == "1"

        # Separator row
        sep = win.activity_tree.item("sep_t2", "values")
        assert sep[0] == "—"

        # Row 2 has Task=2
        r2 = win.activity_tree.item("t2_s1", "values")
        assert str(r2[0]) == "2"
    finally:
        root.destroy()




