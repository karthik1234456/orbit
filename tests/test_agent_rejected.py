"""Tests for rejected tool execution status, recovery hints, and turn retry blocking."""

from pathlib import Path
from typing import Any, Callable, Optional
import pytest

from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.task import StepRecord
from evals.checks import ToolEvent
from evals.metrics import CaseResult, aggregate
from evals.report import render_markdown
from evals.schema import EvalCase
from memory.store import MemoryStore
from security.audit import AuditLogger
from security.permissions import PermissionPolicy
from tools.builtin import register_builtin_tools
from tools.memory_tools import make_memory_tools
from tools.registry import ToolRegistry


class ScriptedProvider(LLMProvider):
    """Scripted LLM provider returning predefined responses in sequence."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        self.call_history.append(list(messages))
        if not self.responses:
            return LLMResponse(text="Default fallback response")
        return self.responses.pop(0)


@pytest.fixture
def agent_environment(tmp_path: Path):
    """Set up a test agent environment with MemoryStore, ToolRegistry, and AuditLogger."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)
    audit_path = tmp_path / "logs" / "audit.jsonl"
    audit = AuditLogger(path=str(audit_path))

    reg = ToolRegistry()
    for t in make_memory_tools(store):
        reg.register(t)
    register_builtin_tools(reg)

    policy = PermissionPolicy()
    return store, reg, audit, policy


def test_rejected_remember_fact_blocks_reworded_retry_in_same_turn(agent_environment):
    """A rejected remember_fact followed by a reworded remember_fact in the same turn:
    the second does not execute, the store stays empty, and error/blocked messages match.
    """
    store, reg, audit, policy = agent_environment

    provider = ScriptedProvider([
        # Step 1: LLM attempts to store sensitive password
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "my password: supersecret123"},
                    id="call_1",
                )
            ],
        ),
        # Step 2: LLM attempts reworded version of the rejected text in the same turn
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "the user master secret is supersecret123"},
                    id="call_2",
                )
            ],
        ),
        # Step 3: LLM final response
        LLMResponse(text="I cannot store passwords or reworded secrets."),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=lambda name, args, warning=None: True,
    )

    reply = agent.chat("Please remember my password supersecret123")
    assert reply == "I cannot store passwords or reworded secrets."

    # Store must remain completely empty
    assert store.count() == 0

    # Verify steps recorded in task run
    assert agent.last_run is not None
    steps = agent.last_run.steps
    assert len(steps) == 2

    # Step 1: Rejected on purpose by safety validator
    assert steps[0].tool == "remember_fact"
    assert steps[0].status == "rejected"
    assert steps[0].result.startswith("ERROR: REJECTED:")

    # Step 2: Blocked by code enforcement without executing
    assert steps[1].tool == "remember_fact"
    assert steps[1].status == "blocked"
    assert steps[1].result == "ERROR: BLOCKED: an earlier call to remember_fact was rejected in this turn; not retrying."


def test_rejected_hint_text_used(agent_environment):
    """Verify that the hint text for rejected is used and sent to the LLM."""
    store, reg, audit, policy = agent_environment

    provider = ScriptedProvider([
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "password = 123456"},
                    id="call_1",
                )
            ],
        ),
        LLMResponse(text="Rejected as requested."),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        confirm_callback=lambda name, args, warning=None: True,
    )

    agent.chat("Remember password = 123456")

    # The tool result message sent in step 2 should contain the exact recovery hint
    assert len(provider.call_history) == 2
    tool_msgs = [m for m in provider.call_history[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]

    expected_hint = (
        "This content was rejected on purpose by a safety rule. "
        "Do not retry this tool with reworded or modified content. "
        "Tell the user it was not saved and why."
    )
    assert expected_hint in content


def test_different_tool_unaffected(agent_environment):
    """Verify that after a tool returns rejected, a different tool is unaffected and executes."""
    store, reg, audit, policy = agent_environment

    provider = ScriptedProvider([
        # Step 1: rejected remember_fact
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "token = abcdefg12345678901234567890"},
                    id="call_1",
                )
            ],
        ),
        # Step 2: calculate (different tool)
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="calculate",
                    args={"expression": "50 * 2"},
                    id="call_2",
                )
            ],
        ),
        # Step 3: LLM final response
        LLMResponse(text="Calculation finished."),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        confirm_callback=lambda name, args, warning=None: True,
    )

    reply = agent.chat("Calculate after secret")
    assert reply == "Calculation finished."

    assert agent.last_run is not None
    steps = agent.last_run.steps
    assert len(steps) == 2

    # Step 1 was rejected
    assert steps[0].tool == "remember_fact"
    assert steps[0].status == "rejected"

    # Step 2 (different tool) executed successfully
    assert steps[1].tool == "calculate"
    assert steps[1].status == "success"
    assert steps[1].result == "100"


def test_rejected_resets_on_next_turn(agent_environment):
    """Verify that the rejected tool tracking resets on the next user message."""
    store, reg, audit, policy = agent_environment

    provider = ScriptedProvider([
        # Turn 1: rejected
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "password: supersecretpassword123"},
                    id="call_1",
                )
            ],
        ),
        LLMResponse(text="Password rejected."),
        # Turn 2: benign fact using the same tool
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "User prefers green tea", "category": "preference"},
                    id="call_2",
                )
            ],
        ),
        LLMResponse(text="I remembered your tea preference."),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        confirm_callback=lambda name, args, warning=None: True,
    )

    # Turn 1
    reply1 = agent.chat("Save my password")
    assert reply1 == "Password rejected."
    assert store.count() == 0

    # Turn 2: same tool, next turn
    reply2 = agent.chat("Save my tea preference")
    assert reply2 == "I remembered your tea preference."
    assert store.count() == 1
    assert agent.last_run.steps[0].status == "success"


def test_rejected_audit_entries_exist(agent_environment):
    """Verify audit entries for rejected outcome and subsequent rejected_retry decision."""
    store, reg, audit, policy = agent_environment

    provider = ScriptedProvider([
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "sk-12345678901234567890"},
                    id="call_1",
                )
            ],
        ),
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="remember_fact",
                    args={"content": "sk-12345678901234567890 reworded"},
                    id="call_2",
                )
            ],
        ),
        LLMResponse(text="Done."),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=lambda name, args, warning=None: True,
    )

    agent.chat("Save sk key")

    records = audit.read_all()
    # Filter for remember_fact tool calls
    rf_records = [r for r in records if r["tool"] == "remember_fact"]
    assert len(rf_records) == 2

    # First call: rejected by safety rule
    assert rf_records[0]["outcome"] == "rejected"

    # Second call: blocked retry attempt
    assert rf_records[1]["decision"] == "rejected_retry"
    assert rf_records[1]["outcome"] == "blocked"


def test_unknown_statuses_dont_break_report():
    """Verify that unknown, unexpected, or None statuses in CaseResult don't break reporting."""
    case = EvalCase(
        id="test_case_status_tolerance",
        category="safety",
        turns=["test prompt"],
    )

    results = [
        CaseResult(
            id="test_case_status_tolerance",
            category="safety",
            repeat=0,
            status="rejected",  # new status
            failed_checks=["refusal_language: missing"],
        ),
        CaseResult(
            id="test_case_unknown_status",
            category="safety",
            repeat=0,
            status="unrecognized_custom_status",
            failed_checks=[],
        ),
        CaseResult(
            id="test_case_none_status",
            category="safety",
            repeat=0,
            status=None,  # type: ignore
            failed_checks=[],
        ),
    ]

    # Metrics aggregation must not crash
    agg = aggregate(results, [case])
    assert agg["overall"]["n"] == 3
    assert agg["overall"]["passed"] == 0

    # Report markdown rendering must not crash
    results_dict = {
        "meta": {"mode": "mock", "model": "mock-test"},
        "results": [r.to_dict() for r in results],
    }
    markdown = render_markdown(results_dict, [case])
    assert isinstance(markdown, str)
    assert len(markdown) > 0
    assert "### `test_case_status_tolerance` - `REJECTED`" in markdown
    assert "### `test_case_unknown_status` - `UNRECOGNIZED_CUSTOM_STATUS`" in markdown
    assert "### `test_case_none_status` - `UNKNOWN`" in markdown


def test_step_record_and_tool_event_handle_rejected():
    """Verify ToolEvent and StepRecord structures explicitly support rejected status."""
    step = StepRecord(
        index=1,
        tool="remember_fact",
        args={"content": "secret"},
        status="rejected",
        result="ERROR: REJECTED: Sensitive content",
        duration_ms=15,
    )
    assert step.status == "rejected"

    event = ToolEvent(
        tool="remember_fact",
        args={"content": "secret"},
        status="rejected",
        turn=0,
    )
    assert event.status == "rejected"
