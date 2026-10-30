"""Guarded typing and dictation tool for allowlisted desktop applications.

Types approved, sanitized plain text into allowlisted windows one character at a time
using Windows SendInput with KEYEVENTF_UNICODE, chunked foreground re-verification,
and strict rate limiting.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Callable, Optional

from config import settings
from config.apps import ALLOWED_APPS, TYPING_ALLOWLIST
from config.paths import is_demo_mode
from security.permissions import PermissionLevel
from system.winwindow import (
    WindowItem,
    enumerate_windows,
    focus as win_focus,
    foreground_window,
    is_elevated_target,
)
from tools.registry import Tool

MAX_TYPING_ACTIONS_PER_10_MIN: int = 5
MAX_CHARS: int = 300
CHUNK_SIZE: int = 20
MAX_CHARS_PER_SEC: float = 30.0

# Regex for control characters and suspicious unicode formatting
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BIDI_ZERO_WIDTH = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069]")


class TypingRateLimiter:
    """Sliding-window rate limiter enforcing at most 5 typing actions per 10 minutes."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self.clock = clock or time.monotonic
        self._actions: list[float] = []

    def check(self) -> Optional[str]:
        now = self.clock()
        cutoff = now - 600.0  # 10 minutes
        self._actions = [t for t in self._actions if t > cutoff]
        if len(self._actions) >= MAX_TYPING_ACTIONS_PER_10_MIN:
            return f"Rate limit reached: at most {MAX_TYPING_ACTIONS_PER_10_MIN} typing actions per 10 minutes."
        return None

    def record(self) -> None:
        self._actions.append(self.clock())


def sanitize_typing_text(text: str) -> str:
    """Sanitize text for typing: collapse whitespace, remove control/newlines/tabs/bidi."""
    if not text:
        return ""
    # 1. Remove bidi override and zero-width chars
    cleaned = _BIDI_ZERO_WIDTH.sub("", text)
    # 2. Remove control characters (including newline and tab)
    cleaned = _CONTROL_CHARS.sub(" ", cleaned)
    # 3. Collapse multiple whitespace into single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 4. Cap at 300 chars
    return cleaned[:MAX_CHARS]


def _real_send_char(ch: str) -> None:
    """Send a single character via SendInput with KEYEVENTF_UNICODE."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    INPUT_KEYBOARD = 1
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_ulong),
        ]

    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _anonymous_ = ("_input",)
        _fields_ = [("type", wintypes.DWORD), ("_input", _INPUT)]

    code = ord(ch)
    inp_down = INPUT(type=INPUT_KEYBOARD)
    inp_down.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)

    inp_up = INPUT(type=INPUT_KEYBOARD)
    inp_up.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)

    inputs = (INPUT * 2)(inp_down, inp_up)
    user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))


def make_typing_tools(
    enumerator: Optional[Callable[[], list[WindowItem]]] = None,
    focus_fn: Optional[Callable[[int], bool]] = None,
    foreground_fn: Optional[Callable[[], tuple[Optional[str], Optional[str]]]] = None,
    char_sender: Optional[Callable[[str], None]] = None,
    is_elevated_fn: Optional[Callable[[int], bool]] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    typing_enabled_getter: Optional[Callable[[], bool]] = None,
) -> list[Tool]:
    """Create and return the type_into_app tool."""
    get_windows = enumerator or enumerate_windows
    do_focus = focus_fn or win_focus
    get_fg = foreground_fn or foreground_window
    actual_sender = char_sender or _real_send_char
    check_elevated = is_elevated_fn or is_elevated_target
    do_sleep = sleep or time.sleep
    rate_limiter = TypingRateLimiter(clock=clock or time.monotonic)

    def _is_enabled() -> bool:
        if is_demo_mode():
            return False
        if typing_enabled_getter is not None:
            return bool(typing_enabled_getter())
        return bool(getattr(settings, "TYPING_ENABLED", False))

    def _extract_args(pos_args: tuple, kwargs: dict) -> dict[str, Any]:
        if pos_args and isinstance(pos_args[0], dict):
            return pos_args[0]
        return dict(kwargs)

    def _check_app_allowlist(app_key: str) -> tuple[bool, str]:
        clean_key = str(app_key).strip().lower()
        allowed = {k.lower() for k in TYPING_ALLOWLIST}
        if clean_key not in allowed:
            allowed_names = ", ".join(sorted(list(allowed)))
            return False, f"I only type into {allowed_names}."
        return True, clean_key

    def _typing_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Typing is off. Turn on TYPING_ENABLED in Settings."

        args = _extract_args(pos_args, kwargs)
        app_name = str(args.get("app", "")).strip()
        ok_app, err_msg = _check_app_allowlist(app_name)
        if not ok_app:
            return err_msg

        raw_text = str(args.get("text", ""))
        sanitized = sanitize_typing_text(raw_text)
        if not sanitized:
            return "Text is empty or contains only invalid characters."
        if len(sanitized) > MAX_CHARS:
            return f"Text too long ({len(sanitized)} characters). Maximum is {MAX_CHARS}."

        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        return None

    def _typing_describe(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        app_name = str(args.get("app", "")).strip()
        raw_text = str(args.get("text", ""))
        sanitized = sanitize_typing_text(raw_text)
        return f"Type into {app_name}: \"{sanitized}\""

    def _typing_exec(*pos_args: Any, **kwargs: Any) -> str:
        if not _is_enabled():
            return "Typing is off. Turn on TYPING_ENABLED in Settings."

        args = _extract_args(pos_args, kwargs)
        app_name = str(args.get("app", "")).strip()
        ok_app, err_msg = _check_app_allowlist(app_name)
        if not ok_app:
            return err_msg

        raw_text = str(args.get("text", ""))
        sanitized = sanitize_typing_text(raw_text)
        if not sanitized:
            return "Text is empty or contains only invalid characters."

        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        # Resolve window for app
        app_info = ALLOWED_APPS.get(app_name.lower())
        if not app_info:
            return f"I don't recognize '{app_name}' as an allowed application."

        allowed_executables = app_info.get("windows") or []
        allowed_set = {exe.lower() for exe in allowed_executables}
        if not allowed_set:
            return f"Application '{app_name}' does not have a configured window process list."

        # Find topmost matching window
        windows = get_windows()
        target_win: Optional[WindowItem] = None
        for w in windows:
            if w.process and w.process.lower() in allowed_set:
                target_win = w
                break

        if target_win is None:
            return f"I can't find an open {app_name.capitalize()} window."

        if check_elevated(target_win.handle):
            return "I can't control that window because it runs with administrator rights."

        # 1. Focus app window
        focused = do_focus(target_win.handle)
        if not focused:
            return f"Windows did not let me bring {app_name.capitalize()} to the front."

        # 2. Verify foreground process is the target app
        _, fg_proc = get_fg()
        if not fg_proc or fg_proc.lower() not in allowed_set:
            return f"Stopped typing because {app_name.capitalize()} is not in the foreground."

        rate_limiter.record()

        # 3. Type character-by-character in chunks of CHUNK_SIZE
        # Re-verify foreground process before every chunk
        char_delay = 1.0 / MAX_CHARS_PER_SEC
        typed_count = 0

        for chunk_start in range(0, len(sanitized), CHUNK_SIZE):
            # Re-verify foreground process before every chunk
            _, current_fg = get_fg()
            if not current_fg or current_fg.lower() not in allowed_set:
                return "Stopped typing because the window changed."

            chunk = sanitized[chunk_start : chunk_start + CHUNK_SIZE]
            for ch in chunk:
                actual_sender(ch)
                typed_count += 1
                do_sleep(char_delay)

        return f"Typed {typed_count} characters into {app_name.capitalize()}; I can't see the result."

    type_tool = Tool(
        name="type_into_app",
        description="Type approved text into an allowlisted application window without pressing Enter.",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Allowlisted application key."},
                "text": {"type": "string", "description": "Sanitized text to type (1 to 300 characters)."},
            },
            "required": ["app", "text"],
        },
        func=_typing_exec,
        permission=PermissionLevel.CONFIRM,
        precheck=_typing_precheck,
        describe=_typing_describe,
        model_visible=False,
        block_when_tainted=True,
    )

    return [type_tool]

