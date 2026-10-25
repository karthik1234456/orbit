"""Tests for Agent memory integration and persistence."""

from pathlib import Path
from typing import Optional

from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from memory.store import MemoryStore
from security.permissions import PermissionPolicy
from tools.memory_tools import make_memory_tools
from tools.registry import Tool, ToolRegistry


class FakeProvider(LLMProvider):
    """Test LLM provider that records all received messages and tools."""

    def __init__(
        self,
        responses: Optional[list[LLMResponse]] = None,
        default_text: str = "Fake response",
    ):
        self.responses = list(responses) if responses else []
        self.default_text = default_text
        self.call_history: list[list[dict]] = []
        self.last_messages: list[dict] = []
        self.last_tools: Optional[list[Tool]] = None
        self.call_count: int = 0

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
    ) -> LLMResponse:
        self.last_messages = [dict(m) for m in messages]
        self.call_history.append([dict(m) for m in messages])
        self.last_tools = tools
        self.call_count += 1

        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text=self.default_text)


def test_memory_block_passed_when_stored_memory_matches(tmp_path: Path):
    """Verify that the memory block is included as additional system text when memory matches user text."""
    store = MemoryStore(path=tmp_path / "memory.db")
    store.add("User prefers Python over JavaScript", category="preference")

    provider = FakeProvider(default_text="Understood.")
    agent = Agent(
        provider=provider,
        system_prompt="You are ORBIT.",
        memory_store=store,
    )

    agent.chat("Write a Python script for me")

    assert len(provider.last_messages) > 0
    # First message should be system message containing system prompt and memory block
    first_msg = provider.last_messages[0]
    assert first_msg["role"] == "system"
    assert "You are ORBIT." in first_msg["content"]
    assert "saved notes about the user" in first_msg["content"]
    assert "User prefers Python over JavaScript" in first_msg["content"]


def test_memory_block_absent_when_nothing_matches(tmp_path: Path):
    """Verify that no memory block or system message is added when no stored memory matches user text."""
    store = MemoryStore(path=tmp_path / "memory.db")
    store.add("User prefers dark mode", category="preference")

    provider = FakeProvider(default_text="Hello there.")
    agent = Agent(
        provider=provider,
        system_prompt="You are ORBIT.",
        memory_store=store,
    )

    agent.chat("Tell me about quantum computing")

    # With no matches, memory_block is empty; agent behaves as before (no system message injected)
    for msg in provider.last_messages:
        assert msg.get("role") != "system"
        assert "Relevant notes" not in msg.get("content", "")
        assert "User prefers dark mode" not in msg.get("content", "")


def test_memory_block_never_appears_in_agent_history(tmp_path: Path):
    """Verify that the memory block never enters agent.history across multiple turns."""
    store = MemoryStore(path=tmp_path / "memory.db")
    store.add("User lives in Seattle", category="fact")

    provider = FakeProvider(default_text="Seattle is great!")
    agent = Agent(
        provider=provider,
        system_prompt="You are ORBIT.",
        memory_store=store,
    )

    agent.chat("What is the weather like in Seattle?")

    # agent.history must contain only user and assistant messages
    assert len(agent.history) == 2
    assert agent.history[0] == {"role": "user", "content": "What is the weather like in Seattle?"}
    assert agent.history[1] == {"role": "assistant", "content": "Seattle is great!"}

    for msg in agent.history:
        assert msg["role"] != "system"
        assert "Relevant notes" not in msg.get("content", "")
        assert "User lives in Seattle" not in msg.get("content", "")


def test_agent_without_store_works_as_before():
    """Verify that Agent without a memory_store retains exact previous behavior."""
    provider = FakeProvider(default_text="Normal response")
    agent = Agent(
        provider=provider,
        system_prompt="You are ORBIT.",
        memory_store=None,
    )

    reply = agent.chat("Hello ORBIT")

    assert reply == "Normal response"
    assert len(agent.history) == 2
    assert agent.history[0] == {"role": "user", "content": "Hello ORBIT"}
    assert agent.history[1] == {"role": "assistant", "content": "Normal response"}

    # Provider received exactly the user message without extra system text
    assert provider.last_messages == [{"role": "user", "content": "Hello ORBIT"}]
    assert provider.last_messages == agent.history[:1]


def test_remember_fact_denied_by_confirm_callback_stores_nothing(tmp_path: Path):
    """Verify that if the user denies remember_fact via confirm_callback, nothing is saved to memory."""
    store = MemoryStore(path=tmp_path / "memory.db")
    registry = ToolRegistry()
    for tool in make_memory_tools(store):
        registry.register(tool)

    policy = PermissionPolicy()
    provider = FakeProvider(
        responses=[
            LLMResponse(
                text="I will remember that.",
                tool_calls=[
                    ToolCall(
                        name="remember_fact",
                        args={"content": "My secret code is xyz", "category": "fact"},
                    )
                ],
            ),
            LLMResponse(text="Action was not approved."),
        ]
    )

    # Deny callback
    agent = Agent(
        provider=provider,
        registry=registry,
        policy=policy,
        confirm_callback=lambda tool_name, args: False,
        memory_store=store,
    )

    agent.chat("Remember my secret code")

    assert store.count() == 0
    assert store.search("secret") == []


def test_remember_fact_approved_stores_and_persists_across_restarts(tmp_path: Path):
    """Verify that approved remember_fact saves data and a fresh MemoryStore instance sees it."""
    db_path = tmp_path / "memory.db"
    store1 = MemoryStore(path=db_path)
    registry = ToolRegistry()
    for tool in make_memory_tools(store1):
        registry.register(tool)

    policy = PermissionPolicy()
    provider = FakeProvider(
        responses=[
            LLMResponse(
                text="I'll save that preference for you.",
                tool_calls=[
                    ToolCall(
                        name="remember_fact",
                        args={"content": "User prefers tabs over spaces", "category": "preference"},
                    )
                ],
            ),
            LLMResponse(text="Saved your preference!"),
        ]
    )

    # Approve callback
    agent = Agent(
        provider=provider,
        registry=registry,
        policy=policy,
        confirm_callback=lambda tool_name, args: True,
        memory_store=store1,
    )

    agent.chat("Please remember that I prefer tabs over spaces")

    # Verify store1 has the record
    assert store1.count() == 1
    found = store1.search("tabs")
    assert len(found) == 1
    assert found[0].content == "User prefers tabs over spaces"
    assert found[0].category == "preference"

    # Verify persistence across restart with a NEW MemoryStore instance on the same path
    store2 = MemoryStore(path=db_path)
    assert store2.count() == 1
    found2 = store2.search("tabs")
    assert len(found2) == 1
    assert found2[0].content == "User prefers tabs over spaces"
    assert found2[0].category == "preference"
