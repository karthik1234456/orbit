"""Power control tool for placing the computer into sleep mode safely.

Guarded by user confirmation dialog, block_when_tainted=True, rate limiting
(at most 2 per 10 minutes), and setting SLEEP_ENABLED (default False).
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Optional

from config import settings
from config.paths import is_demo_mode
from security.permissions import PermissionLevel
from tools.registry import Tool

MAX_SLEEP_ACTIONS_PER_10_MIN: int = 2


class SleepRateLimiter:
    """Sliding-window rate limiter enforcing at most 2 sleep actions per 10 minutes."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self.clock = clock or time.monotonic
        self._actions: list[float] = []

    def check(self) -> Optional[str]:
        now = self.clock()
        cutoff = now - 600.0  # 10 minutes
        self._actions = [t for t in self._actions if t > cutoff]
        if len(self._actions) >= MAX_SLEEP_ACTIONS_PER_10_MIN:
            return f"Rate limit reached: at most {MAX_SLEEP_ACTIONS_PER_10_MIN} sleep actions per 10 minutes."
        return None

    def record(self) -> None:
        self._actions.append(self.clock())


def default_suspend(hibernate: bool = False, force: bool = False, disable_wake_event: bool = False) -> bool:
    """Put Windows to sleep via SetSuspendState."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.powrprof.SetSuspendState(hibernate, force, disable_wake_event))
    except Exception:
        return False


def make_power_tools(
    suspend_fn: Optional[Callable[[bool, bool, bool], bool]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> list[Tool]:
    """Create and return the sleep_computer tool."""
    actual_suspend = suspend_fn or default_suspend
    rate_limiter = SleepRateLimiter(clock=clock or time.monotonic)

    def _is_enabled() -> bool:
        if is_demo_mode():
            return False
        return bool(getattr(settings, "SLEEP_ENABLED", False))

    def _sleep_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Sleep is off. Turn on SLEEP_ENABLED in Settings."
        return rate_limiter.check()

    def _sleep_describe(*pos_args: Any, **kwargs: Any) -> str:
        return "Put this computer to sleep now?"

    def _sleep_exec(*pos_args: Any, **kwargs: Any) -> str:
        if not _is_enabled():
            return "Sleep is off. Turn on SLEEP_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        rate_limiter.record()
        ok = actual_suspend(False, False, False)
        if ok:
            return "Putting the computer to sleep."
        return "Windows did not put the computer to sleep."

    sleep_tool = Tool(
        name="sleep_computer",
        description="Put this computer to sleep safely. Requires confirmation.",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        func=_sleep_exec,
        permission=PermissionLevel.CONFIRM,
        precheck=_sleep_precheck,
        describe=_sleep_describe,
        model_visible=False,
        block_when_tainted=True,
    )

    return [sleep_tool]

