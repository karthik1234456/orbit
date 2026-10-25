"""Tests for scripts/local_check.py diagnostic script without network or Ollama calls."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from core.llm import LLMProvider, LLMResponse, ToolCall
import scripts.local_check as local_check
from security.permissions import PermissionLevel
from tools.registry import Tool


class FakeOllamaProvider(LLMProvider):
    """Fake Ollama provider that records calls and returns canned responses without network requests."""

    def __init__(self, responses: Optional[list[LLMResponse]] = None) -> None:
        self.responses = list(responses) if responses is not None else []
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
        on_delta: Optional[Any] = None,
        should_stop: Optional[Any] = None,
    ) -> LLMResponse:
        self.calls.append({
            "messages": messages,
            "tools": tools,
        })
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(
            text="",
            tool_calls=[ToolCall(name="get_weather", args={"city": "Paris"})],
        )


def test_dummy_weather_tool_construction_and_signature():
    """Verify that the dummy weather tool can be constructed with current Tool signature."""
    tool = Tool(
        name="get_weather",
        description="Get current weather for a city",
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name",
                }
            },
            "required": ["city"],
        },
        func=lambda city="": f"Sunny in {city}",
        permission=PermissionLevel.SAFE,
    )

    assert tool.name == "get_weather"
    assert tool.permission == PermissionLevel.SAFE
    assert callable(tool.func)
    assert tool.func("Paris") == "Sunny in Paris"
    assert "city" in tool.parameters["properties"]


def test_check_tool_calls_constructs_tool_and_payloads():
    """Verify check_tool_calls runs with a fake provider and sends the correct payloads and tool."""
    fake_provider = FakeOllamaProvider()

    status, passes = local_check.check_tool_calls(fake_provider)

    assert status == "PASS"
    assert passes == 3
    assert len(fake_provider.calls) == 3

    # Inspect message payload and tool payload
    for call in fake_provider.calls:
        messages = call["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "What's the weather in Paris?"

        tools = call["tools"]
        assert len(tools) == 1
        tool = tools[0]
        assert isinstance(tool, Tool)
        assert tool.name == "get_weather"
        assert tool.permission == PermissionLevel.SAFE
        assert callable(tool.func)
        assert tool.func("Tokyo") == "Sunny in Tokyo"


def test_check_tool_calls_failure_mode():
    """Verify check_tool_calls handles non-matching tool responses without network calls."""
    fake_provider = FakeOllamaProvider(
        responses=[
            LLMResponse(text="It is rainy in Paris.", tool_calls=[]),
            LLMResponse(text="", tool_calls=[ToolCall(name="wrong_tool", args={"city": "Paris"})]),
            LLMResponse(text="", tool_calls=[ToolCall(name="get_weather", args={"city": "London"})]),
        ]
    )

    status, passes = local_check.check_tool_calls(fake_provider)

    assert status == "FAIL"
    assert passes == 0
    assert len(fake_provider.calls) == 3
