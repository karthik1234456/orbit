"""Guards for loop detection, error thresholds, and execution budget limits."""

import json
import time
from typing import Any, Callable, Optional

from core.task import RunStatus


class LoopGuard:
    """Monitors agent tool calling to prevent runaway loops, error storms, and step overflows."""

    def __init__(
        self,
        max_tool_calls: int = 12,
        max_consecutive_errors: int = 3,
        max_repeats: int = 2,
        max_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_tool_calls = max_tool_calls
        self.max_consecutive_errors = max_consecutive_errors
        self.max_repeats = max_repeats
        self.max_seconds = max_seconds
        self.clock = clock

        self.start_time: float = self.clock()
        self.total_tool_calls: int = 0
        self.consecutive_errors: int = 0
        self.failed_calls: dict[str, int] = {}

    @staticmethod
    def _serialize_call(tool: str, args: Any) -> str:
        """Serialize tool name and sorted args into a deterministic signature."""
        try:
            serialized_args = json.dumps(args, sort_keys=True)
        except (TypeError, ValueError):
            serialized_args = str(sorted(args.items()) if isinstance(args, dict) else str(args))
        return f"{tool}:{serialized_args}"

    def check_before(self, tool: str, args: dict) -> Optional[RunStatus]:
        """Evaluate if the proposed tool call should proceed or be stopped.

        Returns:
            RunStatus.STEP_LIMIT if total calls >= max_tool_calls or elapsed > max_seconds.
            RunStatus.LOOP_DETECTED if the exact same (tool, args) pair has already failed max_repeats times.
            None if execution is permitted to proceed.
        """
        if self.total_tool_calls >= self.max_tool_calls:
            return RunStatus.STEP_LIMIT

        elapsed = self.clock() - self.start_time
        if elapsed > self.max_seconds:
            return RunStatus.STEP_LIMIT

        key = self._serialize_call(tool, args)
        if self.failed_calls.get(key, 0) >= self.max_repeats:
            return RunStatus.LOOP_DETECTED

        return None

    def record(self, tool: str, args: dict, ok: bool) -> None:
        """Update guard counters after a tool execution attempt."""
        self.total_tool_calls += 1
        key = self._serialize_call(tool, args)

        if ok:
            self.consecutive_errors = 0
            # Succeeded call clears prior failures for this signature
            self.failed_calls.pop(key, None)
        else:
            self.consecutive_errors += 1
            self.failed_calls[key] = self.failed_calls.get(key, 0) + 1

    def should_stop_for_errors(self) -> bool:
        """Return True if consecutive errors have reached or exceeded the threshold."""
        return self.consecutive_errors >= self.max_consecutive_errors

    def reset(self) -> None:
        """Reset all counters, failure trackers, and timer."""
        self.start_time = self.clock()
        self.total_tool_calls = 0
        self.consecutive_errors = 0
        self.failed_calls.clear()
