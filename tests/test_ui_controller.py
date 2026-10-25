"""Tests for ChatController, UiEvent, and ConfirmRequest."""

import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional


from config import settings
from core.llm import QuotaExceededError
from ui.controller import ChatController, ConfirmRequest, UiEvent


class FakeRun:
    """Mock TaskRun providing summary string."""

    def __init__(self, summary_text: str = "✓ Step 1: tool (success) -> done"):
        self._summary_text = summary_text

    def summary(self) -> str:
        return self._summary_text


class FakeAgent:
    """Mock Agent with customizable streaming deltas, tool confirmation, and errors."""

    def __init__(
        self,
        deltas: Optional[list[str]] = None,
        reply: str = "Done",
        error: Optional[Exception] = None,
        confirm_fn: Optional[Callable[[], Any]] = None,
        block_event: Optional[threading.Event] = None,
        summary: str = "✓ Step 1: tool (success) -> done",
    ):
        self.deltas = deltas or []
        self.reply = reply
        self.error = error
        self.confirm_fn = confirm_fn
        self.block_event = block_event
        self.last_run = FakeRun(summary)

    def chat(
        self,
        text: str,
        on_delta: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        if self.block_event:
            self.block_event.wait()

        if self.error:
            raise self.error

        for d in self.deltas:
            if cancel_event and cancel_event.is_set():
                return "(interrupted)"
            if on_delta:
                on_delta(d)

        if self.confirm_fn:
            self.confirm_fn()

        if cancel_event and cancel_event.is_set():
            return "(interrupted)"

        return self.reply


class FakeApp:
    """Container holding a fake Agent."""

    def __init__(self, agent: FakeAgent):
        self.agent = agent


def wait_until_idle(controller: ChatController, timeout: float = 5.0) -> None:
    """Poll until controller is no longer busy."""
    deadline = time.time() + timeout
    while controller.is_busy and time.time() < deadline:
        time.sleep(0.01)
    if controller.is_busy:
        raise TimeoutError("ChatController remained busy beyond timeout.")


def drain_all(controller: ChatController, timeout: float = 5.0) -> list[UiEvent]:
    """Wait for controller to finish and drain all queued events."""
    wait_until_idle(controller, timeout=timeout)
    return controller.drain(max_items=1000)


def test_deltas_reply_and_task_done():
    """Verify that deltas arrive in order, followed by reply and task_done."""
    agent = FakeAgent(
        deltas=["Chunk 1", " - ", "Chunk 2"],
        reply="Final reply text",
        summary="✓ Step 1: read_file (success) -> content",
    )
    controller = ChatController()
    controller.attach(FakeApp(agent))

    assert controller.submit("test prompt") is True
    events = drain_all(controller)

    kinds = [e.kind for e in events]
    assert kinds == ["delta", "delta", "delta", "reply", "task_done"]

    assert events[0].data["delta"] == "Chunk 1"
    assert events[1].data["delta"] == " - "
    assert events[2].data["delta"] == "Chunk 2"

    reply_event = events[3]
    assert reply_event.data["text"] == "Final reply text"
    assert reply_event.data["streamed"] is True

    done_event = events[4]
    assert done_event.data["summary"] == "✓ Step 1: read_file (success) -> content"


def test_second_submit_while_busy_returns_false_and_emits_busy():
    """Verify submitting while busy returns False and emits 'busy' event."""
    block = threading.Event()
    agent = FakeAgent(block_event=block, reply="Finished")
    controller = ChatController()
    controller.attach(FakeApp(agent))

    assert controller.submit("first task") is True
    time.sleep(0.05)
    assert controller.is_busy is True

    # Second submit while first is executing
    assert controller.submit("second task") is False

    events = controller.drain()
    busy_events = [e for e in events if e.kind == "busy"]
    assert len(busy_events) == 1

    # Unblock and finish
    block.set()
    wait_until_idle(controller)
    assert controller.is_busy is False


def test_empty_submit_returns_false():
    """Verify empty or whitespace-only submissions return False."""
    controller = ChatController()
    controller.attach(FakeApp(FakeAgent()))

    assert controller.submit("") is False
    assert controller.submit("   ") is False
    assert controller.submit(None) is False  # type: ignore[arg-type]
    assert controller.drain() == []


def test_exception_becomes_error_event_and_subsequent_submit_works():
    """Verify unexpected exceptions emit 'error' event and reset busy state."""
    agent = FakeAgent(error=RuntimeError("Worker failure simulation"))
    controller = ChatController()
    controller.attach(FakeApp(agent))

    assert controller.submit("trigger failure") is True
    events = drain_all(controller)

    error_events = [e for e in events if e.kind == "error"]
    assert len(error_events) == 1
    assert "Worker failure simulation" in error_events[0].data["message"]
    assert controller.is_busy is False

    # Second submit should succeed
    agent.error = None
    agent.reply = "Healthy again"
    assert controller.submit("retry task") is True
    events2 = drain_all(controller)
    replies = [e for e in events2 if e.kind == "reply"]
    assert len(replies) == 1
    assert replies[0].data["text"] == "Healthy again"


def test_quota_exceeded_error_friendly_message():
    """Verify QuotaExceededError produces a helpful message with model name."""
    agent = FakeAgent(error=QuotaExceededError("Resource has been exhausted"))
    controller = ChatController()
    controller.attach(FakeApp(agent))

    assert controller.submit("ask model") is True
    events = drain_all(controller)

    error_events = [e for e in events if e.kind == "error"]
    assert len(error_events) == 1
    msg = error_events[0].data["message"]

    expected_model = getattr(settings, "MODEL_NAME", "gemini-2.5-flash")
    assert expected_model in msg
    assert "wait" in msg.lower()
    assert "model_name" in msg.lower()


def test_confirmation_allow_and_deny():
    """Verify confirmation: allow returns True, deny returns False."""
    controller = ChatController(confirm_timeout_s=5.0)

    # 1. Test allow
    allow_result = []

    def confirm_allow():
        res = controller.confirm_callback("delete_file", {"path": "doc.txt"})
        allow_result.append(res)

    agent_allow = FakeAgent(confirm_fn=confirm_allow)
    controller.attach(FakeApp(agent_allow))

    assert controller.submit("delete file") is True

    # Wait for confirm_request event
    req_event = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        for e in controller.drain():
            if e.kind == "confirm_request":
                req_event = e
                break
        if req_event:
            break
        time.sleep(0.01)

    assert req_event is not None
    req_id = req_event.data["id"]
    controller.resolve_confirmation(req_id, allowed=True)
    wait_until_idle(controller)
    assert allow_result == [True]

    # 2. Test deny
    deny_result = []

    def confirm_deny():
        res = controller.confirm_callback("delete_file", {"path": "doc.txt"})
        deny_result.append(res)

    agent_deny = FakeAgent(confirm_fn=confirm_deny)
    controller.attach(FakeApp(agent_deny))

    assert controller.submit("delete file again") is True

    req_event = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        for e in controller.drain():
            if e.kind == "confirm_request":
                req_event = e
                break
        if req_event:
            break
        time.sleep(0.01)

    assert req_event is not None
    req_id = req_event.data["id"]
    controller.resolve_confirmation(req_id, allowed=False)
    wait_until_idle(controller)
    assert deny_result == [False]


def test_confirmation_timeout_gives_false():
    """Verify confirmation times out and returns False with short timeout."""
    controller = ChatController(confirm_timeout_s=0.2)
    result = []

    def confirm_call():
        res = controller.confirm_callback("format_disk", {"drive": "C:"})
        result.append(res)

    agent = FakeAgent(confirm_fn=confirm_call)
    controller.attach(FakeApp(agent))

    assert controller.submit("format disk") is True
    # Do NOT resolve confirmation; wait for timeout
    wait_until_idle(controller, timeout=3.0)
    assert result == [False]


def test_cancel_while_waiting_gives_false():
    """Verify calling cancel() while waiting on confirmation aborts with False."""
    controller = ChatController(confirm_timeout_s=10.0)
    result = []

    def confirm_call():
        res = controller.confirm_callback("delete_all", {})
        result.append(res)

    agent = FakeAgent(confirm_fn=confirm_call)
    controller.attach(FakeApp(agent))

    assert controller.submit("delete everything") is True

    # Wait until confirmation request is enqueued
    req_event = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        for e in controller.drain():
            if e.kind == "confirm_request":
                req_event = e
                break
        if req_event:
            break
        time.sleep(0.01)

    assert req_event is not None
    # Cancel while worker is blocked
    controller.cancel()
    wait_until_idle(controller, timeout=3.0)
    assert result == [False]


def test_shutdown_while_waiting_gives_false():
    """Verify shutdown() unblocks pending confirmation and returns False."""
    controller = ChatController(confirm_timeout_s=10.0)
    result = []

    def confirm_call():
        res = controller.confirm_callback("launch_app", {"app": "calc"})
        result.append(res)

    agent = FakeAgent(confirm_fn=confirm_call)
    controller.attach(FakeApp(agent))

    assert controller.submit("launch app") is True

    req_event = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        for e in controller.drain():
            if e.kind == "confirm_request":
                req_event = e
                break
        if req_event:
            break
        time.sleep(0.01)

    assert req_event is not None
    controller.shutdown()
    assert result == [False]


def test_answering_request_1_after_request_2_started_does_not_approve_request_2():
    """Verify stale request IDs are ignored and cannot approve a subsequent request."""
    controller = ChatController(confirm_timeout_s=0.2)
    results = []

    def two_confirms():
        r1 = controller.confirm_callback("tool1", {"step": 1})
        results.append(("tool1", r1))
        r2 = controller.confirm_callback("tool2", {"step": 2})
        results.append(("tool2", r2))

    agent = FakeAgent(confirm_fn=two_confirms)
    controller.attach(FakeApp(agent))

    assert controller.submit("multistep confirmation") is True

    # Wait for first confirm_request
    req1 = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        for e in controller.drain():
            if e.kind == "confirm_request" and e.data["id"] == 1:
                req1 = e
                break
        if req1:
            break
        time.sleep(0.01)

    assert req1 is not None
    # Let request 1 time out or resolve it as denied
    controller.resolve_confirmation(1, allowed=False)

    # Now wait for second confirm_request (id=2)
    req2 = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        for e in controller.drain():
            if e.kind == "confirm_request" and e.data["id"] == 2:
                req2 = e
                break
        if req2:
            break
        time.sleep(0.01)

    assert req2 is not None

    # Attempt to approve request 1 while request 2 is active (stale answer!)
    controller.resolve_confirmation(1, allowed=True)

    # Request 2 should NOT be approved by answering request 1; wait for request 2 timeout
    wait_until_idle(controller, timeout=3.0)

    # Both requests must be False! Request 2 timed out without approval.
    assert results == [("tool1", False), ("tool2", False)]


def test_confirm_request_redacts_keys_and_truncates_strings():
    """Verify ConfirmRequest redacts sensitive keys and truncates long strings to 200 chars."""
    args = {
        "password": "super_secret_value",
        "api_key": "secret_key_12345",
        "long_text": "A" * 300,
        "normal_field": "safe",
    }
    req = ConfirmRequest(id=1, tool="test_tool", args=args, warning=None, timeout_s=60.0)

    assert req.args["password"] == "***"
    assert req.args["api_key"] == "***"
    assert len(req.args["long_text"]) == 200
    assert req.args["long_text"] == "A" * 200
    assert req.args["normal_field"] == "safe"


def test_worker_blocks_while_main_thread_continues():
    """Verify worker thread blocks inside confirm_callback while main thread executes."""
    controller = ChatController(confirm_timeout_s=5.0)
    worker_finished = threading.Event()

    def confirm_call():
        controller.confirm_callback("slow_tool", {})
        worker_finished.set()

    agent = FakeAgent(confirm_fn=confirm_call)
    controller.attach(FakeApp(agent))

    assert controller.submit("test blocking") is True

    # Wait until confirmation request is emitted
    req_event = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        for e in controller.drain():
            if e.kind == "confirm_request":
                req_event = e
                break
        if req_event:
            break
        time.sleep(0.01)

    assert req_event is not None
    # Worker must be busy and blocked, while main thread continues running
    assert controller.is_busy is True
    assert not worker_finished.is_set()

    # Main thread does independent operations while worker is blocked
    accumulator = 0
    for i in range(100):
        accumulator += i
    assert accumulator == 4950

    # Worker still blocked
    assert not worker_finished.is_set()

    # Now resolve
    controller.resolve_confirmation(req_event.data["id"], allowed=True)
    wait_until_idle(controller)
    assert worker_finished.is_set()


def test_on_agent_event_forwarding_and_safety():
    """Verify on_agent_event forwards lifecycle events and never raises."""
    controller = ChatController()

    controller.on_agent_event({"type": "step_start", "tool": "calc", "index": 1})
    controller.on_agent_event({"type": "step_end", "tool": "calc", "index": 1, "status": "success"})
    controller.on_agent_event({"type": "task_end", "status": "COMPLETED"})
    # Non-standard or malformed events must not raise
    controller.on_agent_event({"type": "custom_internal_type"})
    controller.on_agent_event("not_a_dict")  # type: ignore[arg-type]
    controller.on_agent_event(None)  # type: ignore[arg-type]

    events = controller.drain()
    kinds = [e.kind for e in events]
    assert kinds == ["step_start", "step_end", "task_end"]
    assert events[0].data["tool"] == "calc"
    assert events[1].data["status"] == "success"
    assert events[2].data["status"] == "COMPLETED"


def test_import_ui_controller_does_not_load_tkinter():
    """Verify importing ui.controller in a clean subprocess does not import tkinter."""
    code = "import sys, ui.controller; print('tkinter' in sys.modules)"
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == "False"

    code_pkg = "import sys, ui; print('tkinter' in sys.modules)"
    res_pkg = subprocess.run(
        [sys.executable, "-c", code_pkg],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res_pkg.stdout.strip() == "False"


def test_controller_events_carry_task_counter_and_increment():
    """Verify every event of a task carries the same task number and the next task gets the next number."""
    agent1 = FakeAgent(deltas=["A", "B"], reply="Reply 1")
    controller = ChatController()
    controller.attach(FakeApp(agent1))

    assert controller.submit("First task") is True
    controller.on_agent_event({"type": "step_start", "index": 1, "tool": "test_tool"})
    controller.on_agent_event({"type": "step_end", "index": 1, "tool": "test_tool", "status": "success", "result": "ok"})
    events1 = drain_all(controller)

    assert len(events1) > 0
    for e in events1:
        assert e.data.get("task") == 1, f"Event {e.kind} missing or has wrong task id: {e.data}"

    agent2 = FakeAgent(deltas=["C"], reply="Reply 2")
    controller.attach(FakeApp(agent2))
    assert controller.submit("Second task") is True
    controller.on_agent_event({"type": "step_start", "index": 1, "tool": "test_tool"})
    events2 = drain_all(controller)

    assert len(events2) > 0
    for e in events2:
        assert e.data.get("task") == 2, f"Event {e.kind} missing or has wrong task id: {e.data}"

