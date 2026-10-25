"""Tests for Agent tool execution loop, error continuation, step limits, and history trimming."""

from typing import Optional
import pytest
from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from tools.registry import Tool, ToolRegistry


class ScriptedProvider(LLMProvider):
    """Test provider that returns pre-scripted responses in sequence."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
    ) -> LLMResponse:
        self.call_history.append(list(messages))
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="Default fallback response")


def add_numbers(a: int, b: int) -> int:
    return a + b


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="add",
            description="Add two numbers",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
            func=add_numbers,
        )
    )
    return reg


def test_tool_call_then_final_answer(registry: ToolRegistry):
    """Verify agent executes a requested tool and feeds the result back to obtain a final answer."""
    responses = [
        LLMResponse(
            text="Let me compute that.",
            tool_calls=[ToolCall(name="add", args={"a": 10, "b": 20})],
        ),
        LLMResponse(text="The answer is 30."),
    ]
    provider = ScriptedProvider(responses)
    agent = Agent(provider=provider, registry=registry)

    reply = agent.chat("What is 10 + 20?")
    assert reply == "The answer is 30."

    # Verify tool events record
    assert len(agent.last_tool_events) == 1
    assert agent.last_tool_events[0] == ("add", {"a": 10, "b": 20}, "30")

    # Verify conversational history sequence
    assert len(agent.history) == 4
    assert agent.history[0] == {"role": "user", "content": "What is 10 + 20?"}
    assert agent.history[1]["role"] == "assistant"
    assert len(agent.history[1]["tool_calls"]) == 1
    assert agent.history[2] == {"role": "tool", "name": "add", "content": "30"}
    assert agent.history[3] == {"role": "assistant", "content": "The answer is 30."}


def test_unknown_tool_returns_error_and_loop_continues(registry: ToolRegistry):
    """Verify an unknown tool returns an ERROR string to the model and the agent continues."""
    responses = [
        LLMResponse(
            text="",
            tool_calls=[ToolCall(name="missing_tool", args={"x": 1})],
        ),
        LLMResponse(text="Tool was not found, answering from knowledge."),
    ]
    provider = ScriptedProvider(responses)
    agent = Agent(provider=provider, registry=registry)

    reply = agent.chat("Use missing tool")
    assert reply == "Tool was not found, answering from knowledge."

    # Verify the error was trapped and recorded
    assert len(agent.last_tool_events) == 1
    name, args, result = agent.last_tool_events[0]
    assert name == "missing_tool"
    assert result.startswith("ERROR:")
    assert "Unknown tool 'missing_tool'" in result

    # Check that tool response message in history contains the error
    assert agent.history[2]["role"] == "tool"
    assert agent.history[2]["content"].startswith("ERROR:")


def test_loop_stops_at_max_steps(registry: ToolRegistry):
    """Verify the agent halts at max_steps if the provider keeps requesting tool calls."""
    # Endless sequence of tool calls
    responses = [
        LLMResponse(
            text=f"Step {i}",
            tool_calls=[ToolCall(name="add", args={"a": 1, "b": 1})],
        )
        for i in range(10)
    ]
    provider = ScriptedProvider(responses)
    agent = Agent(provider=provider, registry=registry, max_steps=3)

    reply = agent.chat("Infinite loop test")

    # Should have stopped after exactly 3 steps
    assert len(provider.call_history) == 3
    assert len(agent.last_tool_events) == 3
    assert "unable to complete" in reply.lower() or "allowed steps" in reply.lower()


def test_history_trimming_leaves_no_orphan_tool_message(registry: ToolRegistry):
    """Verify trimming to 20 messages strips any leading tool message whose call was sliced off."""
    provider = ScriptedProvider([LLMResponse(text="OK")])
    agent = Agent(provider=provider, registry=registry)

    # Construct history of 21 messages where index [-20] lands directly on a 'tool' message
    # Index 0: assistant
    # Index 1: tool (at -20 position when history has length 21)
    # Index 2: assistant
    # Index 3..20: 18 user messages
    agent.history = [
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [ToolCall(name="add", args={"a": 1, "b": 2})],
        },
        {"role": "tool", "name": "add", "content": "3"},
        {"role": "assistant", "content": "Result is 3"},
    ] + [{"role": "user", "content": f"msg {i}"} for i in range(18)]

    assert len(agent.history) == 21
    # Raw slice [-20:] would start with index 1 (role: 'tool')
    assert agent.history[-20]["role"] == "tool"

    context = agent._get_context_messages(limit=20)
    # The leading 'tool' message must be pruned to avoid an orphan tool response
    assert context[0]["role"] != "tool"
    assert context[0]["role"] == "assistant"
    assert len(context) == 19
