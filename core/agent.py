"""Agent implementation managing conversational state, tool calling, permissions, guards, and audit logging."""

import time
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

if TYPE_CHECKING:
    from core.planner import Plan

from config import settings
from core.guards import LoopGuard
from core.llm import LLMProvider
from core.task import RunLogger, RunStatus, StepRecord, TaskRun
from security.audit import AuditLogger
from security.permissions import Decision, PermissionPolicy
from tools.registry import ToolRegistry

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
        self.last_run = run

        # Compute additional per-turn system text (memory, docs, etc.)
        extra_blocks: list[str] = []

        if self.memory_store is not None:
            from memory.context import build_memory_block
            mem_block = build_memory_block(self.memory_store, user_text)
            if mem_block:
                extra_blocks.append(mem_block)

        if extra_system_text:
            if isinstance(extra_system_text, str):
                if extra_system_text.strip():
                    extra_blocks.append(extra_system_text.strip())
            elif isinstance(extra_system_text, (list, tuple)):
                for b in extra_system_text:
                    if b and str(b).strip():
                        extra_blocks.append(str(b).strip())

        combined_extra = "\n\n".join(extra_blocks)

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

                    # 1. Pre-execution guard check
                    stop_reason = guard.check_before(tc.name, tc.args)
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

                    self._emit_event({
                        "type": "step_start",
                        "tool": tc.name,
                        "status": "start",
                        "index": step_index,
                        "args": tc.args,
                    })

                    start_time = time.perf_counter()

                    tool = self.registry.get(tc.name) if self.registry else None
                    tool_level = tool.permission if tool else None

                    if tc.name in self._rejected_tools:
                        result = f"ERROR: BLOCKED: an earlier call to {tc.name} was rejected in this turn; not retrying."
                        status = "blocked"
                        outcome = "blocked"
                        log_decision = "rejected_retry"
                        hint = _RECOVERY_HINTS["blocked"]
                    else:
                        # Determine security decision if policy is configured
                        if self.policy is not None:
                            decision = self.policy.decide(tc.name, tool_level)
                        else:
                            decision = Decision.ALLOW

                        # Execute or prompt based on policy decision
                        if decision == Decision.ALLOW:
                            if self.registry:
                                result = self.registry.execute(tc.name, tc.args)
                            else:
                                result = f"ERROR: No tool registry configured to execute '{tc.name}'"
                            log_decision = "allow"

                            if result.startswith("ERROR: REJECTED:"):
                                status = "rejected"
                                outcome = "rejected"
                                hint = _RECOVERY_HINTS["rejected"]
                                self._rejected_tools.add(tc.name)
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
                                precheck_err = self.registry.validate_arguments(tc.name, tc.args)

                            if precheck_err is None and tool and tool.precheck is not None:
                                try:
                                    pre_res = tool.precheck(tc.args)
                                    if pre_res is not None:
                                        precheck_err = str(pre_res)
                                except Exception as exc:
                                    precheck_err = f"ERROR: Tool '{tc.name}' precheck failed: {exc}"

                            if precheck_err is not None:
                                if not precheck_err.startswith("ERROR"):
                                    precheck_err = f"ERROR: {precheck_err}"
                                result = precheck_err
                                if result.startswith("ERROR: REJECTED:"):
                                    status = "rejected"
                                    outcome = "rejected"
                                    hint = _RECOVERY_HINTS["rejected"]
                                    self._rejected_tools.add(tc.name)
                                else:
                                    status = "blocked"
                                    outcome = "blocked"
                                    log_decision = "precheck_failed"
                                    hint = _RECOVERY_HINTS["blocked"]
                            else:
                                warning = TAINT_WARNING if tainted else None
                                confirmed = self._ask_confirmation(tc.name, tc.args, warning=warning)
                                if confirmed:
                                    if self.registry:
                                        result = self.registry.execute(tc.name, tc.args)
                                    else:
                                        result = f"ERROR: No tool registry configured to execute '{tc.name}'"
                                    log_decision = "user_approved"

                                    if result.startswith("ERROR: REJECTED:"):
                                        status = "rejected"
                                        outcome = "rejected"
                                        hint = _RECOVERY_HINTS["rejected"]
                                        self._rejected_tools.add(tc.name)
                                    elif result.startswith("ERROR"):
                                        status = "error"
                                        outcome = "error"
                                        hint = _RECOVERY_HINTS["error"]
                                    else:
                                        status = "success"
                                        outcome = "success"
                                        hint = None
                                else:
                                    result = f"ERROR: user denied permission for {tc.name}"
                                    status = "denied"
                                    outcome = "blocked"
                                    log_decision = "user_denied"
                                    hint = _RECOVERY_HINTS["denied"]

                        else:  # Decision.DENY or unhandled
                            result = f"ERROR: tool {tc.name} is not permitted"
                            status = "blocked"
                            outcome = "blocked"
                            log_decision = "deny"
                            hint = _RECOVERY_HINTS["blocked"]

                    duration_ms = (time.perf_counter() - start_time) * 1000

                    # Audit every tool invocation attempt
                    if self.audit:
                        try:
                            self.audit.log(
                                tool=tc.name,
                                args=tc.args,
                                decision=log_decision,
                                outcome=outcome,
                                result=result,
                                duration_ms=round(duration_ms, 2),
                                tainted=tainted,
                            )
                        except TypeError:
                            self.audit.log(
                                tool=tc.name,
                                args=tc.args,
                                decision=log_decision,
                                outcome=outcome,
                                result=result,
                                duration_ms=round(duration_ms, 2),
                            )

                    # If this tool produces untrusted output and succeeded, taint the agent for the rest of the turn
                    if status == "success" and tool and getattr(tool, "untrusted_output", False):
                        tainted = True

                    # Update step run record
                    run.add_step(
                        tool=tc.name,
                        args=tc.args,
                        status=status,
                        result=result,
                        duration_ms=int(round(duration_ms)),
                    )

                    self.last_tool_events.append((tc.name, tc.args, result))

                    # Append recovery hint to content sent to the LLM
                    content_for_llm = f"{result}\n{hint}" if hint else result
                    self.history.append({
                        "role": "tool",
                        "name": tc.name,
                        "content": content_for_llm,
                    })

                    self._emit_event({
                        "type": "step_end",
                        "tool": tc.name,
                        "index": step_index,
                        "status": status,
                        "result": result,
                        "duration_ms": int(round(duration_ms)),
                    })

                    # Update loop guard counters
                    ok = (status == "success")
                    guard.record(tc.name, tc.args, ok=ok)

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

