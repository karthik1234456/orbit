"""Tests for Agent functionality."""

from typing import Optional
from core.agent import Agent
from core.llm import LLMProvider, LLMResponse
from tools.registry import Tool


class FakeProvider(LLMProvider):
    """Test LLM provider returning a predetermined string and capturing inputs."""

    def __init__(self, fixed_response: str = "Test response"):
        self.fixed_response = fixed_response
        self.last_messages: list[dict] = []
        self.last_tools: Optional[list[Tool]] = None
        self.call_count: int = 0

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
    ) -> LLMResponse:
        self.last_messages = list(messages)
        self.last_tools = tools
        self.call_count += 1
        return LLMResponse(text=self.fixed_response)


def test_chat_returns_reply():
    """Verify chat returns the expected reply from the provider."""
    provider = FakeProvider(fixed_response="Expected answer")
    agent = Agent(provider=provider)
    reply = agent.chat("Hello")
    assert reply == "Expected answer"


def test_history_grows_by_two_per_turn():
    """Verify history expands by 2 items (user + assistant) on each interaction."""
    provider = FakeProvider()
    agent = Agent(provider=provider)
    assert len(agent.history) == 0

    agent.chat("Turn 1")
    assert len(agent.history) == 2
    assert agent.history[0] == {"role": "user", "content": "Turn 1"}
    assert agent.history[1] == {"role": "assistant", "content": provider.fixed_response}

    agent.chat("Turn 2")
    assert len(agent.history) == 4


def test_only_last_20_messages_sent_to_provider():
    """Verify the agent bounds provider context to only the last 20 messages."""
    provider = FakeProvider()
    agent = Agent(provider=provider)

    # 15 turns will produce 30 total messages in agent.history
    for i in range(15):
        agent.chat(f"Turn {i}")

    # Provider must have received at most 20 messages in the final call
    assert len(provider.last_messages) == 20
    # The last message sent in that batch was the user's message for Turn 14
    assert provider.last_messages[-1] == {"role": "user", "content": "Turn 14"}
    # Verify exact match with the history slice sent
    assert provider.last_messages == agent.history[-21:-1]


def test_reset_clears_history():
    """Verify reset clears the conversation history."""
    provider = FakeProvider()
    agent = Agent(provider=provider)
    agent.chat("Message before reset")
    assert len(agent.history) == 2

    agent.reset()
    assert len(agent.history) == 0


class ScriptedProvider(LLMProvider):
    """Test provider returning pre-scripted LLMResponse items."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
        on_delta=None,
        should_stop=None,
    ) -> LLMResponse:
        self.call_history.append(list(messages))
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="Fallback")


def test_empty_reply_retries_once_and_succeeds(tmp_path):
    """A scripted provider that returns "" then "ok" yields "ok" with one retry recorded."""
    from security.audit import AuditLogger

    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    provider = ScriptedProvider([
        LLMResponse(text=""),
        LLMResponse(text="ok"),
    ])

    agent = Agent(provider=provider, audit=audit)
    reply = agent.chat("Hello")

    assert reply == "ok"
    assert agent.last_run.llm_calls == 2

    # Check run trace
    steps = agent.last_run.steps
    assert len(steps) == 1
    assert steps[0].tool == "empty_reply"
    assert steps[0].status == "retried"

    # Check audit log
    entries = audit.read_all()
    empty_entries = [e for e in entries if e.get("decision") == "empty_reply"]
    assert len(empty_entries) == 1
    assert empty_entries[0]["outcome"] == "retried"

    # Check system note in second provider call
    assert len(provider.call_history) == 2
    retry_messages = provider.call_history[1]
    sys_content = next((m["content"] for m in retry_messages if m.get("role") == "system"), "")
    assert "Your previous reply was empty. Answer the user directly in one or two sentences." in sys_content


def test_empty_reply_twice_returns_fixed_message(tmp_path):
    """A scripted provider that returns "" twice yields the fixed message and records fallback."""
    from security.audit import AuditLogger

    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    provider = ScriptedProvider([
        LLMResponse(text=""),
        LLMResponse(text=""),
    ])

    agent = Agent(provider=provider, audit=audit)
    reply = agent.chat("Hello")

    expected_fixed = "I couldn't produce an answer. Please rephrase or try again."
    assert reply == expected_fixed
    assert agent.last_run.llm_calls == 2

    # Check run trace: both retried and fallback recorded
    steps = agent.last_run.steps
    assert len(steps) == 2
    assert steps[0].tool == "empty_reply"
    assert steps[0].status == "retried"
    assert steps[1].tool == "empty_reply"
    assert steps[1].status == "fallback"

    # Check audit log: both retried and fallback recorded
    entries = audit.read_all()
    empty_entries = [e for e in entries if e.get("decision") == "empty_reply"]
    assert len(empty_entries) == 2
    assert empty_entries[0]["outcome"] == "retried"
    assert empty_entries[1]["outcome"] == "fallback"


def test_non_empty_reply_triggers_no_retry(tmp_path):
    """A non-empty reply triggers no retry."""
    from security.audit import AuditLogger

    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    provider = ScriptedProvider([
        LLMResponse(text="Direct answer"),
    ])

    agent = Agent(provider=provider, audit=audit)
    reply = agent.chat("Hello")

    assert reply == "Direct answer"
    assert agent.last_run.llm_calls == 1
    assert len(agent.last_run.steps) == 0
    assert not any(e.get("decision") == "empty_reply" for e in audit.read_all())


def test_empty_after_tool_call_is_not_retried(tmp_path):
    """Empty after a tool call is not retried."""
    from core.llm import ToolCall
    from security.audit import AuditLogger
    from security.permissions import PermissionLevel
    from tools.registry import Tool, ToolRegistry

    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            func=lambda: "done",
            permission=PermissionLevel.SAFE,
        )
    )

    provider = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="test_tool", args={})]),
        LLMResponse(text=""),
    ])

    agent = Agent(provider=provider, registry=registry, audit=audit)
    reply = agent.chat("Run test")

    assert reply == ""
    assert agent.last_run.llm_calls == 2
    assert not any(s.tool == "empty_reply" for s in agent.last_run.steps)
    assert not any(e.get("decision") == "empty_reply" for e in audit.read_all())


def test_whitespace_only_counts_as_empty(tmp_path):
    """Whitespace-only counts as empty and triggers retry."""
    from security.audit import AuditLogger

    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    provider = ScriptedProvider([
        LLMResponse(text="   \n\t  "),
        LLMResponse(text="ok"),
    ])

    agent = Agent(provider=provider, audit=audit)
    reply = agent.chat("Hello")

    assert reply == "ok"
    assert agent.last_run.llm_calls == 2
    assert len(agent.last_run.steps) == 1
    assert agent.last_run.steps[0].status == "retried"
    empty_entries = [e for e in audit.read_all() if e.get("decision") == "empty_reply"]
    assert len(empty_entries) == 1
    assert empty_entries[0]["outcome"] == "retried"

