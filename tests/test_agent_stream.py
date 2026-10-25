"""Tests for streaming, cancellation, and partial history handling in core/agent.py."""

import threading
from unittest.mock import patch

from config import settings
from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.task import RunStatus
from security.permissions import PermissionLevel
from tools.registry import Tool, ToolRegistry


class FakeStreamingProvider(LLMProvider):
    """Scripted LLM provider capable of simulating token streaming, tool calls, and interruptions."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools=None,
        on_delta=None,
        should_stop=None,
    ) -> LLMResponse:
        self.call_history.append(list(messages))
        if not self.responses:
            return LLMResponse(text="Default fallback")
        resp = self.responses.pop(0)

        if resp.interrupted:
            if on_delta and resp.text:
                for word in resp.text.split(" "):
                    on_delta(word + " ")
            return resp

        if on_delta and resp.text:
            words = resp.text.split(" ")
            accumulated = []
            for i, word in enumerate(words):
                if should_stop and should_stop():
                    return LLMResponse(
                        text=" ".join(accumulated),
                        tool_calls=[],
                        interrupted=True,
                    )
                chunk = word if i == len(words) - 1 else word + " "
                accumulated.append(word)
                on_delta(chunk)
                if should_stop and should_stop():
                    return LLMResponse(
                        text=" ".join(accumulated),
                        tool_calls=[],
                        interrupted=True,
                    )

        return resp


def test_deltas_reach_on_delta_during_chat():
    """When STREAM_REPLIES is True, text deltas reach the on_delta callback."""
    provider = FakeStreamingProvider([LLMResponse(text="Here is your requested answer.")])
    agent = Agent(provider=provider)

    deltas: list[str] = []
    reply = agent.chat("What is the answer?", on_delta=deltas.append)

    assert reply == "Here is your requested answer."
    assert "".join(deltas).strip() == "Here is your requested answer."
    assert agent.last_run.status == RunStatus.COMPLETED


def test_streaming_disabled_ignores_on_delta():
    """When STREAM_REPLIES is False, on_delta is ignored and no deltas are emitted."""
    provider = FakeStreamingProvider([LLMResponse(text="Streaming is disabled here.")])
    agent = Agent(provider=provider)

    deltas: list[str] = []
    with patch.object(settings, "STREAM_REPLIES", False):
        reply = agent.chat("Hello", on_delta=deltas.append)

    assert reply == "Streaming is disabled here."
    assert len(deltas) == 0


def test_interrupted_response_saved_to_history_and_next_chat_works():
    """An interrupted response is saved to history with partial text and subsequent chat() works normally."""
    provider = FakeStreamingProvider([
        LLMResponse(text="The solar system consists of eight planets and ", interrupted=True),
        LLMResponse(text="Continuing: several dwarf planets including Pluto."),
    ])
    agent = Agent(provider=provider)

    # First turn: interrupted
    partial_reply = agent.chat("Tell me about the solar system")
    assert partial_reply == "The solar system consists of eight planets and "
    assert agent.last_run.status == RunStatus.CANCELLED

    # Check history has the partial assistant text
    assert len(agent.history) == 2
    assert agent.history[0] == {"role": "user", "content": "Tell me about the solar system"}
    assert agent.history[1] == {"role": "assistant", "content": "The solar system consists of eight planets and "}

    # Second turn: regular continuation
    second_reply = agent.chat("Continue please")
    assert second_reply == "Continuing: several dwarf planets including Pluto."
    assert agent.last_run.status == RunStatus.COMPLETED

    # Verify conversation history structure remains completely valid
    assert len(agent.history) == 4
    roles = [m["role"] for m in agent.history]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_cancel_event_before_tool_call_prevents_execution_and_matches_history():
    """cancel_event set before a tool call prevents execution and leaves no orphan tool calls."""
    registry = ToolRegistry()
    executed_tools: list[str] = []

    registry.register(
        Tool(
            name="file_create",
            description="Create file",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}},
            func=lambda name: executed_tools.append(name) or f"Created {name}",
            permission=PermissionLevel.SAFE,
        )
    )

    provider = FakeStreamingProvider([
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(name="file_create", args={"name": "file1.txt"}),
                ToolCall(name="file_create", args={"name": "file2.txt"}),
            ],
        )
    ])
    agent = Agent(provider=provider, registry=registry)

    cancel_event = threading.Event()
    cancel_event.set()  # Cancelled before any tool runs

    reply = agent.chat("create two files", cancel_event=cancel_event)

    # Verify tools were never executed
    assert len(executed_tools) == 0
    assert reply == "(interrupted)"
    assert agent.last_run.status == RunStatus.CANCELLED

    # Verify history integrity: every tool_call in assistant messages has a matching tool message
    assistant_msgs = [m for m in agent.history if m.get("role") == "assistant" and "tool_calls" in m]
    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]

    total_expected_calls = sum(len(m.get("tool_calls", [])) for m in assistant_msgs)
    assert total_expected_calls == 2
    assert len(tool_msgs) == 2
    for tm in tool_msgs:
        assert tm["content"] == "ERROR: not executed (task stopped)"

    # Final assistant message is "(interrupted)"
    assert agent.history[-1] == {"role": "assistant", "content": "(interrupted)"}


def test_no_streaming_behavior_unchanged():
    """Standard chat() without on_delta or cancel_event behaves identically to original implementation."""
    provider = FakeStreamingProvider([LLMResponse(text="Normal answer without streaming.")])
    agent = Agent(provider=provider)

    reply = agent.chat("What is 1+1?")
    assert reply == "Normal answer without streaming."
    assert agent.last_run.status == RunStatus.COMPLETED
    assert len(agent.history) == 2
