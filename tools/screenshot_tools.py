"""Screenshot capture tool with rate limiting, storage budgeting, and disk hygiene.

Captures the current desktop screen via Pillow ImageGrab and saves it directly into
workspace/screenshots/ with timestamped filenames. Images are write-only and never read back.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Optional

from config import settings
from config.paths import get_workspace_dir, is_demo_mode
from security.permissions import PermissionLevel
from tools.registry import Tool

MIN_FREE_DISK_BYTES: int = 200 * 1024 * 1024  # 200 MB
MAX_SCREENSHOTS_PER_HOUR: int = 20


class ScreenshotRateLimiter:
    """Sliding-window rate limiter enforcing at most 20 screenshots per hour."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self.clock = clock or time.monotonic
        self._shots: list[float] = []

    def check(self) -> Optional[str]:
        now = self.clock()
        cutoff = now - 3600.0
        self._shots = [t for t in self._shots if t > cutoff]
        if len(self._shots) >= MAX_SCREENSHOTS_PER_HOUR:
            return f"Rate limit reached: at most {MAX_SCREENSHOTS_PER_HOUR} screenshots per hour."
        return None

    def record(self) -> None:
        self._shots.append(self.clock())


def default_grabber() -> Any:
    """Grab screenshot using Pillow ImageGrab (imported lazily)."""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except ImportError:
        raise RuntimeError("Pillow is required for taking screenshots. Install it with: pip install pillow")


def make_screenshot_tools(
    workspace_dir_getter: Optional[Callable[[], Path]] = None,
    grabber: Optional[Callable[[], Any]] = None,
    disk_free_getter: Optional[Callable[[Path], int]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> list[Tool]:
    """Create and return the take_screenshot tool."""
    get_ws = workspace_dir_getter or get_workspace_dir
    actual_grab = grabber or default_grabber
    get_time = clock or time.time
    rate_limiter = ScreenshotRateLimiter(clock=clock or time.monotonic)

    def _default_disk_free(p: Path) -> int:
        try:
            return shutil.disk_usage(str(p)).free
        except Exception:
            return MIN_FREE_DISK_BYTES + 1

    get_disk_free = disk_free_getter or _default_disk_free

    def _is_enabled() -> bool:
        if is_demo_mode():
            return False
        return bool(getattr(settings, "SCREENSHOTS_ENABLED", True))

    def _screenshot_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Screenshots are off. Turn on SCREENSHOTS_ENABLED in Settings."
        return rate_limiter.check()

    def _screenshot_exec(*pos_args: Any, **kwargs: Any) -> str:
        if not _is_enabled():
            return "Screenshots are off. Turn on SCREENSHOTS_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        ws = get_ws()
        target_dir = ws / "screenshots"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Cannot create screenshots directory: {exc}"

        free_bytes = get_disk_free(target_dir)
        if free_bytes < MIN_FREE_DISK_BYTES:
            return "Cannot take screenshot: low disk space (less than 200 MB free)."

        try:
            img = actual_grab()
        except RuntimeError as rerr:
            return str(rerr)
        except Exception as exc:
            return f"Screenshot capture failed: {exc}"

        rate_limiter.record()

        # Build filename strictly from clock
        ts = datetime.fromtimestamp(get_time()).strftime("%Y%m%d-%H%M%S")
        filename = f"{ts}.png"
        filepath = target_dir / filename

        try:
            img.save(str(filepath), format="PNG")
        except Exception as exc:
            return f"Failed to save screenshot: {exc}"

        return f"Saved a screenshot to workspace/screenshots/{filename}."

    screenshot_tool = Tool(
        name="take_screenshot",
        description="Capture the current desktop screen and save it to workspace/screenshots/.",
        parameters={"type": "object", "properties": {}},
        func=_screenshot_exec,
        permission=PermissionLevel.SAFE,
        precheck=_screenshot_precheck,
        model_visible=False,
    )

    return [screenshot_tool]

