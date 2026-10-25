"""Evaluation LLM provider implementations for mock replay, call counting, and noop testing."""

from typing import Any, Callable, Optional

from core.llm import LLMProvider, LLMResponse, ToolCall


class GoldenProvider(LLMProvider):
    """Replays scripted golden responses and tool calls for deterministic evaluation runs."""

    def __init__(self, case: Any) -> None:
        self.case = case
        self.turn_index = -1
        self.call_in_turn = 0

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        """Advance turns when a new user message arrives and return scripted tool calls and final text."""
        # Detect new user turn
        if messages and messages[-1].get("role") == "user":
            self.turn_index += 1
            self.call_in_turn = 0

        golden = getattr(self.case, "golden", []) or []
        if self.turn_index < 0 or self.turn_index >= len(golden):
            return LLMResponse(text="Done.", tool_calls=[])

        turn_golden = golden[self.turn_index]
        calls = turn_golden.get("calls", [])
        final_text = turn_golden.get("final", "")

        # If turn has tool calls and this is the first generate call in this turn
        if calls and self.call_in_turn == 0:
            self.call_in_turn += 1
            tool_calls = [
                ToolCall(
                    name=c["tool"],
                    args=c.get("args", {}),
                    id=f"call_{self.turn_index}_{idx}",
                )
                for idx, c in enumerate(calls)
            ]
            return LLMResponse(text="", tool_calls=tool_calls)

        # Subsequent call or turn with no tool calls
        self.call_in_turn += 1
        return LLMResponse(text=final_text, tool_calls=[])


class CountingProvider(LLMProvider):
    """Wraps an LLMProvider and records the total number of generate invocations."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.calls = 0

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        self.calls += 1
        return self.inner.generate(
            messages,
            tools=tools,
            on_delta=on_delta,
            should_stop=should_stop,
        )


class NoopProvider(LLMProvider):
    """Always returns a static completion without executing any tools."""

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        return LLMResponse(text="Done.", tool_calls=[])
