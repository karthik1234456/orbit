"""Window control tools for desktop window management.

Provides safe, deterministic window control actions (focus, minimize, maximize, restore,
snap, show desktop) restricted to allowlisted applications with verified outcomes.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from config import settings
from config.apps import ALLOWED_APPS
from config.paths import is_demo_mode
from security.permissions import PermissionLevel
from system.winwindow import (
    WindowItem,
    enumerate_windows,
    focus as win_focus,
    is_elevated_target,
    maximize as win_maximize,
    minimize as win_minimize,
    restore as win_restore,
    show_desktop as win_show_desktop,
    snap as win_snap,
)
from tools.registry import Tool


class WindowRateLimiter:
    """Sliding-window rate limiter enforcing at most 30 window actions per minute."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self.clock = clock or time.monotonic
        self._actions: list[float] = []

    def check(self) -> Optional[str]:
        now = self.clock()
        cutoff = now - 60.0
        self._actions = [t for t in self._actions if t > cutoff]
        if len(self._actions) >= 30:
            return "Rate limit reached: at most 30 window actions per minute."
        return None

    def record(self) -> None:
        self._actions.append(self.clock())


def _resolve_app_target(
    app_key: str,
    enumerator: Callable[[], list[WindowItem]],
) -> tuple[Optional[str], Optional[WindowItem], Optional[str]]:
    """Resolve app key against ALLOWED_APPS and find topmost matching window in Z-order.

    Returns: (display_name, window_item, error_message)
    """
    clean_key = str(app_key).strip().lower()
    app_info = ALLOWED_APPS.get(clean_key)
    if not app_info:
        # Match case-insensitively across allowed app keys
        for k, v in ALLOWED_APPS.items():
            if k.lower() == clean_key:
                clean_key = k
                app_info = v
                break

    if not app_info:
        return (clean_key.capitalize(), None, f"I don't recognize '{clean_key}' as an allowed application.")

    allowed_executables = app_info.get("windows")
    if not allowed_executables:
        return (clean_key.capitalize(), None, f"Application '{clean_key}' does not have a configured window process list.")

    allowed_set = {exe.lower() for exe in allowed_executables}
    display_name = clean_key.capitalize()

    # Enumerate visible windows in Z-order
    windows = enumerator()
    for win in windows:
        if win.process and win.process.lower() in allowed_set:
            return (display_name, win, None)

    return (display_name, None, f"I can't find an open {display_name} window.")


def make_window_tools(
    enumerator: Optional[Callable[[], list[WindowItem]]] = None,
    focus_fn: Optional[Callable[[int], bool]] = None,
    minimize_fn: Optional[Callable[[int], bool]] = None,
    maximize_fn: Optional[Callable[[int], bool]] = None,
    restore_fn: Optional[Callable[[int], bool]] = None,
    snap_fn: Optional[Callable[[int, str], bool]] = None,
    show_desktop_fn: Optional[Callable[[], bool]] = None,
    is_elevated_fn: Optional[Callable[[int], bool]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> list[Tool]:
    """Create and return the window control tool suite."""
    get_windows = enumerator or enumerate_windows
    do_focus = focus_fn or win_focus
    do_minimize = minimize_fn or win_minimize
    do_maximize = maximize_fn or win_maximize
    do_restore = restore_fn or win_restore
    do_snap = snap_fn or win_snap
    do_show_desktop = show_desktop_fn or win_show_desktop
    check_elevated = is_elevated_fn or is_elevated_target
    rate_limiter = WindowRateLimiter(clock=clock)

    def _is_enabled() -> bool:
        if is_demo_mode():
            return False
        return bool(getattr(settings, "WINDOW_CONTROL_ENABLED", True))

    def _extract_args(pos_args: tuple, kwargs: dict) -> dict[str, Any]:
        if pos_args and isinstance(pos_args[0], dict):
            return pos_args[0]
        return dict(kwargs)

    # 1. focus_app
    def _focus_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        return rate_limiter.check()

    def _focus_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        app_name = str(args.get("app", "")).strip()
        disp, win, err = _resolve_app_target(app_name, get_windows)
        if err or win is None:
            return err or f"I can't find an open {disp} window."

        if check_elevated(win.handle):
            return "I can't control that window because it runs with administrator rights."

        rate_limiter.record()
        ok = do_focus(win.handle)
        if ok:
            return f"{disp} is now in front."
        return f"Windows did not let me bring {disp} to the front."

    focus_app_tool = Tool(
        name="focus_app",
        description="Bring an allowed application window to the foreground, restoring it if minimized.",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Allowed application key (e.g. 'notepad', 'chrome')."},
            },
            "required": ["app"],
        },
        func=_focus_exec,
        permission=PermissionLevel.SAFE,
        precheck=_focus_precheck,
        model_visible=False,
    )

    # 2. minimize_app
    def _minimize_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        return rate_limiter.check()

    def _minimize_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        app_name = str(args.get("app", "")).strip()
        disp, win, err = _resolve_app_target(app_name, get_windows)
        if err or win is None:
            return err or f"I can't find an open {disp} window."

        if check_elevated(win.handle):
            return "I can't control that window because it runs with administrator rights."

        rate_limiter.record()
        ok = do_minimize(win.handle)
        if ok:
            return f"Minimized {disp}."
        return f"Could not minimize {disp}."

    minimize_app_tool = Tool(
        name="minimize_app",
        description="Minimize an allowed application window.",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Allowed application key."},
            },
            "required": ["app"],
        },
        func=_minimize_exec,
        permission=PermissionLevel.SAFE,
        precheck=_minimize_precheck,
        model_visible=False,
    )

    # 3. maximize_app
    def _maximize_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        return rate_limiter.check()

    def _maximize_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        app_name = str(args.get("app", "")).strip()
        disp, win, err = _resolve_app_target(app_name, get_windows)
        if err or win is None:
            return err or f"I can't find an open {disp} window."

        if check_elevated(win.handle):
            return "I can't control that window because it runs with administrator rights."

        rate_limiter.record()
        ok = do_maximize(win.handle)
        if ok:
            return f"Maximized {disp}."
        return f"Could not maximize {disp}."

    maximize_app_tool = Tool(
        name="maximize_app",
        description="Maximize an allowed application window.",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Allowed application key."},
            },
            "required": ["app"],
        },
        func=_maximize_exec,
        permission=PermissionLevel.SAFE,
        precheck=_maximize_precheck,
        model_visible=False,
    )

    # 4. restore_app
    def _restore_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        return rate_limiter.check()

    def _restore_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        app_name = str(args.get("app", "")).strip()
        disp, win, err = _resolve_app_target(app_name, get_windows)
        if err or win is None:
            return err or f"I can't find an open {disp} window."

        if check_elevated(win.handle):
            return "I can't control that window because it runs with administrator rights."

        rate_limiter.record()
        ok = do_restore(win.handle)
        if ok:
            return f"Restored {disp}."
        return f"Could not restore {disp}."

    restore_app_tool = Tool(
        name="restore_app",
        description="Restore an allowed application window to normal size.",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Allowed application key."},
            },
            "required": ["app"],
        },
        func=_restore_exec,
        permission=PermissionLevel.SAFE,
        precheck=_restore_precheck,
        model_visible=False,
    )

    # 5. snap_app
    def _snap_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        args = _extract_args(pos_args, kwargs)
        side = str(args.get("side", "")).strip().lower()
        if side not in ("left", "right"):
            return "Side must be 'left' or 'right'."
        return rate_limiter.check()

    def _snap_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        side = str(args.get("side", "")).strip().lower()
        if side not in ("left", "right"):
            return "Side must be 'left' or 'right'."

        app_name = str(args.get("app", "")).strip()
        disp, win, err = _resolve_app_target(app_name, get_windows)
        if err or win is None:
            return err or f"I can't find an open {disp} window."

        if check_elevated(win.handle):
            return "I can't control that window because it runs with administrator rights."

        rate_limiter.record()
        ok = do_snap(win.handle, side)
        if ok:
            return f"Snapped {disp} to the {side} half."
        return f"Could not snap {disp} to the {side} half."

    snap_app_tool = Tool(
        name="snap_app",
        description="Snap an allowed application window to the left or right half of the monitor.",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Allowed application key."},
                "side": {"type": "string", "enum": ["left", "right"], "description": "Monitor side ('left' or 'right')."},
            },
            "required": ["app", "side"],
        },
        func=_snap_exec,
        permission=PermissionLevel.SAFE,
        precheck=_snap_precheck,
        model_visible=False,
    )

    # 6. show_desktop
    def _show_desktop_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        return rate_limiter.check()

    def _show_desktop_exec(*pos_args: Any, **kwargs: Any) -> str:
        if not _is_enabled():
            return "Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings."
        limit_err = rate_limiter.check()
        if limit_err:
            return limit_err

        rate_limiter.record()
        do_show_desktop()
        return "Showed the desktop."

    show_desktop_tool = Tool(
        name="show_desktop",
        description="Minimize all windows to reveal the desktop via Win+D.",
        parameters={"type": "object", "properties": {}},
        func=_show_desktop_exec,
        permission=PermissionLevel.SAFE,
        precheck=_show_desktop_precheck,
        model_visible=False,
    )

    return [
        focus_app_tool,
        minimize_app_tool,
        maximize_app_tool,
        restore_app_tool,
        snap_app_tool,
        show_desktop_tool,
    ]

