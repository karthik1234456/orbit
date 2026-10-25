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
