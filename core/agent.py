"""Agent implementation managing conversational state, tool calling, permissions, guards, and audit logging."""

import os
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Union

if TYPE_CHECKING:
    from core.planner import Plan

from config import settings
from core.guards import LoopGuard
from core.llm import LLMProvider
from core.session_state import SessionState
from core.task import RunLogger, RunStatus, StepRecord, TaskRun
from security.audit import AuditLogger
from security.permissions import Decision, PermissionPolicy
from tools.registry import ToolRegistry

COMMAND_SHORTCUTS: bool = getattr(settings, "COMMAND_SHORTCUTS", True)
SESSION_CONTEXT: bool = getattr(settings, "SESSION_CONTEXT", True)

TAINT_WARNING = (
    "This action was requested after ORBIT read untrusted content (documents or files). "
    "Make sure YOU asked for it."
)

_RECOVERY_HINTS = {
    "denied": "Do not retry this action. Tell the user it was not approved and ask what they want instead.",
    "blocked": "This action is not allowed. Do not retry. Suggest an alternative if one exists.",
    "rejected": "This content was rejected on purpose by a safety rule. Do not retry this tool with reworded or modified content. Tell the user it was not saved and why.",
    "error": "The tool failed. Read the error, fix the arguments or choose a different approach. Do not repeat the identical call.",
}


class Agent:
    """Conversational agent that manages dialog state, permissions, tool executions, and audit logging."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: Optional[ToolRegistry] = None,
        system_prompt: str = "You are ORBIT, a helpful desktop assistant.",
        max_steps: int = 5,
        policy: Optional[PermissionPolicy] = None,
        audit: Optional[AuditLogger] = None,
        confirm_callback: Optional[Callable[[str, dict], bool]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        run_logger: Optional[RunLogger] = None,
        max_tool_calls: Optional[int] = None,
        max_consecutive_errors: Optional[int] = None,
        max_task_seconds: Optional[float] = None,
        memory_store: Optional[Any] = None,
        limits: Optional[Any] = None,
        retriever: Optional[Callable[[str], Any]] = None,
        command_shortcuts: Optional[bool] = None,
        session_context: Optional[bool] = None,
        session_state: Optional[SessionState] = None,
        allowed_apps: Optional[Mapping[str, Any]] = None,
        sites: Optional[Mapping[str, Any]] = None,
        browser_controller: Optional[Any] = None,
    ):
        """Initialize agent with LLM provider, tool registry, permission policy, guards, and audit logger."""
        self.provider = provider
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.policy = policy
        self.audit = audit
        # Default confirmation callback safely denies any tool requiring permission
        self.confirm_callback = confirm_callback or (lambda tool_name, args, warning=None: False)
        self.on_event = on_event
        self.run_logger = run_logger or RunLogger()
        self.memory_store = memory_store
        self.retriever = retriever
        self.command_shortcuts = command_shortcuts
        self.session_context = session_context
        self.session_state = session_state if session_state is not None else SessionState()
        self.browser_controller = browser_controller

        if allowed_apps is None:
            from config.apps import ALLOWED_APPS
            self.allowed_apps = ALLOWED_APPS
        else:
            self.allowed_apps = allowed_apps

        if sites is None:
            from config.sites import SITES
            self.sites = SITES
        else:
            self.sites = sites

        if limits is not None:
            if hasattr(limits, "max_tool_calls") and max_tool_calls is None:
                max_tool_calls = limits.max_tool_calls
            if hasattr(limits, "max_consecutive_errors") and max_consecutive_errors is None:
                max_consecutive_errors = limits.max_consecutive_errors
            if hasattr(limits, "max_seconds") and max_task_seconds is None:
                max_task_seconds = limits.max_seconds

        self.max_tool_calls = max_tool_calls if max_tool_calls is not None else settings.MAX_TOOL_CALLS
        self.max_consecutive_errors = (
            max_consecutive_errors if max_consecutive_errors is not None else settings.MAX_CONSECUTIVE_ERRORS
        )
        self.max_task_seconds = (
            max_task_seconds if max_task_seconds is not None else settings.MAX_TASK_SECONDS
        )

        self.history: list[dict] = []
        self.last_tool_events: list[tuple[str, dict, str]] = []
        self.last_run: Optional[TaskRun] = None
        self._rejected_tools: set[str] = set()

    def shortcuts_enabled(self) -> bool:
        """Check whether deterministic command shortcuts are active."""
        if self.command_shortcuts is not None:
            return bool(self.command_shortcuts)
        env_val = os.getenv("COMMAND_SHORTCUTS")
        if env_val is not None:
            v = env_val.strip().lower()
            if v in ("0", "false", "no", "off"):
                return False
            if v in ("1", "true", "yes", "on"):
                return True
        return bool(getattr(settings, "COMMAND_SHORTCUTS", True))

    def session_context_enabled(self) -> bool:
        """Check whether session context tracking is active."""
        if self.session_context is not None:
            return bool(self.session_context)
        env_val = os.getenv("SESSION_CONTEXT")
        if env_val is not None:
            v = env_val.strip().lower()
            if v in ("0", "false", "no", "off"):
                return False
            if v in ("1", "true", "yes", "on"):
                return True
        return bool(getattr(settings, "SESSION_CONTEXT", True))

    def _execute_single_tool(
        self,
        name: str,
        args: dict,
        guard: LoopGuard,
        run: TaskRun,
        tainted: bool,
        is_shortcut: bool = False,
    ) -> tuple[Optional[RunStatus], str, str, str, Optional[str], bool]:
        """Execute a single tool call through the full validation and execution pipeline.

        Returns: (stop_reason, status, outcome, result, hint, updated_tainted)
        """
        step_index = len(run.steps) + 1

        # 1. Pre-execution guard check
        stop_reason = guard.check_before(name, args)
        if stop_reason is not None:
            return stop_reason, "blocked", "blocked", "ERROR: not executed (task stopped)", None, tainted

        self._emit_event({
            "type": "step_start",
            "tool": name,
            "status": "start",
            "index": step_index,
            "args": args,
        })

        start_time = time.perf_counter()

        tool = self.registry.get(name) if self.registry else None
        tool_level = tool.permission if tool else None

        if name in self._rejected_tools:
            result = f"ERROR: BLOCKED: an earlier call to {name} was rejected in this turn; not retrying."
            status = "blocked"
            outcome = "blocked"
            log_decision = "rejected_retry"
            hint = _RECOVERY_HINTS["blocked"]
        else:
            # Determine security decision if policy is configured
            if self.policy is not None:
                decision = self.policy.decide(name, tool_level)
            else:
                decision = Decision.ALLOW

            # Execute or prompt based on policy decision
            if decision == Decision.ALLOW:
                if self.registry:
                    result = self.registry.execute(name, args)
                else:
                    result = f"ERROR: No tool registry configured to execute '{name}'"
                log_decision = "allow"

                if result.startswith("ERROR: REJECTED:"):
                    status = "rejected"
                    outcome = "rejected"
                    hint = _RECOVERY_HINTS["rejected"]
                    self._rejected_tools.add(name)
                elif result.startswith("ERROR"):
                    status = "error"
                    outcome = "error"
                    hint = _RECOVERY_HINTS["error"]
                else:
                    status = "success"
                    outcome = "success"
                    hint = None

            elif decision == Decision.ASK:
                # Precheck phase: run registry argument validation and tool precheck before asking user
                precheck_err = None
                if self.registry:
                    precheck_err = self.registry.validate_arguments(name, args)

                if precheck_err is None and tool and tool.precheck is not None:
                    try:
                        pre_res = tool.precheck(args)
                        if pre_res is not None:
                            precheck_err = str(pre_res)
                    except Exception as exc:
                        precheck_err = f"ERROR: Tool '{name}' precheck failed: {exc}"

                if precheck_err is not None:
                    if not precheck_err.startswith("ERROR"):
                        precheck_err = f"ERROR: {precheck_err}"
                    result = precheck_err
                    if result.startswith("ERROR: REJECTED:"):
                        status = "rejected"
                        outcome = "rejected"
                        hint = _RECOVERY_HINTS["rejected"]
                        self._rejected_tools.add(name)
                    else:
                        status = "blocked"
                        outcome = "blocked"
                        log_decision = "precheck_failed"
                        hint = _RECOVERY_HINTS["blocked"]
                else:
                    warning = TAINT_WARNING if tainted else None
                    confirmed = self._ask_confirmation(name, args, warning=warning)
                    if confirmed:
                        if self.registry:
                            result = self.registry.execute(name, args)
                        else:
                            result = f"ERROR: No tool registry configured to execute '{name}'"
                        log_decision = "user_approved"

                        if result.startswith("ERROR: REJECTED:"):
                            status = "rejected"
                            outcome = "rejected"
                            hint = _RECOVERY_HINTS["rejected"]
                            self._rejected_tools.add(name)
                        elif result.startswith("ERROR"):
                            status = "error"
                            outcome = "error"
                            hint = _RECOVERY_HINTS["error"]
                        else:
                            status = "success"
                            outcome = "success"
                            hint = None
                    else:
                        result = f"ERROR: user denied permission for {name}"
                        status = "denied"
                        outcome = "blocked"
                        log_decision = "user_denied"
                        hint = _RECOVERY_HINTS["denied"]

            else:  # Decision.DENY or unhandled
                result = f"ERROR: tool {name} is not permitted"
                status = "blocked"
                outcome = "blocked"
                log_decision = "deny"
                hint = _RECOVERY_HINTS["blocked"]

        duration_ms = (time.perf_counter() - start_time) * 1000

        # Audit every tool invocation attempt
        if self.audit:
            audit_decision = (
                "user_denied"
                if (is_shortcut and log_decision == "user_denied")
                else ("shortcut" if is_shortcut else log_decision)
            )
            try:
                self.audit.log(
                    tool=name,
                    args=args,
                    decision=audit_decision,
                    outcome=outcome,
                    result=result,
                    duration_ms=round(duration_ms, 2),
                    tainted=tainted,
                )
            except TypeError:
                self.audit.log(
                    tool=name,
                    args=args,
                    decision=audit_decision,
                    outcome=outcome,
                    result=result,
                    duration_ms=round(duration_ms, 2),
                )

        # If this tool produces untrusted output and succeeded, taint the agent for the rest of the turn
        if status == "success" and tool and getattr(tool, "untrusted_output", False):
            tainted = True

        # Update step run record
        step_decision = (
            "user_denied"
            if (is_shortcut and log_decision == "user_denied")
            else ("shortcut" if is_shortcut else None)
        )
        run.add_step(
            tool=name,
            args=args,
            status=status,
            result=result,
            duration_ms=int(round(duration_ms)),
            decision=step_decision,
        )

        self.last_tool_events.append((name, args, result))

        self._emit_event({
            "type": "step_end",
            "tool": name,
            "index": step_index,
            "status": status,
            "result": result,
            "duration_ms": int(round(duration_ms)),
        })

        # Update loop guard counters
        ok = (status == "success")
        guard.record(name, args, ok=ok)
        if ok:
            self.session_state.update_from_tool(name, args, status="success", sites=self.sites)

        return None, status, outcome, result, hint, tainted

    def _ask_confirmation(
        self, tool_name: str, args: dict, warning: Optional[str] = None
    ) -> bool:
        """Call confirm_callback with optional warning, falling back to 2-argument signature if needed."""
        try:
            return bool(self.confirm_callback(tool_name, args, warning=warning))
        except TypeError:
            return bool(self.confirm_callback(tool_name, args))

    def _emit_event(self, event: dict) -> None:
        """Safely emit an event to the on_event callback without allowing exceptions to break execution."""
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                # Exceptions inside on_event must never break the agent
                pass

    def _get_context_messages(self, limit: int = 20) -> list[dict]:
        """Trim history to at most `limit` messages while avoiding leading orphan tool messages."""
        messages = self.history[-limit:]
        # Slicing at a fixed offset can split an assistant call and its response.
        # Strip any leading 'tool' messages to preserve conversational role ordering.
        while messages and messages[0].get("role") == "tool":
            messages = messages[1:]
        return messages

    @staticmethod
    def _build_stop_message(
        status: RunStatus, executed_steps: list[StepRecord], pending_tools: list[str]
    ) -> str:
        """Construct a clear message explaining why the task stopped, what succeeded, and what was omitted."""
        done_tools = [s.tool for s in executed_steps if s.status == "success"]
        failed_tools = [
            s.tool for s in executed_steps
            if s.status != "success" and s.result != "ERROR: not executed (task stopped)"
        ]

        reason_descriptions = {
            RunStatus.STEP_LIMIT: "exceeded the maximum tool execution budget or time limit",
            RunStatus.LOOP_DETECTED: "detected repeated failures for the same tool call (loop prevention)",
            RunStatus.TOO_MANY_ERRORS: "encountered too many consecutive tool execution errors",
            RunStatus.CANCELLED: "task was cancelled by user",
            RunStatus.FAILED: "task execution failed",
        }
        reason = reason_descriptions.get(status, str(status.value))

        parts = [f"Task stopped: {reason}."]
        if done_tools:
            parts.append(f"Successfully executed: {', '.join(done_tools)}.")
        else:
            parts.append("No actions completed successfully.")

        if failed_tools:
            parts.append(f"Failed tools: {', '.join(failed_tools)}.")

        if pending_tools:
            parts.append(f"Not executed: {', '.join(pending_tools)}.")

        return " ".join(parts)

    def chat(
        self,
        user_text: str,
        extra_system_text: Optional[Union[str, list[str], Callable[[str], None]]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[Any] = None,
    ) -> str:
        """Run conversational loop handling permission checks, guards, tool executions, and audit logs."""
        if callable(extra_system_text) and on_delta is None:
            on_delta = extra_system_text
            extra_system_text = None

        effective_on_delta = on_delta if getattr(settings, "STREAM_REPLIES", True) else None
        should_stop = cancel_event.is_set if cancel_event is not None else None

        self.last_tool_events.clear()
        self._rejected_tools.clear()
        self.history.append({"role": "user", "content": user_text})
        tainted = False

        # Fresh guard and task run per chat interaction
        guard = LoopGuard(
            max_tool_calls=self.max_tool_calls,
            max_consecutive_errors=self.max_consecutive_errors,
            max_seconds=self.max_task_seconds,
        )
        run = TaskRun(goal=user_text)
        run.context = self.session_state.to_dict()
        _orig_finish = run.finish

        def finish_with_context(st: RunStatus, txt: str = "") -> None:
            run.context = self.session_state.to_dict()
            _orig_finish(st, txt)

        run.finish = finish_with_context  # type: ignore[assignment]
        self.last_run = run

        # Compute additional per-turn system text (memory, docs, etc.)
        extra_blocks: list[str] = []

        if self.memory_store is not None:
            from memory.context import build_memory_block
            mem_block = build_memory_block(self.memory_store, user_text)
            if mem_block:
                extra_blocks.append(mem_block)

        if self.retriever is not None and getattr(settings, "RAG_AUTO", True):
            retrieval = None
            retrieval_exc = None
            try:
                retrieval = self.retriever(user_text)
            except Exception as exc:
                retrieval_exc = exc

            if retrieval is not None:
                searched = bool(getattr(retrieval, "searched", False))
                block = getattr(retrieval, "block", "") or ""
                note = getattr(retrieval, "note", "") or ""
                injected = int(getattr(retrieval, "injected", 0) or 0)
                withheld = int(getattr(retrieval, "withheld", 0) or 0)
                raw_terms = getattr(retrieval, "terms", [])
                terms = list(raw_terms) if raw_terms else []
                reason = str(getattr(retrieval, "reason", "") or "")

                if block.strip():
                    extra_blocks.append(block.strip())
                    tainted = True
                    retrieval_outcome = "injected"
                elif note.strip():
                    extra_blocks.append(note.strip())
                    retrieval_outcome = "none"
                else:
                    retrieval_outcome = "none"

                run.auto_retrievals.append({
                    "terms": terms,
                    "injected": injected,
                    "withheld": withheld,
                    "searched": searched,
                    "reason": reason,
                    "note": note,
                })

                self._emit_event({
                    "type": "auto_retrieval",
                    "injected": injected,
                    "withheld": withheld,
                })

                if self.audit:
                    try:
                        self.audit.log(
                            tool="auto_retrieval",
                            args={},
                            decision="auto_retrieval",
                            outcome=retrieval_outcome,
                            result=None,
                            tainted=tainted,
                        )
                    except TypeError:
                        self.audit.log(
                            tool="auto_retrieval",
                            args={},
                            decision="auto_retrieval",
                            outcome=retrieval_outcome,
                        )
            else:
                run.auto_retrievals.append({
                    "terms": [],
                    "injected": 0,
                    "withheld": 0,
                    "searched": False,
                    "reason": f"error: {retrieval_exc}",
                })
                self._emit_event({
                    "type": "auto_retrieval",
                    "injected": 0,
                    "withheld": 0,
                })
                if self.audit:
                    try:
                        self.audit.log(
                            tool="auto_retrieval",
                            args={},
                            decision="auto_retrieval",
                            outcome="none",
                            result=None,
                            tainted=tainted,
                        )
                    except TypeError:
                        self.audit.log(
                            tool="auto_retrieval",
                            args={},
                            decision="auto_retrieval",
                            outcome="none",
                        )

        if self.session_context_enabled():
            ctx_block = self.session_state.context_block()
            if ctx_block:
                extra_blocks.append(ctx_block)

        if extra_system_text:
            if isinstance(extra_system_text, str):
                if extra_system_text.strip():
                    extra_blocks.append(extra_system_text.strip())
            elif isinstance(extra_system_text, (list, tuple)):
                for b in extra_system_text:
                    if b and str(b).strip():
                        extra_blocks.append(str(b).strip())

        combined_extra = "\n\n".join(extra_blocks)

        # Deterministic shortcut bypass before any LLM call
        if self.shortcuts_enabled():
            from core.shortcuts import ShortcutPlan, ToolCallSpec, parse_shortcut

            browser_enabled = bool(
                getattr(self, "browser_controller", None)
                or (self.registry and (bool(self.registry.get("browser_click_result")) or bool(self.registry.get("browser_scroll"))))
            )

            shortcut_spec = parse_shortcut(
                user_text,
                self.allowed_apps,
                self.sites,
                state=self.session_state,
                browser_enabled=browser_enabled,
            )

            if isinstance(shortcut_spec, ShortcutPlan):
                if not all(self.registry and self.registry.get(s.name) for s in shortcut_spec.steps):
                    pass
                else:
                    if cancel_event is not None and cancel_event.is_set():
                        cancel_reply = "(interrupted)"
                        run.finish(RunStatus.CANCELLED, cancel_reply)
                        if self.run_logger:
                            self.run_logger.log(run)
                        self.history.append({"role": "assistant", "content": cancel_reply})
                        self._emit_event({
                            "type": "task_end",
                            "status": RunStatus.CANCELLED.value,
                            "tool": None,
                            "index": len(run.steps),
                        })
                        return cancel_reply

                    site_display = {
                        "youtube": "YouTube",
                        "google": "Google",
                        "github": "GitHub",
                        "wikipedia": "Wikipedia",
                    }.get(shortcut_spec.site.lower(), shortcut_spec.site.title())

                    failed_step_index = None
                    failed_reason = ""
                    stop_reason = None

                    for idx, step in enumerate(shortcut_spec.steps):
                        if cancel_event is not None and cancel_event.is_set():
                            cancel_reply = "(interrupted)"
                            run.finish(RunStatus.CANCELLED, cancel_reply)
                            if self.run_logger:
                                self.run_logger.log(run)
                            self.history.append({"role": "assistant", "content": cancel_reply})
                            self._emit_event({
                                "type": "task_end",
                                "status": RunStatus.CANCELLED.value,
                                "tool": None,
                                "index": len(run.steps),
                            })
                            return cancel_reply

                        s_stop_reason, status, outcome, result, hint, tainted = self._execute_single_tool(
                            step.name,
                            step.args,
                            guard,
                            run,
                            tainted,
                            is_shortcut=True,
                        )

                        if s_stop_reason is not None:
                            failed_step_index = idx
                            stop_reason = s_stop_reason
                            failed_reason = self._build_stop_message(s_stop_reason, run.steps, [step.name])
                            break

                        if status != "success":
                            failed_step_index = idx
                            clean_msg = result
                            for pfx in ("ERROR: REJECTED:", "ERROR: BLOCKED:", "ERROR:"):
                                if clean_msg.startswith(pfx):
                                    clean_msg = clean_msg[len(pfx):].strip()
                                    break
                            failed_reason = clean_msg
                            break

                    if getattr(shortcut_spec, "style", "") == "ordinal":
                        ordinal_num = getattr(shortcut_spec, "ordinal", 1)
                        step1_res = run.steps[1].result if len(run.steps) > 1 else ""
                        prefix = f"Opened result {ordinal_num}: "
                        if step1_res.startswith(prefix):
                            title = step1_res[len(prefix):].strip()
                        elif step1_res.startswith("Opened result"):
                            title = step1_res.split(":", 1)[-1].strip() if ":" in step1_res else step1_res.strip()
                        else:
                            title = step1_res.strip()

                        if failed_step_index is None:
                            step2_res = run.steps[2].result if len(run.steps) > 2 else "Playing."
                            if step2_res.strip() == "Playing.":
                                reply = f"Opened result {ordinal_num}: {title}. Playing."
                            elif "didn't start" in step2_res or "press play" in step2_res:
                                reply = f"Opened result {ordinal_num}: {title}. The video is open but didn't start; press play."
                            else:
                                reply = f"Opened result {ordinal_num}: {title}. {step2_res}"
                            run.finish(RunStatus.COMPLETED, reply)
                        elif failed_step_index == 2:
                            reply = f"Opened result {ordinal_num}: {title}. The video is open but didn't start; press play."
                            run.finish(RunStatus.COMPLETED, reply)
                        elif failed_step_index == 1:
                            clean_r = failed_reason.rstrip(".")
                            reply = f"Searched {site_display} for {shortcut_spec.query}, but I couldn't open result {ordinal_num}: {clean_r}."
                            run.finish(stop_reason if stop_reason is not None else RunStatus.COMPLETED, reply)
                        else:
                            clean_r = failed_reason.rstrip(".")
                            reply = f"Searched {site_display} for {shortcut_spec.query}, but search failed: {clean_r}."
                            run.finish(stop_reason if stop_reason is not None else RunStatus.COMPLETED, reply)
                    elif shortcut_spec.is_play:
                        if failed_step_index is None:
                            reply = f"Searched {site_display} for {shortcut_spec.query} and started the first result."
                            run.finish(RunStatus.COMPLETED, reply)
                        elif failed_step_index == 2:
                            # Final media(play) step failure is not fatal
                            reply = f"Searched {site_display} for {shortcut_spec.query} and opened the first result; press play if it didn't start."
                            run.finish(RunStatus.COMPLETED, reply)
                        else:
                            clean_r = failed_reason.rstrip(".")
                            reply = f"Searched {site_display} for {shortcut_spec.query}, but I couldn't start a video: {clean_r}."
                            run.finish(stop_reason if stop_reason is not None else RunStatus.COMPLETED, reply)
                    else:
                        if failed_step_index is None:
                            reply = f"Searched {site_display} for {shortcut_spec.query}."
                            run.finish(RunStatus.COMPLETED, reply)
                        else:
                            clean_r = failed_reason.rstrip(".")
                            reply = f"Searched {site_display} for {shortcut_spec.query}, but failed: {clean_r}."
                            run.finish(stop_reason if stop_reason is not None else RunStatus.COMPLETED, reply)

                    stream_cb = effective_on_delta or on_delta
                    if stream_cb:
                        try:
                            stream_cb(reply)
                        except Exception:
                            pass

                    self.history.append({"role": "assistant", "content": reply})
                    if self.run_logger:
                        self.run_logger.log(run)
                    self._emit_event({
                        "type": "task_end",
                        "status": run.status.value if run.status else RunStatus.COMPLETED.value,
                        "tool": None,
                        "index": len(run.steps),
                    })
                    return reply

            elif (
                shortcut_spec is not None
                and self.registry
                and self.registry.get(shortcut_spec.name)
            ):
                if cancel_event is not None and cancel_event.is_set():
                    cancel_reply = "(interrupted)"
                    run.finish(RunStatus.CANCELLED, cancel_reply)
                    if self.run_logger:
                        self.run_logger.log(run)
                    self.history.append({"role": "assistant", "content": cancel_reply})
                    self._emit_event({
                        "type": "task_end",
                        "status": RunStatus.CANCELLED.value,
                        "tool": None,
                        "index": len(run.steps),
                    })
                    return cancel_reply

                stop_reason, status, outcome, result, hint, tainted = self._execute_single_tool(
                    shortcut_spec.name,
                    shortcut_spec.args,
                    guard,
                    run,
                    tainted,
                    is_shortcut=True,
                )
                if stop_reason is not None:
                    reply = self._build_stop_message(stop_reason, run.steps, [shortcut_spec.name])
                    run.finish(stop_reason, reply)
                elif status == "success":
                    if shortcut_spec.name.startswith("browser_"):
                        reply = result
                    else:
                        target_name = getattr(shortcut_spec, "target_name", "") or shortcut_spec.name
                        reply = f"Opened {target_name}."
                    run.finish(RunStatus.COMPLETED, reply)
                else:
                    if result.startswith("ERROR: I can only control"):
                        reply = result[len("ERROR:"):].strip()
                    else:
                        reply = result
                    run.finish(RunStatus.COMPLETED, reply)

                stream_cb = effective_on_delta or on_delta
                if stream_cb:
                    try:
                        stream_cb(reply)
                    except Exception:
                        pass

                self.history.append({"role": "assistant", "content": reply})
                if self.run_logger:
                    self.run_logger.log(run)
                self._emit_event({
                    "type": "task_end",
                    "status": run.status.value if run.status else RunStatus.COMPLETED.value,
                    "tool": None,
                    "index": len(run.steps),
                })
                return reply

        tools = self.registry.list_tools() if self.registry else None

        try:
            for _ in range(self.max_steps):
                context_messages = self._get_context_messages(limit=20)
                if combined_extra:
                    system_text = (
                        f"{self.system_prompt}\n\n{combined_extra}"
                        if self.system_prompt
                        else combined_extra
                    )
                    messages_to_send = [{"role": "system", "content": system_text}] + context_messages
                else:
                    messages_to_send = context_messages

                run.llm_calls += 1
                try:
                    response = self.provider.generate(
                        messages_to_send,
                        tools=tools,
                        on_delta=effective_on_delta,
                        should_stop=should_stop,
                    )
                except TypeError:
                    response = self.provider.generate(messages_to_send, tools=tools)

                if getattr(response, "interrupted", False):
                    reply = response.text if (response.text and response.text.strip()) else "(interrupted)"
                    self.history.append({"role": "assistant", "content": reply})
                    run.finish(RunStatus.CANCELLED, reply)
                    if self.run_logger:
                        self.run_logger.log(run)
                    self._emit_event({
                        "type": "task_end",
                        "status": RunStatus.CANCELLED.value,
                        "tool": None,
                        "index": len(run.steps),
                    })
                    return response.text

                if not response.tool_calls:
                    reply = response.text or ""

                    # If final text of a turn with no tool calls is empty, retry once with system note
                    if not reply.strip() and len(run.steps) == 0:
                        empty_note = "Your previous reply was empty. Answer the user directly in one or two sentences."
                        if self.audit:
                            try:
                                self.audit.log(
                                    tool="empty_reply",
                                    args={},
                                    decision="empty_reply",
                                    outcome="retried",
                                    result=None,
                                    tainted=tainted,
                                )
                            except TypeError:
                                self.audit.log(
                                    tool="empty_reply",
                                    args={},
                                    decision="empty_reply",
                                    outcome="retried",
                                )
                        run.add_step(
                            tool="empty_reply",
                            args={},
                            status="retried",
                            result="",
                            duration_ms=0,
                        )

                        if combined_extra:
                            retry_extra = f"{combined_extra}\n\n{empty_note}"
                            retry_sys = f"{self.system_prompt}\n\n{retry_extra}" if self.system_prompt else retry_extra
                        else:
                            retry_sys = f"{self.system_prompt}\n\n{empty_note}" if self.system_prompt else empty_note

                        retry_messages = [{"role": "system", "content": retry_sys}] + context_messages

                        run.llm_calls += 1
                        try:
                            retry_response = self.provider.generate(
                                retry_messages,
                                tools=tools,
                                on_delta=effective_on_delta,
                                should_stop=should_stop,
                            )
                        except TypeError:
                            retry_response = self.provider.generate(retry_messages, tools=tools)

                        if getattr(retry_response, "interrupted", False):
                            cancel_reply = retry_response.text if (retry_response.text and retry_response.text.strip()) else "(interrupted)"
                            self.history.append({"role": "assistant", "content": cancel_reply})
                            run.finish(RunStatus.CANCELLED, cancel_reply)
                            if self.run_logger:
                                self.run_logger.log(run)
                            self._emit_event({
                                "type": "task_end",
                                "status": RunStatus.CANCELLED.value,
                                "tool": None,
                                "index": len(run.steps),
                            })
                            return cancel_reply

                        retry_text = retry_response.text or ""
                        if not retry_response.tool_calls and not retry_text.strip():
                            fallback_msg = "I couldn't produce an answer. Please rephrase or try again."
                            if self.audit:
                                try:
                                    self.audit.log(
                                        tool="empty_reply",
                                        args={},
                                        decision="empty_reply",
                                        outcome="fallback",
                                        result=fallback_msg,
                                        tainted=tainted,
                                    )
                                except TypeError:
                                    self.audit.log(
                                        tool="empty_reply",
                                        args={},
                                        decision="empty_reply",
                                        outcome="fallback",
                                    )
                            run.add_step(
                                tool="empty_reply",
                                args={},
                                status="fallback",
                                result=fallback_msg,
                                duration_ms=0,
                            )
                            reply = fallback_msg
                            stream_cb = effective_on_delta or on_delta
                            if stream_cb:
                                try:
                                    stream_cb(reply)
                                except Exception:
                                    pass
                            self.history.append({"role": "assistant", "content": reply})
                            run.finish(RunStatus.COMPLETED, reply)
                            if self.run_logger:
                                self.run_logger.log(run)
                            self._emit_event({
                                "type": "task_end",
                                "status": RunStatus.COMPLETED.value,
                                "tool": None,
                                "index": len(run.steps),
                            })
                            return reply
                        elif not retry_response.tool_calls:
                            reply = retry_text
                        else:
                            response = retry_response

                if not response.tool_calls:
                    # Verify action claims against tool executions in this turn
                    try:
                        from core.claims import find_action_claims, unverified_claims

                        claims = find_action_claims(reply)
                        if claims:
                            unverified = unverified_claims(claims, run.steps)
                            if unverified:
                                warning_line = (
                                    "\n\n[ORBIT check] I said I did something, but no tool actually did it, "
                                    "so nothing was changed."
                                )
                                reply = reply + warning_line
                                stream_cb = effective_on_delta or on_delta
                                if stream_cb:
                                    try:
                                        stream_cb(warning_line)
                                    except Exception:
                                        pass
                                run.unverified_claims += len(unverified)
                                if self.audit:
                                    try:
                                        self.audit.log(
                                            tool="claim_check",
                                            args={"claims": [c.phrase for c in unverified], "count": len(unverified)},
                                            decision="claim_check",
                                            outcome="flagged",
                                            result=warning_line.strip(),
                                        )
                                    except TypeError:
                                        self.audit.log(
                                            tool="claim_check",
                                            args={"claims": [c.phrase for c in unverified], "count": len(unverified)},
                                            decision="claim_check",
                                            outcome="flagged",
                                        )
                    except Exception:
                        pass

                    self.history.append({"role": "assistant", "content": reply})
                    run.finish(RunStatus.COMPLETED, reply)
                    if self.run_logger:
                        self.run_logger.log(run)
                    self._emit_event({
                        "type": "task_end",
                        "status": RunStatus.COMPLETED.value,
                        "tool": None,
                        "index": len(run.steps),
                    })
                    return reply

                # Record assistant turn with its tool requests
                self.history.append({
                    "role": "assistant",
                    "content": response.text or "",
                    "tool_calls": response.tool_calls,
                })

                # Process each requested tool
                for tc_idx, tc in enumerate(response.tool_calls):
                    step_index = len(run.steps) + 1

                    # Check cancel_event before executing each tool call
                    if cancel_event is not None and cancel_event.is_set():
                        pending_names = []
                        for unexecuted_tc in response.tool_calls[tc_idx:]:
                            pending_names.append(unexecuted_tc.name)
                            not_exec_msg = "ERROR: not executed (task stopped)"
                            self.history.append({
                                "role": "tool",
                                "name": unexecuted_tc.name,
                                "content": not_exec_msg,
                            })
                            self.last_tool_events.append((unexecuted_tc.name, unexecuted_tc.args, not_exec_msg))
                            run.add_step(
                                tool=unexecuted_tc.name,
                                args=unexecuted_tc.args,
                                status="blocked",
                                result=not_exec_msg,
                                duration_ms=0,
                            )

                        cancel_reply = "(interrupted)"
                        run.finish(RunStatus.CANCELLED, cancel_reply)
                        if self.run_logger:
                            self.run_logger.log(run)
                        self.history.append({"role": "assistant", "content": cancel_reply})
                        self._emit_event({
                            "type": "task_end",
                            "status": RunStatus.CANCELLED.value,
                            "tool": None,
                            "index": len(run.steps),
                        })
                        return cancel_reply

                    stop_reason, status, outcome, result, hint, tainted = self._execute_single_tool(
                        tc.name,
                        tc.args,
                        guard,
                        run,
                        tainted,
                        is_shortcut=False,
                    )
                    if stop_reason is not None:
                        # Append unexecuted tool messages for all remaining calls to preserve history consistency
                        pending_names = []
                        for unexecuted_tc in response.tool_calls[tc_idx:]:
                            pending_names.append(unexecuted_tc.name)
                            not_exec_msg = "ERROR: not executed (task stopped)"
                            self.history.append({
                                "role": "tool",
                                "name": unexecuted_tc.name,
                                "content": not_exec_msg,
                            })
                            self.last_tool_events.append((unexecuted_tc.name, unexecuted_tc.args, not_exec_msg))
                            run.add_step(
                                tool=unexecuted_tc.name,
                                args=unexecuted_tc.args,
                                status="blocked",
                                result=not_exec_msg,
                                duration_ms=0,
                            )

                        final_reply = self._build_stop_message(stop_reason, run.steps, pending_names)
                        run.finish(stop_reason, final_reply)
                        if self.run_logger:
                            self.run_logger.log(run)
                        self.history.append({"role": "assistant", "content": final_reply})
                        self._emit_event({
                            "type": "task_end",
                            "status": stop_reason.value,
                            "tool": None,
                            "index": len(run.steps),
                        })
                        return final_reply

                    # Append recovery hint to content sent to the LLM
                    content_for_llm = f"{result}\n{hint}" if hint else result
                    self.history.append({
                        "role": "tool",
                        "name": tc.name,
                        "content": content_for_llm,
                    })

                    # Check for consecutive error thresholds
                    if guard.should_stop_for_errors():
                        # Fill pending unexecuted tool calls
                        pending_names = []
                        for unexecuted_tc in response.tool_calls[tc_idx + 1:]:
                            pending_names.append(unexecuted_tc.name)
                            not_exec_msg = "ERROR: not executed (task stopped)"
                            self.history.append({
                                "role": "tool",
                                "name": unexecuted_tc.name,
                                "content": not_exec_msg,
                            })
                            self.last_tool_events.append((unexecuted_tc.name, unexecuted_tc.args, not_exec_msg))
                            run.add_step(
                                tool=unexecuted_tc.name,
                                args=unexecuted_tc.args,
                                status="blocked",
                                result=not_exec_msg,
                                duration_ms=0,
                            )

                        stop_reason = RunStatus.TOO_MANY_ERRORS
                        final_reply = self._build_stop_message(stop_reason, run.steps, pending_names)
                        run.finish(stop_reason, final_reply)
                        if self.run_logger:
                            self.run_logger.log(run)
                        self.history.append({"role": "assistant", "content": final_reply})
                        self._emit_event({
                            "type": "task_end",
                            "status": stop_reason.value,
                            "tool": None,
                            "index": len(run.steps),
                        })
                        return final_reply

            # Fallback if max_steps is reached without terminal assistant text
            fallback_msg = "I'm sorry, but I was unable to complete your request within the allowed steps."
            run.finish(RunStatus.STEP_LIMIT, fallback_msg)
            if self.run_logger:
                self.run_logger.log(run)
            self.history.append({"role": "assistant", "content": fallback_msg})
            self._emit_event({
                "type": "task_end",
                "status": RunStatus.STEP_LIMIT.value,
                "tool": None,
                "index": len(run.steps),
            })
            return fallback_msg

        except KeyboardInterrupt:
            # Handle cancellation: fill pending tool results for any open assistant turn
            last_assistant = None
            assistant_idx = -1
            for idx in range(len(self.history) - 1, -1, -1):
                if self.history[idx].get("role") == "assistant" and "tool_calls" in self.history[idx]:
                    last_assistant = self.history[idx]
                    assistant_idx = idx
                    break

            if last_assistant and assistant_idx >= 0:
                open_tcs = last_assistant.get("tool_calls") or []
                appended_tool_msgs = sum(
                    1 for m in self.history[assistant_idx + 1:] if m.get("role") == "tool"
                )
                for pending_tc in open_tcs[appended_tool_msgs:]:
                    not_exec_msg = "ERROR: not executed (task stopped)"
                    self.history.append({
                        "role": "tool",
                        "name": pending_tc.name,
                        "content": not_exec_msg,
                    })
                    self.last_tool_events.append((pending_tc.name, pending_tc.args, not_exec_msg))
                    run.add_step(
                        tool=pending_tc.name,
                        args=pending_tc.args,
                        status="blocked",
                        result=not_exec_msg,
                        duration_ms=0,
                    )

            cancel_msg = "Task cancelled."
            run.finish(RunStatus.CANCELLED, cancel_msg)
            if self.run_logger:
                self.run_logger.log(run)
            self.history.append({"role": "assistant", "content": cancel_msg})
            self._emit_event({
                "type": "task_end",
                "status": RunStatus.CANCELLED.value,
                "tool": None,
                "index": len(run.steps),
            })
            return cancel_msg

    def reset(self) -> None:
        """Clear conversation history and recent tool event records."""
        self.history.clear()
        self.last_tool_events.clear()
        self.last_run = None
        self.session_state.clear()

    def run_plan(self, plan: "Plan") -> str:
        """Execute a structured plan by submitting it through chat().

        Guards, error limits, cancellation, and permission checks apply automatically.
        """
        import re
        plan_lines = []
        for idx, step in enumerate(plan.steps, start=1):
            clean_step = re.sub(r"^\d+[\.\)]\s*", "", step.strip())
            plan_lines.append(f"{idx}. {clean_step}")
        formatted_plan = "\n".join(plan_lines)

        message = (
            f"Goal: {plan.goal}\n\n"
            f"Plan:\n{formatted_plan}\n\n"
            "Follow this plan step by step using tools. If a step fails, stop and report."
        )
        return self.chat(message)

