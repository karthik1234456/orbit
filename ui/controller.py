"""UI controller bridging background agent execution to a user interface.

This module provides thread-safe orchestration between UI event loops
(such as Tkinter or web sockets) and ORBIT's conversational agent.
NOTE: This module must NOT import tkinter to remain UI-toolkit agnostic.
"""

from dataclasses import dataclass, field
import queue
import threading
from typing import Any, Optional

from config import settings
from core.llm import QuotaExceededError
from security.audit import redact_data


@dataclass
class UiEvent:
    """Event emitted by the controller for consumption by the UI thread."""
    kind: str
    data: dict = field(default_factory=dict)


@dataclass
class ConfirmRequest:
    """Structured request sent to the UI when a sensitive tool requires user approval.

    The arguments are automatically redacted and string values truncated to 200
    characters to prevent accidental credential leakage in UI logs and modals.
    """
    id: int
    tool: str
    args: dict
    warning: Optional[str]
    timeout_s: float

    def __post_init__(self) -> None:
        if self.args is not None:
            self.args = redact_data(self.args)
        else:
            self.args = {}


class ChatController:
    """Thread-safe controller managing background agent execution and UI events.

    Architectural and Security Rationale:
    1. Why the confirmation callback blocks the worker thread:
       The agent's conversational execution loop is synchronous and stepwise.
       When a tool requiring confirmation (PermissionLevel.CONFIRM) is invoked,
       the agent must pause at that exact step before any tool execution occurs.
       By blocking the background worker thread via a threading.Event, the UI
       thread remains completely free and responsive to handle rendering, user
       interactions, and dialog modals, while guaranteeing the agent cannot
       proceed without a verified user decision.

    2. Why every uncertain path is DENY:
       Security-first principle. If a confirmation times out, if the task is
       cancelled, if the application shuts down, or if an unexpected exception
       occurs while waiting, executing the requested tool could perform
       destructive, unauthorized, or irreversible system actions. Defaulting
       to DENY (False) on all uncertain, interrupted, or timed-out code paths
       ensures strict fail-closed safety.

    3. Why stale IDs are ignored:
       In an asynchronous user interface, user input events (such as clicking an
       approval button) can arrive after a confirmation has already timed out or
       after a subsequent tool call has issued a new request. If stale IDs were
       accepted, an approval intended for an earlier benign action could
       inadvertently authorize a completely different, dangerous action. Ignoring
       stale IDs guarantees exact 1-to-1 correlation between the user's explicit
       decision and the specific active tool step.
    """

    def __init__(self, confirm_timeout_s: float = 60.0) -> None:
        """Initialize controller with configurable confirmation timeout."""
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.app: Any = None
        self.cancel_event = threading.Event()

        self._events: queue.Queue[UiEvent] = queue.Queue()
        self._lock = threading.Lock()
        self._busy: bool = False
        self._shutdown: bool = False
        self._worker_thread: Optional[threading.Thread] = None

        self._request_counter: int = 0
        self._pending_confirmation: Optional[dict] = None
        self._pending_plan: Optional[dict] = None
        self._task_counter: int = 0
        self._current_task: int = 0
        self.voice_controller: Optional[Any] = None

    def attach(self, app: Any) -> None:
        """Attach the wired App instance to the controller."""
        self.app = app

    @property
    def is_busy(self) -> bool:
        """Return True if an agent task is currently running."""
        with self._lock:
            return self._busy

    @property
    def pending_tool(self) -> Optional[str]:
        """Return the tool name currently awaiting confirmation, if any."""
        with self._lock:
            if self._pending_confirmation is not None:
                return self._pending_confirmation.get("tool")
        return None

    def drain(self, max_items: int = 100) -> list[UiEvent]:
        """Perform non-blocking read of queued UiEvents for UI timer processing."""
        events: list[UiEvent] = []
        for _ in range(max_items):
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def _emit(self, kind: str, data: Optional[dict] = None, task: Optional[int] = None) -> None:
        """Enqueue a new UiEvent for the UI thread."""
        event_data = dict(data) if data is not None else {}
        if "task" not in event_data:
            if task is not None:
                event_data["task"] = task
            elif self._current_task:
                event_data["task"] = self._current_task
        self._events.put(UiEvent(kind=kind, data=event_data))

    def on_agent_event(self, event: dict) -> None:
        """Convert agent lifecycle events into UiEvents. Must never raise."""
        try:
            if not isinstance(event, dict):
                return
            etype = event.get("type")
            if etype in (
                "step_start",
                "step_end",
                "task_end",
                "auto_retrieval",
                "plan_start",
                "plan_step",
                "plan_end",
            ):
                data = dict(event)
                if "task" not in data and self._current_task:
                    data["task"] = self._current_task
                self._emit(etype, data)
        except Exception:
            pass

    def submit(self, text: str, source: str = "typed") -> bool:
        """Submit user text for execution on a background worker thread.

        Returns False if text is empty or controller is busy.
        """
        if text is None:
            return False
        clean_text = text.strip()
        if not clean_text:
            return False

        with self._lock:
            if self._shutdown or self._busy:
                if self._busy:
                    pending_tool = self._pending_confirmation.get("tool") if self._pending_confirmation else None
                    self._emit("busy", {
                        "pending_confirmation": self._pending_confirmation is not None,
                        "tool": pending_tool,
                    })
                return False

            self._busy = True
            self._task_counter += 1
            task_id = self._task_counter
            self._current_task = task_id
            self.cancel_event.clear()
            worker = threading.Thread(
                target=self._worker_loop,
                args=(clean_text, task_id, source),
                daemon=True,
                name="ChatControllerWorker",
            )
            self._worker_thread = worker
            worker.start()
            return True

    def _worker_loop(self, text: str, task_id: int, source: str = "typed") -> None:
        """Background worker thread executing the agent chat loop."""
        streamed_any = False

        def on_delta(delta: str) -> None:
            nonlocal streamed_any
            if delta:
                streamed_any = True
                self._emit("delta", {"delta": delta, "text": delta, "task": task_id})

        try:
            if self.app is None or not hasattr(self.app, "agent"):
                raise RuntimeError("ChatController has no attached App or Agent.")

            kwargs = {
                "on_delta": on_delta,
                "cancel_event": self.cancel_event,
                "source": source,
            }
            try:
                import inspect
                sig = inspect.signature(self.app.agent.chat)
                if "plan_callback" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                    kwargs["plan_callback"] = self.plan_callback
                if "source" not in sig.parameters and not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                    kwargs.pop("source", None)
            except Exception:
                kwargs["plan_callback"] = self.plan_callback

            final_text = self.app.agent.chat(text, **kwargs)

            had_denial = False
            agent = getattr(self.app, "agent", None)
            if agent and getattr(agent, "last_run", None):
                for step in getattr(agent.last_run, "steps", []):
                    if getattr(step, "status", "") == "denied" or "user denied permission" in str(getattr(step, "result", "")).lower():
                        had_denial = True
                        break
            if "user denied permission" in str(final_text).lower():
                had_denial = True

            if had_denial:
                final_text = "Okay, I didn't do that because you didn't approve it."

            self._emit("reply", {
                "text": final_text,
                "reply": final_text,
                "streamed": streamed_any,
                "task": task_id,
            })

            summary = ""
            if agent is not None and getattr(agent, "last_run", None) is not None:
                last_run = agent.last_run
                if hasattr(last_run, "summary") and callable(last_run.summary):
                    summary = last_run.summary() or ""

            self._emit("task_done", {
                "summary": summary,
                "task": task_id,
            })

        except QuotaExceededError:
            model_name = getattr(settings, "MODEL_NAME", "gemini-2.5-flash")
            msg = (
                f"Quota exceeded for model '{model_name}'. "
                f"Please wait a few moments before retrying, or switch MODEL_NAME in settings."
            )
            self._emit("error", {
                "error": msg,
                "message": msg,
                "model": model_name,
                "task": task_id,
            })

        except Exception as exc:
            msg = str(exc) if str(exc) else exc.__class__.__name__
            self._emit("error", {
                "error": msg,
                "message": msg,
                "task": task_id,
            })

        finally:
            with self._lock:
                self._busy = False
                self._worker_thread = None

    def confirm_callback(
        self, tool: str, args: dict, warning: Optional[str] = None
    ) -> bool:
        """Prompt user for confirmation from the worker thread.

        Blocks the worker thread for up to confirm_timeout_s. Returns False
        on timeout, cancel, shutdown, or user denial.
        """
        # Fail closed immediately if already cancelled or shutting down
        if self._shutdown or self.cancel_event.is_set():
            return False

        with self._lock:
            self._request_counter += 1
            req_id = self._request_counter
            confirm_event = threading.Event()
            pending = {
                "id": req_id,
                "tool": tool,
                "event": confirm_event,
                "allowed": False,
            }
            self._pending_confirmation = pending

        req = ConfirmRequest(
            id=req_id,
            tool=tool,
            args=args,
            warning=warning,
            timeout_s=self.confirm_timeout_s,
        )

        self._emit("confirm_request", {
            "request": req,
            "id": req_id,
            "tool": tool,
            "args": req.args,
            "warning": warning,
            "timeout_s": self.confirm_timeout_s,
            "task": self._current_task,
        })

        signaled = confirm_event.wait(timeout=self.confirm_timeout_s)

        with self._lock:
            if self._pending_confirmation is pending:
                self._pending_confirmation = None

        if not signaled:
            # Timed out waiting for confirmation
            return False

        if self.cancel_event.is_set() or self._shutdown:
            # Cancelled or shut down while waiting
            return False

        return bool(pending["allowed"])

    def plan_callback(self, labels: list[str], permissions: list[str]) -> bool:
        """Prompt user for plan preview and approval from the worker thread.

        Blocks the worker thread for up to confirm_timeout_s (default 60s). Returns False
        on timeout, cancel, shutdown, or user denial.
        """
        # Fail closed immediately if already cancelled or shutting down
        if self._shutdown or self.cancel_event.is_set():
            return False

        with self._lock:
            self._request_counter += 1
            req_id = self._request_counter
            plan_event = threading.Event()
            pending = {
                "id": req_id,
                "labels": list(labels),
                "permissions": list(permissions),
                "event": plan_event,
                "allowed": False,
            }
            self._pending_plan = pending

        self._emit("plan_preview", {
            "id": req_id,
            "labels": list(labels),
            "permissions": list(permissions),
            "timeout_s": self.confirm_timeout_s,
            "task": self._current_task,
        })

        signaled = plan_event.wait(timeout=self.confirm_timeout_s)

        with self._lock:
            if self._pending_plan is pending:
                self._pending_plan = None

        if not signaled:
            # Timed out waiting for plan confirmation
            return False

        if self.cancel_event.is_set() or self._shutdown:
            # Cancelled or shut down while waiting
            return False

        return bool(pending["allowed"])

    def resolve_confirmation(self, request_id: int, allowed: bool) -> None:
        """Resolve an active confirmation request from the UI thread.

        Stale answers (request_id not matching currently pending request)
        are ignored to prevent approving subsequent or different requests.
        """
        with self._lock:
            if self._pending_confirmation is None:
                return
            if self._pending_confirmation["id"] != request_id:
                return

            pending = self._pending_confirmation
            pending["allowed"] = bool(allowed)
            self._pending_confirmation = None

        pending["event"].set()

    def resolve_plan(self, plan_id: int, allowed: bool) -> None:
        """Resolve an active plan preview request from the UI thread.

        Stale answers (plan_id not matching currently pending request)
        are ignored to prevent approving subsequent or different requests.
        """
        with self._lock:
            if self._pending_plan is None:
                return
            if self._pending_plan["id"] != plan_id:
                return

            pending = self._pending_plan
            pending["allowed"] = bool(allowed)
            self._pending_plan = None

        pending["event"].set()

    def cancel(self) -> None:
        """Set the cancel event and deny any active confirmation or plan preview request."""
        self.cancel_event.set()
        pending = None
        pending_plan = None
        with self._lock:
            if self._pending_confirmation is not None:
                pending = self._pending_confirmation
                pending["allowed"] = False
                self._pending_confirmation = None
            if self._pending_plan is not None:
                pending_plan = self._pending_plan
                pending_plan["allowed"] = False
                self._pending_plan = None

        if pending is not None:
            pending["event"].set()
        if pending_plan is not None:
            pending_plan["event"].set()

        if self.voice_controller is not None:
            try:
                self.voice_controller.cancel()
            except Exception:
                pass

    def reset(self) -> None:
        """Reset agent conversation history and session state."""
        with self._lock:
            app = self.app
        if app is not None and hasattr(app, "agent") and app.agent is not None:
            if hasattr(app.agent, "reset"):
                app.agent.reset()

    def shutdown(self) -> None:
        """Cancel, deny pending confirmations, and join worker thread for up to 2s."""
        self._shutdown = True
        self.cancel()
        worker = None
        with self._lock:
            worker = self._worker_thread
        if worker is not None and worker.is_alive():
            if threading.current_thread() != worker:
                worker.join(timeout=2.0)
        with self._lock:
            app = self.app
        if app is not None and hasattr(app, "shutdown") and callable(app.shutdown):
            try:
                app.shutdown()
            except Exception:
                pass
