"""Windows system keyboard events, lock station, and settings URI integration.

Provides a thin Windows layer behind injectable callables. Uses ctypes.windll.user32
for keybd_event and LockWorkStation, and os.startfile for ms-settings URIs.
Never executes external shells, child processes, or command lines.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

# Virtual-Key codes table for system volume and media controls
VOLUME_MUTE: int = 0xAD        # VK_VOLUME_MUTE
VOLUME_DOWN: int = 0xAE        # VK_VOLUME_DOWN
VOLUME_UP: int = 0xAF          # VK_VOLUME_UP
MEDIA_NEXT: int = 0xB0         # VK_MEDIA_NEXT_TRACK
MEDIA_PREV: int = 0xB1         # VK_MEDIA_PREV_TRACK
MEDIA_STOP: int = 0xB2         # VK_MEDIA_STOP
MEDIA_PLAY_PAUSE: int = 0xB3   # VK_MEDIA_PLAY_PAUSE

# Keyboard event flags
KEYEVENTF_EXTENDEDKEY: int = 0x0001
KEYEVENTF_KEYUP: int = 0x0002


class SystemControlUnavailable(RuntimeError):
    """Raised when system controls are invoked on non-Windows platforms or without system support."""
    pass


def _require_windows() -> None:
    """Validate operating system platform."""
    if sys.platform != "win32":
        raise SystemControlUnavailable(
            f"System controls are only available on Windows (current platform: {sys.platform})."
        )


def _real_send_key(vk: int) -> None:
    """Send key-down followed by key-up with extended-key flag via ctypes.windll.user32."""
    _require_windows()
    import ctypes
    user32 = ctypes.windll.user32
    # Key down
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    # Key up
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


def _real_lock() -> None:
    """Lock the Windows workstation using LockWorkStation via ctypes.windll.user32."""
    _require_windows()
    import ctypes
    user32 = ctypes.windll.user32
    user32.LockWorkStation()


def _real_open_uri(uri: str) -> None:
    """Open an allowed URI using os.startfile."""
    _require_windows()
    os.startfile(uri)


# Injectable default callables
_send_key_fn: Optional[Callable[[int], None]] = None
_lock_fn: Optional[Callable[[], None]] = None
_open_uri_fn: Optional[Callable[[str], None]] = None


def set_key_sender(fn: Optional[Callable[[int], None]]) -> None:
    """Set custom key sender callable (used for testing)."""
    global _send_key_fn
    _send_key_fn = fn


def set_lock_fn(fn: Optional[Callable[[], None]]) -> None:
    """Set custom workstation lock callable (used for testing)."""
    global _lock_fn
    _lock_fn = fn


def set_uri_opener(fn: Optional[Callable[[str], None]]) -> None:
    """Set custom URI opener callable (used for testing)."""
    global _open_uri_fn
    _open_uri_fn = fn


def send_key(vk: int) -> None:
    """Send a virtual-key press (down and up). Uses injected sender if set, else Windows keybd_event."""
    if _send_key_fn is not None:
        _send_key_fn(vk)
    else:
        _real_send_key(vk)


def lock() -> None:
    """Lock the workstation. Uses injected lock function if set, else LockWorkStation."""
    if _lock_fn is not None:
        _lock_fn()
    else:
        _real_lock()


def open_uri(uri: str) -> None:
    """Open a URI. Uses injected opener if set, else os.startfile."""
    if _open_uri_fn is not None:
        _open_uri_fn(uri)
    else:
        _real_open_uri(uri)
