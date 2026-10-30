"""Windows system control tools for volume, media keys, screen lock, and settings pages.

All operations execute through injectable callables with strict validation, trigger-first descriptions,
per-minute rate limits, honest result strings, and taint-escalation flags.
Never executes external shells, child processes, or command lines.
"""

from __future__ import annotations

from datetime import datetime
import sys
from typing import Callable, Optional

from security.permissions import PermissionLevel
from system.winkeys import (
    MEDIA_NEXT,
    MEDIA_PLAY_PAUSE,
    MEDIA_PREV,
    MEDIA_STOP,
    VOLUME_DOWN,
    VOLUME_MUTE,
    VOLUME_UP,
    SystemControlUnavailable,
    lock as default_lock,
    open_uri as default_open_uri,
    send_key as default_send_key,
)
from tools.registry import Tool

# Fixed mapping of allowed settings page keys to official Microsoft ms-settings: URIs.
# Verified against official Windows ms-settings URI scheme documentation:
# - bluetooth: ms-settings:bluetooth (Settings > Bluetooth & devices)
# - wifi: ms-settings:network-wifi (Settings > Network & internet > Wi-Fi)
# - network: ms-settings:network (Settings > Network & internet)
# - display: ms-settings:display (Settings > System > Display)
# - sound: ms-settings:sound (Settings > System > Sound)
# - apps: ms-settings:appsfeatures (Settings > Apps > Installed apps / Apps & features)
# - updates: ms-settings:windowsupdate (Settings > Windows Update)
# - privacy: ms-settings:privacy (Settings > Privacy & security)
# - battery: ms-settings:batterysaver (Settings > System > Power & battery; older Win10 builds: Battery saver)
# - storage: ms-settings:storagesense (Settings > System > Storage)
# - accounts: ms-settings:yourinfo (Settings > Accounts > Your info)
#
# Security note: The model's input is strictly validated as a key in this fixed dictionary;
# the model string is NEVER concatenated into or used directly as a URI.
SETTINGS_PAGES: dict[str, str] = {
    "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "display": "ms-settings:display",
    "sound": "ms-settings:sound",
    "apps": "ms-settings:appsfeatures",
    "updates": "ms-settings:windowsupdate",
    "privacy": "ms-settings:privacy",
    "battery": "ms-settings:batterysaver",
    "storage": "ms-settings:storagesense",
    "accounts": "ms-settings:yourinfo",
}

# Rate limit telemetry and history
_RATE_LIMIT_REFUSALS: int = 0
_volume_press_history: list[tuple[float, int]] = []
_media_press_history: list[tuple[float, int]] = []


def get_rate_limit_refusal_count() -> int:
    """Return total count of rate limit refusals for telemetry/diagnostics."""
    return _RATE_LIMIT_REFUSALS


def reset_rate_limits() -> None:
    """Reset rate limit history and refusal counter (for tests)."""
    global _RATE_LIMIT_REFUSALS, _volume_press_history, _media_press_history
    _RATE_LIMIT_REFUSALS = 0
    _volume_press_history.clear()
    _media_press_history.clear()


def make_system_tools(
    sender: Optional[Callable[[int], None]] = None,
    clock: Optional[Callable[[], datetime]] = None,
    lock_fn: Optional[Callable[[], None]] = None,
    opener: Optional[Callable[[str], None]] = None,
) -> list[Tool]:
    """Create and return system control tools with injectable hardware callables and clock.

    If run on non-Windows platforms without injected test callables, returns an empty list
    so that system tools never register.
    """
    is_windows = sys.platform == "win32"
    has_fakes = sender is not None or lock_fn is not None or opener is not None

    if not is_windows and not has_fakes:
        return []

    eff_send_key = sender or default_send_key
    eff_lock = lock_fn or default_lock
    eff_opener = opener or default_open_uri
    eff_clock = clock or datetime.now

    def _get_timestamp() -> float:
        now_dt = eff_clock()
        return now_dt.timestamp() if hasattr(now_dt, "timestamp") else float(now_dt)

    def _check_volume_rate_limit(count: int) -> bool:
        """Check if adding count volume key presses exceeds 30 presses in the last 60 seconds."""
        global _RATE_LIMIT_REFUSALS, _volume_press_history
        now_ts = _get_timestamp()
        cutoff = now_ts - 60.0
        _volume_press_history = [(ts, c) for ts, c in _volume_press_history if ts >= cutoff]
        current_total = sum(c for _, c in _volume_press_history)
        if current_total + count > 30:
            _RATE_LIMIT_REFUSALS += 1
            return False
        _volume_press_history.append((now_ts, count))
        return True

    def _check_media_rate_limit(count: int = 1) -> bool:
        """Check if adding count media key presses exceeds 10 presses in the last 60 seconds."""
        global _RATE_LIMIT_REFUSALS, _media_press_history
        now_ts = _get_timestamp()
        cutoff = now_ts - 60.0
        _media_press_history = [(ts, c) for ts, c in _media_press_history if ts >= cutoff]
        current_total = sum(c for _, c in _media_press_history)
        if current_total + count > 10:
            _RATE_LIMIT_REFUSALS += 1
            return False
        _media_press_history.append((now_ts, count))
        return True

    # --- Tool 1: volume_change ---
    def volume_change_func(direction: str, steps: int = 3) -> str:
        dir_clean = str(direction).strip().lower()
        if dir_clean not in ("up", "down"):
            return "ERROR: direction must be 'up' or 'down'."
        try:
            steps_int = int(steps)
        except (ValueError, TypeError):
            return "ERROR: steps must be an integer between 1 and 10."
        if not (1 <= steps_int <= 10):
            return "ERROR: steps must be an integer between 1 and 10."

        if not _check_volume_rate_limit(steps_int):
            return "ERROR: too many key presses, try again in a minute."

        vk = VOLUME_UP if dir_clean == "up" else VOLUME_DOWN
        try:
            for _ in range(steps_int):
                eff_send_key(vk)
        except SystemControlUnavailable as exc:
            return f"ERROR: {exc}"

        return f"Sent volume {dir_clean} x{steps_int} (each press is roughly 2% on Windows)."

    def volume_change_precheck(args: dict) -> str | None:
        direction = args.get("direction")
        if direction not in ("up", "down"):
            return "ERROR: direction must be 'up' or 'down'."
        steps = args.get("steps")
        if steps is not None:
            if not isinstance(steps, int) or isinstance(steps, bool):
                return "ERROR: steps must be an integer between 1 and 10."
            if not (1 <= steps <= 10):
                return "ERROR: steps must be an integer between 1 and 10."
        return None

    tool_volume_change = Tool(
        name="volume_change",
        description="Call this whenever the user asks to turn the volume up or down by a number of steps or increments.",
        parameters={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Direction to adjust volume ('up' or 'down').",
                },
                "steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Number of key press steps (1 to 10, default 3). Each step is roughly 2% on Windows.",
                },
            },
            "required": ["direction"],
        },
        func=volume_change_func,
        permission=PermissionLevel.SAFE,
        escalate_when_tainted=True,
        precheck=volume_change_precheck,
    )

    # --- Tool 2: volume_mute_toggle ---
    def volume_mute_toggle_func() -> str:
        if not _check_volume_rate_limit(1):
            return "ERROR: too many key presses, try again in a minute."

        try:
            eff_send_key(VOLUME_MUTE)
        except SystemControlUnavailable as exc:
            return f"ERROR: {exc}"

        return "Sent the mute toggle; I can't see whether sound is now muted."

    tool_mute_toggle = Tool(
        name="volume_mute_toggle",
        description="Call this whenever the user asks to mute, unmute, or toggle system volume.",
        parameters={
            "type": "object",
            "properties": {},
        },
        func=volume_mute_toggle_func,
        permission=PermissionLevel.SAFE,
        escalate_when_tainted=True,
    )

    # --- Tool 3: media_key ---
    def media_key_func(action: str) -> str:
        act_clean = str(action).strip().lower()
        key_map = {
            "play_pause": (MEDIA_PLAY_PAUSE, "play/pause"),
            "next": (MEDIA_NEXT, "next"),
            "previous": (MEDIA_PREV, "previous"),
            "stop": (MEDIA_STOP, "stop"),
        }
        if act_clean not in key_map:
            return "ERROR: action must be one of: play_pause, next, previous, stop."

        if not _check_media_rate_limit(1):
            return "ERROR: too many key presses, try again in a minute."

        vk, display_name = key_map[act_clean]
        try:
            eff_send_key(vk)
        except SystemControlUnavailable as exc:
            return f"ERROR: {exc}"

        return f"Sent {display_name} to Windows; whichever app has media focus will react, I can't confirm which."

    def media_key_precheck(args: dict) -> str | None:
        action = args.get("action")
        if action not in ("play_pause", "next", "previous", "stop"):
            return "ERROR: action must be one of: play_pause, next, previous, stop."
        return None

    tool_media_key = Tool(
        name="media_key",
        description="Call this whenever the user asks to play, pause, resume, stop, skip, or navigate tracks in media players.",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play_pause", "next", "previous", "stop"],
                    "description": "Media action to send ('play_pause', 'next', 'previous', or 'stop').",
                },
            },
            "required": ["action"],
        },
        func=media_key_func,
        permission=PermissionLevel.SAFE,
        escalate_when_tainted=True,
        precheck=media_key_precheck,
    )

    # --- Tool 4: lock_screen ---
    def lock_screen_func() -> str:
        try:
            eff_lock()
        except SystemControlUnavailable as exc:
            return f"ERROR: {exc}"
        return "Locking the screen."

    tool_lock_screen = Tool(
        name="lock_screen",
        description="Call this whenever the user asks to lock the screen, computer, PC, or workstation.",
        parameters={
            "type": "object",
            "properties": {},
        },
        func=lock_screen_func,
        permission=PermissionLevel.CONFIRM,
        escalate_when_tainted=False,
    )

    # --- Tool 5: open_settings_page ---
    def open_settings_page_func(page: str) -> str:
        page_clean = str(page).strip().lower()
        if page_clean not in SETTINGS_PAGES:
            valid_pages = ", ".join(sorted(SETTINGS_PAGES.keys()))
            return f"ERROR: Unknown settings page '{page}'. Must be one of: {valid_pages}."

        uri = SETTINGS_PAGES[page_clean]
        try:
            eff_opener(uri)
        except SystemControlUnavailable as exc:
            return f"ERROR: {exc}"

        return f"Opened {page_clean.capitalize()} settings."

    def open_settings_page_precheck(args: dict) -> str | None:
        page = args.get("page")
        if page not in SETTINGS_PAGES:
            valid_pages = ", ".join(sorted(SETTINGS_PAGES.keys()))
            return f"ERROR: Unknown settings page '{page}'. Must be one of: {valid_pages}."
        return None

    tool_settings_page = Tool(
        name="open_settings_page",
        description="Call this whenever the user asks to open a Windows Settings page.",
        parameters={
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "enum": list(sorted(SETTINGS_PAGES.keys())),
                    "description": (
                        "Settings page identifier: bluetooth, wifi, network, display, "
                        "sound, apps, updates, privacy, battery, storage, or accounts."
                    ),
                },
            },
            "required": ["page"],
        },
        func=open_settings_page_func,
        permission=PermissionLevel.CONFIRM,
        escalate_when_tainted=False,
        precheck=open_settings_page_precheck,
    )

    return [
        tool_volume_change,
        tool_mute_toggle,
        tool_media_key,
        tool_lock_screen,
        tool_settings_page,
    ]
