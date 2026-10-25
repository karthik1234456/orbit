"""Tests for Agent recovery hints, loop guards, error limits, cancellation, events, and task logging."""

import json
from pathlib import Path

from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.task import RunLogger, RunStatus
from security.permissions import PermissionLevel, PermissionPolicy
from tools.registry import Tool, ToolRegistry


class FakeProvider(LLMProvider):
    """Scripted LLM provider returning predefined responses in sequence."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(self, messages: list[dict], tools=None) -> LLMResponse:
        self.call_history.append(list(messages))
        if not self.responses:
            return LLMResponse(text="Default fallback response")
        return self.responses.pop(0)


def _verify_history_consistency(history: list[dict]) -> None:
    """Verify that every assistant message with tool_calls is followed by a tool message for each call."""
    idx = 0
    while idx < len(history):
        msg = history[idx]
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            tcs = msg.get("tool_calls") or []
            # The next len(tcs) messages MUST be 'tool' messages
            for offset, tc in enumerate(tcs):
                tool_msg_idx = idx + 1 + offset
                assert tool_msg_idx < len(history), (
                    f"Assistant at {idx} requested {len(tcs)} tools, but history ended prematurely at {tool_msg_idx}"
                )
                tool_msg = history[tool_msg_idx]
                assert tool_msg.get("role") == "tool", (
                    f"Expected tool message at index {tool_msg_idx}, got role={tool_msg.get('role')}"
                )
                assert tool_msg.get("name") == tc.name, (
                    f"Expected tool message for '{tc.name}' at {tool_msg_idx}, got '{tool_msg.get('name')}'"
                )
            idx += 1 + len(tcs)
        else:
            idx += 1


def test_1_denied_result_carries_recovery_hint():
    """(1) A denied result carries the 'Do not retry' hint sent to the LLM."""
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="confirm_action",
            description="Requires confirmation",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
            func=lambda target: f"done {target}",
            permission=PermissionLevel.CONFIRM,
        )
    )

    policy = PermissionPolicy()
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="confirm_action", args={"target": "system"})]),
        LLMResponse(text="Understood, action was denied."),
    ])

    # User denies confirmation
    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        confirm_callback=lambda name, args: False,
    )

    reply = agent.chat("delete system")
    assert reply == "Understood, action was denied."

    # Verify tool message sent to LLM in call_history[1]
    assert len(provider.call_history) == 2
    tool_messages = [m for m in provider.call_history[1] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    content = tool_messages[0]["content"]
    assert "ERROR: user denied permission" in content
    expected_hint = "Do not retry this action. Tell the user it was not approved and ask what they want instead."
    assert expected_hint in content


def test_2_same_failing_call_twice_triggers_loop_detected():
    """(2) The same failing call twice triggers LOOP_DETECTED and is not executed a third time."""
    execution_count = 0

    def faulty_tool(param: str):
        nonlocal execution_count
        execution_count += 1
        return "ERROR: invalid parameter value"

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="faulty_tool",
            description="Consistently failing tool",
            parameters={"type": "object", "properties": {"param": {"type": "string"}}},
            func=faulty_tool,
            permission=PermissionLevel.SAFE,
        )
    )

    # Provider script: LLM attempts identical failing call 3 times
    failing_call = ToolCall(name="faulty_tool", args={"param": "retry_me"})
    provider = FakeProvider([
        LLMResponse(tool_calls=[failing_call]),  # 1st attempt -> executed, fails
        LLMResponse(tool_calls=[failing_call]),  # 2nd attempt -> executed, fails
        LLMResponse(tool_calls=[failing_call]),  # 3rd attempt -> LOOP_DETECTED by check_before
    ])

    agent = Agent(provider=provider, registry=reg)
    reply = agent.chat("run faulty tool")

    # Should execute exactly twice, blocked before the third execution
    assert execution_count == 2
    assert agent.last_run is not None
    assert agent.last_run.status == RunStatus.LOOP_DETECTED
    assert "loop" in reply.lower() or "detected" in reply.lower()
    _verify_history_consistency(agent.history)


def test_3_three_different_failing_calls_in_a_row_give_too_many_errors():
    """(3) Three different failing calls in a row give TOO_MANY_ERRORS."""
    call_log = []

    def make_fail(name: str):
        def _fn():
            call_log.append(name)
            return f"ERROR: failure from {name}"
        return _fn

    reg = ToolRegistry()
    for name in ["fail_a", "fail_b", "fail_c", "fail_d"]:
        reg.register(
            Tool(
                name=name,
                description=f"Failing tool {name}",
                parameters={"type": "object", "properties": {}},
                func=make_fail(name),
                permission=PermissionLevel.SAFE,
            )
        )

    # 3 different calls so LOOP_DETECTED is not triggered, but consecutive errors reaches 3
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="fail_a", args={})]),
        LLMResponse(tool_calls=[ToolCall(name="fail_b", args={})]),
        LLMResponse(tool_calls=[ToolCall(name="fail_c", args={})]),
        LLMResponse(tool_calls=[ToolCall(name="fail_d", args={})]),
    ])

    agent = Agent(provider=provider, registry=reg, max_consecutive_errors=3)
    reply = agent.chat("trigger errors")

    assert call_log == ["fail_a", "fail_b", "fail_c"]
    assert agent.last_run is not None
    assert agent.last_run.status == RunStatus.TOO_MANY_ERRORS
    assert "too many" in reply.lower() or "errors" in reply.lower()
    _verify_history_consistency(agent.history)


def test_4_exceeding_tool_budget_gives_step_limit_with_message():
    """(4) Exceeding the tool budget gives STEP_LIMIT with an informative message."""
    executed = []

    def safe_step(num: int):
        executed.append(num)
        return f"Step {num} completed successfully"

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="safe_step",
            description="Performs a step",
            parameters={"type": "object", "properties": {"num": {"type": "integer"}}},
            func=safe_step,
            permission=PermissionLevel.SAFE,
        )
    )

    # Budget is 2 tool calls. Provider requests 3 sequential calls.
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="safe_step", args={"num": 1})]),
        LLMResponse(tool_calls=[ToolCall(name="safe_step", args={"num": 2})]),
        LLMResponse(tool_calls=[ToolCall(name="safe_step", args={"num": 3})]),
    ])

    agent = Agent(provider=provider, registry=reg, max_tool_calls=2)
    reply = agent.chat("do 3 steps")

    # Step 1 and 2 execute; step 3 blocked before execution by guard
    assert executed == [1, 2]
    assert agent.last_run is not None
    assert agent.last_run.status == RunStatus.STEP_LIMIT
    assert "Task stopped" in reply
    assert "budget" in reply.lower() or "limit" in reply.lower()
    assert "safe_step" in reply
    _verify_history_consistency(agent.history)


def test_5_keyboard_interrupt_gives_cancelled_and_next_chat_works():
    """(5) KeyboardInterrupt raised inside a tool gives CANCELLED and the next chat() works."""

    def interrupt_tool():
        raise KeyboardInterrupt("Simulated user abort")

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="interrupt_tool",
            description="Raises KeyboardInterrupt",
            parameters={"type": "object", "properties": {}},
            func=interrupt_tool,
            permission=PermissionLevel.SAFE,
        )
    )

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="interrupt_tool", args={})]),
        LLMResponse(text="Next conversation works normally!"),
    ])

    agent = Agent(provider=provider, registry=reg)

    # First chat interrupted
    reply = agent.chat("run tool that gets cancelled")
    assert reply == "Task cancelled."
    assert agent.last_run is not None
    assert agent.last_run.status == RunStatus.CANCELLED
    assert agent.last_run.final_text == "Task cancelled."
    _verify_history_consistency(agent.history)

    # Subsequent chat call works without issue
    reply2 = agent.chat("hello again")
    assert reply2 == "Next conversation works normally!"
    assert agent.last_run is not None
    assert agent.last_run.status == RunStatus.COMPLETED
    _verify_history_consistency(agent.history)


def test_6_history_consistency_on_early_stop():
    """(6) After any early stop, history never has an assistant tool_calls message without matching tool messages."""
    executed = []

    def first_success():
        executed.append("first")
        return "success"

    def will_interrupt():
        executed.append("second")
        raise KeyboardInterrupt("User abort mid-batch")

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="first_success",
            description="First tool",
            parameters={"type": "object", "properties": {}},
            func=first_success,
            permission=PermissionLevel.SAFE,
        )
    )
    reg.register(
        Tool(
            name="will_interrupt",
            description="Second tool that interrupts",
            parameters={"type": "object", "properties": {}},
            func=will_interrupt,
            permission=PermissionLevel.SAFE,
        )
    )
    reg.register(
        Tool(
            name="unreached_tool",
            description="Third tool that should not be reached",
            parameters={"type": "object", "properties": {}},
            func=lambda: "unreached",
            permission=PermissionLevel.SAFE,
        )
    )

    # Assistant issues 3 tool calls in a single turn
    provider = FakeProvider([
        LLMResponse(
            tool_calls=[
                ToolCall(name="first_success", args={}),
                ToolCall(name="will_interrupt", args={}),
                ToolCall(name="unreached_tool", args={}),
            ]
        ),
    ])

    agent = Agent(provider=provider, registry=reg)
    reply = agent.chat("batch with interrupt")
    assert reply == "Task cancelled."

    # Validate that all 3 tool calls have corresponding tool messages
    _verify_history_consistency(agent.history)

    # Find the assistant turn with tool_calls
    assistant_msg = [m for m in agent.history if m.get("role") == "assistant" and "tool_calls" in m][0]
    assistant_idx = agent.history.index(assistant_msg)
    tool_msgs = agent.history[assistant_idx + 1:assistant_idx + 4]

    assert len(tool_msgs) == 3
    assert tool_msgs[0]["name"] == "first_success"
    assert tool_msgs[0]["content"] == "success"
    assert tool_msgs[1]["name"] == "will_interrupt"
    assert tool_msgs[1]["content"] == "ERROR: not executed (task stopped)"
    assert tool_msgs[2]["name"] == "unreached_tool"
    assert tool_msgs[2]["content"] == "ERROR: not executed (task stopped)"


def test_7_crashing_on_event_callback_does_not_break_run():
    """(7) A crashing on_event callback does not break the agent execution."""

    def broken_event_handler(event: dict):
        raise RuntimeError("Crash inside consumer telemetry handler!")

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="simple_tool",
            description="Simple tool",
            parameters={"type": "object", "properties": {}},
            func=lambda: "tool success",
            permission=PermissionLevel.SAFE,
        )
    )

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="simple_tool", args={})]),
        LLMResponse(text="Completed successfully despite event error."),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        on_event=broken_event_handler,
    )

    reply = agent.chat("do work")
    assert reply == "Completed successfully despite event error."
    assert agent.last_run is not None
    assert agent.last_run.status == RunStatus.COMPLETED


def test_8_last_run_written_to_run_logger(tmp_path: Path):
    """(8) last_run is written to logs/runs.jsonl (tmp_path)."""
    log_path = tmp_path / "logs" / "runs.jsonl"
    run_logger = RunLogger(path=str(log_path))

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="greet",
            description="Greet tool",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}},
            func=lambda name: f"Hello, {name}!",
            permission=PermissionLevel.SAFE,
        )
    )

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="greet", args={"name": "Alice"})]),
        LLMResponse(text="Alice has been greeted."),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        run_logger=run_logger,
    )

    reply = agent.chat("Say hello to Alice")
    assert reply == "Alice has been greeted."
    assert agent.last_run is not None
    assert agent.last_run.status == RunStatus.COMPLETED

    assert log_path.exists(), "logs/runs.jsonl was not created"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 1
    record = lines[0]

    assert record["goal"] == "Say hello to Alice"
    assert record["status"] == "COMPLETED"
    assert record["final_text"] == "Alice has been greeted."
    assert len(record["steps"]) == 1
    assert record["steps"][0]["tool"] == "greet"
    assert record["steps"][0]["status"] == "success"
