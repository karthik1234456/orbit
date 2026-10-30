"""Windows foreground window inspection, enumeration, and window control.

Provides a thin Windows layer behind injectable callables for querying the active
foreground window, enumerating top-level windows without titles, and controlling window
placement (focus, minimize, maximize, restore, snap, show desktop) with verified outcomes.
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
from typing import Callable, NamedTuple, Optional

from system.winkeys import SystemControlUnavailable

# Process access rights for querying process image name
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000

# Window show states
SW_MAXIMIZE: int = 3
SW_MINIMIZE: int = 6
SW_RESTORE: int = 9

# DWM and Monitor constants
DWMWA_CLOAKED: int = 14
MONITOR_DEFAULTTONEAREST: int = 2

# SetWindowPos flags
SWP_NOZORDER: int = 0x0004
SWP_NOACTIVATE: int = 0x0010

# Default WhatsApp executable names (case-insensitive)
DEFAULT_WHATSAPP_PROCESS_NAMES: tuple[str, ...] = ("WhatsApp.exe", "WhatsApp.Root.exe")


class WindowItem(NamedTuple):
    """Represents a top-level visible window in Z-order.

    Strict privacy guarantee: Window title strings are NEVER stored, logged, or returned.
    Only window handle, process exe name, bounding rect, and window state are retained.
    """
    handle: int
    process: Optional[str]
    rect: tuple[int, int, int, int]  # (left, top, right, bottom)
    state: str  # "normal", "minimized", "maximized"


def _require_windows() -> None:
    """Validate operating system platform."""
    if sys.platform != "win32":
        raise SystemControlUnavailable("Windows window functions unavailable on this platform.")


# ---------------------------------------------------------------------------
# Low-level Windows API helpers (only executed on win32 when not mocked)
# ---------------------------------------------------------------------------

def _real_foreground_window() -> tuple[Optional[str], Optional[str]]:
    """Query the active foreground window title and process executable name."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return (None, None)

    # 1. Query window title
    length = user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title: Optional[str] = title_buf.value
    else:
        title = ""

    # 2. Query process executable name with limited query rights
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_process:
        return (title, None)

    try:
        size = wintypes.DWORD(1024)
        path_buf = ctypes.create_unicode_buffer(1024)
        success = kernel32.QueryFullProcessImageNameW(h_process, 0, path_buf, ctypes.byref(size))
        if success:
            exe_path = path_buf.value
            exe_name = Path(exe_path).name
            return (title, exe_name)
        return (title, None)
    finally:
        kernel32.CloseHandle(h_process)


def _real_process_name_for_hwnd(hwnd: int) -> Optional[str]:
    """Query process executable name for a specific window handle."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_process:
        return None

    try:
        size = wintypes.DWORD(1024)
        path_buf = ctypes.create_unicode_buffer(1024)
        success = kernel32.QueryFullProcessImageNameW(h_process, 0, path_buf, ctypes.byref(size))
        if success:
            return Path(path_buf.value).name
        return None
    finally:
        kernel32.CloseHandle(h_process)


def _real_is_cloaked(hwnd: int) -> bool:
    """Check if window is cloaked by DWM (Windows 10/11 virtual desktops or UWP suspended)."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    try:
        dwmapi = ctypes.windll.dwmapi
        cloaked = wintypes.DWORD(0)
        res = dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return bool(res == 0 and cloaked.value != 0)
    except Exception:
        return False


def _real_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Query window bounding rectangle."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _real_window_state(hwnd: int) -> str:
    """Query window iconic/zoomed state."""
    _require_windows()
    import ctypes
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        return "minimized"
    if user32.IsZoomed(hwnd):
        return "maximized"
    return "normal"


def _real_monitor_work_area(hwnd: int) -> tuple[int, int, int, int]:
    """Query monitor working area rectangle (excluding taskbar) for given window."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    h_mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not h_mon:
        return (0, 0, 1920, 1080)

    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(h_mon, ctypes.byref(mi)):
        return (int(mi.rcWork.left), int(mi.rcWork.top), int(mi.rcWork.right), int(mi.rcWork.bottom))
    return (0, 0, 1920, 1080)


# ---------------------------------------------------------------------------
# Global injectable hooks for testing
# ---------------------------------------------------------------------------

_foreground_probe_fn: Optional[Callable[[], tuple[Optional[str], Optional[str]]]] = None
_window_enumerator_fn: Optional[Callable[[], list[WindowItem]]] = None


def set_foreground_probe(fn: Optional[Callable[[], tuple[Optional[str], Optional[str]]]]) -> None:
    """Set custom foreground probe callable (used for testing)."""
    global _foreground_probe_fn
    _foreground_probe_fn = fn


def get_foreground_probe() -> Callable[[], tuple[Optional[str], Optional[str]]]:
    """Return the active foreground window probe."""
    if _foreground_probe_fn is not None:
        return _foreground_probe_fn
    return _real_foreground_window


def set_window_enumerator(fn: Optional[Callable[[], list[WindowItem]]]) -> None:
    """Set custom window enumerator callable (used for testing)."""
    global _window_enumerator_fn
    _window_enumerator_fn = fn


# ---------------------------------------------------------------------------
# Public window query and verification functions
# ---------------------------------------------------------------------------

def foreground_window() -> tuple[Optional[str], Optional[str]]:
    """Query the active foreground window title and process executable name."""
    probe = get_foreground_probe()
    return probe()


def verify_whatsapp_window(
    title: Optional[str],
    process_name: Optional[str],
    allowed_process_names: Optional[tuple[str, ...]] = None,
) -> bool:
    """Verify that a window is WhatsApp based on title and process name."""
    if not title or not process_name:
        return False
    if "whatsapp" not in title.lower():
        return False
    targets = allowed_process_names or DEFAULT_WHATSAPP_PROCESS_NAMES
    allowed = {p.lower() for p in targets}
    return process_name.lower() in allowed


def wait_for_foreground(
    predicate: Callable[[str, str], bool],
    timeout: float = 6.0,
    foreground_fn: Optional[Callable[[], tuple[Optional[str], Optional[str]]]] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> bool:
    """Poll foreground window until predicate(title, process_name) is True or timeout expires."""
    get_time = clock or time.monotonic
    do_sleep = sleep_fn or sleep or time.sleep
    get_fg = foreground_fn or foreground_window

    deadline = get_time() + float(timeout)
    while get_time() <= deadline:
        title, proc = get_fg()
        if title is not None and proc is not None and predicate(title, proc):
            return True
        do_sleep(0.1)

    return False


def enumerate_windows(
    enum_fn: Optional[Callable[[], list[WindowItem]]] = None,
) -> list[WindowItem]:
    """Enumerate top-level, visible, non-cloaked windows with a title in Z-order.

    Strict privacy rule: NEVER stores or returns window title strings.
    Each item is returned as WindowItem(handle, process_exe_name or None, rect, state).
    """
    if enum_fn is not None:
        return enum_fn()
    if _window_enumerator_fn is not None:
        return _window_enumerator_fn()

    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    windows: list[WindowItem] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_callback(hwnd: int, lparam: int) -> bool:
        try:
            # 1. Must be visible
            if not user32.IsWindowVisible(hwnd):
                return True

            # 2. Must not be cloaked
            if _real_is_cloaked(hwnd):
                return True

            # 3. Must have a non-empty title (length > 0)
            # CRITICAL: We only check length and NEVER keep or record title text!
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True

            # 4. Process executable name
            proc_name = _real_process_name_for_hwnd(hwnd)

            # 5. Rect and state
            rect = _real_window_rect(hwnd)
            state = _real_window_state(hwnd)

            windows.append(WindowItem(handle=hwnd, process=proc_name, rect=rect, state=state))
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(_enum_callback)
    user32.EnumWindows(cb, 0)
    return windows


def is_elevated_target(
    handle: int,
    open_process_fn: Optional[Callable[[int], bool]] = None,
) -> bool:
    """Heuristic check: returns True if target process runs with elevated privileges.

    If process cannot be opened even with limited query rights, it is treated as elevated.
    """
    if open_process_fn is not None:
        try:
            res = open_process_fn(handle)
            return not bool(res)
        except Exception:
            return True

    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if not pid.value:
        return False

    # Attempt to open process with limited query rights
    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_process:
        # Cannot open process -> elevated or protected process
        return True

    kernel32.CloseHandle(h_process)
    return False


# ---------------------------------------------------------------------------
# Window Actions with verified outcomes
# ---------------------------------------------------------------------------

def focus(
    handle: int,
    show_window_fn: Optional[Callable[[int, int], None]] = None,
    set_foreground_fn: Optional[Callable[[int], bool]] = None,
    get_foreground_fn: Optional[Callable[[], Optional[int]]] = None,
    is_iconic_fn: Optional[Callable[[int], bool]] = None,
    alt_key_sender: Optional[Callable[[], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    key_sender: Optional[Callable[[int], None]] = None,
) -> bool:
    """Bring target window to foreground, restoring if minimized, with verified outcome.

    If foreground does not switch within 400 ms, attempts ONE workaround via harmless ALT
    key press, then re-verifies. Returns True if foreground switched to handle, else False.
    """
    get_time = clock or time.monotonic
    do_sleep = sleep or sleep_fn or time.sleep

    if show_window_fn is None or set_foreground_fn is None or get_foreground_fn is None:
        _require_windows()
        import ctypes
        user32 = ctypes.windll.user32
        actual_show = show_window_fn or (lambda h, cmd: user32.ShowWindow(h, cmd))
        actual_set_fg = set_foreground_fn or (lambda h: bool(user32.SetForegroundWindow(h)))
        actual_get_fg = get_foreground_fn or (lambda: user32.GetForegroundWindow())
        actual_is_iconic = is_iconic_fn or (lambda h: bool(user32.IsIconic(h)))
    else:
        actual_show = show_window_fn
        actual_set_fg = set_foreground_fn
        actual_get_fg = get_foreground_fn
        actual_is_iconic = is_iconic_fn or (lambda h: False)

    # Restore if minimized
    if actual_is_iconic(handle):
        actual_show(handle, SW_RESTORE)

    # First attempt to set foreground
    actual_set_fg(handle)

    # Wait up to 400 ms for switch
    deadline = get_time() + 0.400
    while get_time() <= deadline:
        if actual_get_fg() == handle:
            return True
        do_sleep(0.05)

    # Workaround: send one harmless ALT key press to enable foreground switch
    if key_sender is not None:
        key_sender(0x12)
    elif alt_key_sender is not None:
        alt_key_sender()
    else:
        try:
            from system.winkeys import send_key
            send_key(0x12)  # VK_MENU
        except Exception:
            pass

    actual_set_fg(handle)

    # Re-verify up to 200 ms after workaround
    deadline2 = get_time() + 0.200
    while get_time() <= deadline2:
        if actual_get_fg() == handle:
            return True
        do_sleep(0.05)

    return actual_get_fg() == handle


def minimize(
    handle: int,
    show_window_fn: Optional[Callable[[int, int], None]] = None,
    is_iconic_fn: Optional[Callable[[int], bool]] = None,
) -> bool:
    """Minimize window with verified outcome (IsIconic)."""
    if show_window_fn is None or is_iconic_fn is None:
        _require_windows()
        import ctypes
        user32 = ctypes.windll.user32
        actual_show = show_window_fn or (lambda h, cmd: user32.ShowWindow(h, cmd))
        actual_iconic = is_iconic_fn or (lambda h: bool(user32.IsIconic(h)))
    else:
        actual_show = show_window_fn
        actual_iconic = is_iconic_fn

    actual_show(handle, SW_MINIMIZE)
    return actual_iconic(handle)


def maximize(
    handle: int,
    show_window_fn: Optional[Callable[[int, int], None]] = None,
    is_zoomed_fn: Optional[Callable[[int], bool]] = None,
) -> bool:
    """Maximize window with verified outcome (IsZoomed)."""
    if show_window_fn is None or is_zoomed_fn is None:
        _require_windows()
        import ctypes
        user32 = ctypes.windll.user32
        actual_show = show_window_fn or (lambda h, cmd: user32.ShowWindow(h, cmd))
        actual_zoomed = is_zoomed_fn or (lambda h: bool(user32.IsZoomed(h)))
    else:
        actual_show = show_window_fn
        actual_zoomed = is_zoomed_fn

    actual_show(handle, SW_MAXIMIZE)
    return actual_zoomed(handle)


def restore(
    handle: int,
    show_window_fn: Optional[Callable[[int, int], None]] = None,
    is_iconic_fn: Optional[Callable[[int], bool]] = None,
    is_zoomed_fn: Optional[Callable[[int], bool]] = None,
) -> bool:
    """Restore window to normal size and verify state is neither iconic nor zoomed."""
    if show_window_fn is None or is_iconic_fn is None or is_zoomed_fn is None:
        _require_windows()
        import ctypes
        user32 = ctypes.windll.user32
        actual_show = show_window_fn or (lambda h, cmd: user32.ShowWindow(h, cmd))
        actual_iconic = is_iconic_fn or (lambda h: bool(user32.IsIconic(h)))
        actual_zoomed = is_zoomed_fn or (lambda h: bool(user32.IsZoomed(h)))
    else:
        actual_show = show_window_fn
        actual_iconic = is_iconic_fn
        actual_zoomed = is_zoomed_fn

    actual_show(handle, SW_RESTORE)
    return (not actual_iconic(handle)) and (not actual_zoomed(handle))


def snap(
    handle: int,
    side: str,
    monitor_work_area_fn: Optional[Callable[[int], tuple[int, int, int, int]]] = None,
    show_window_fn: Optional[Callable[[int, int], None]] = None,
    set_window_pos_fn: Optional[Callable[[int, int, int, int, int, int, int], None]] = None,
    get_window_rect_fn: Optional[Callable[[int], tuple[int, int, int, int]]] = None,
    is_zoomed_fn: Optional[Callable[[int], bool]] = None,
    monitor_rect_fn: Optional[Callable[[int], tuple[int, int, int, int]]] = None,
    set_pos_fn: Optional[Callable[[int, int, int, int, int], None]] = None,
    get_rect_fn: Optional[Callable[[int], tuple[int, int, int, int]]] = None,
) -> bool:
    """Snap window to the left or right half of the monitor work area.

    Restores first if currently maximized. Verifies that the resulting window rect
    matches the target within an 8-pixel margin.
    """
    clean_side = str(side).strip().lower()
    if clean_side not in ("left", "right"):
        return False

    eff_work = monitor_rect_fn or monitor_work_area_fn
    eff_pos = (lambda h, ins, x, y, cx, cy, fl: set_pos_fn(h, x, y, cx, cy)) if set_pos_fn else set_window_pos_fn
    eff_rect = get_rect_fn or get_window_rect_fn

    if (
        eff_work is None
        or show_window_fn is None
        or eff_pos is None
        or eff_rect is None
    ):
        _require_windows()
        import ctypes
        user32 = ctypes.windll.user32
        actual_work = eff_work or _real_monitor_work_area
        actual_show = show_window_fn or (lambda h, cmd: user32.ShowWindow(h, cmd))
        actual_set_pos = eff_pos or (
            lambda h, ins_after, x, y, cx, cy, flags: user32.SetWindowPos(h, ins_after, x, y, cx, cy, flags)
        )
        actual_rect = eff_rect or _real_window_rect
        actual_zoomed = is_zoomed_fn or (lambda h: bool(user32.IsZoomed(h)))
    else:
        actual_work = eff_work
        actual_show = show_window_fn
        actual_set_pos = eff_pos
        actual_rect = eff_rect
        actual_zoomed = is_zoomed_fn or (lambda h: False)

    # 1. Restore if maximized
    if actual_zoomed(handle):
        actual_show(handle, SW_RESTORE)

    # 2. Compute monitor work area
    rc_left, rc_top, rc_right, rc_bottom = actual_work(handle)
    total_w = rc_right - rc_left
    total_h = rc_bottom - rc_top
    half_w = total_w // 2

    if clean_side == "left":
        target_x = rc_left
        target_y = rc_top
        target_w = half_w
        target_h = total_h
    else:
        target_x = rc_left + half_w
        target_y = rc_top
        target_w = total_w - half_w
        target_h = total_h

    # 3. Position window
    actual_set_pos(
        handle,
        0,
        target_x,
        target_y,
        target_w,
        target_h,
        SWP_NOZORDER,
    )

    # 4. Verify resulting bounding rectangle within 8px tolerance
    cur_left, cur_top, cur_right, cur_bottom = actual_rect(handle)
    cur_x = cur_left
    cur_y = cur_top
    cur_w = cur_right - cur_left
    cur_h = cur_bottom - cur_top

    tol = 8
    ok_x = abs(cur_x - target_x) <= tol
    ok_y = abs(cur_y - target_y) <= tol
    ok_w = abs(cur_w - target_w) <= tol
    ok_h = abs(cur_h - target_h) <= tol

    return ok_x and ok_y and ok_w and ok_h


def show_desktop(
    key_chord_fn: Optional[Callable[[], None]] = None,
    chord_fn: Optional[Callable[[int, int], None]] = None,
) -> bool:
    """Minimize all windows to reveal the desktop via Win+D chord."""
    if chord_fn is not None:
        chord_fn(0x5B, 0x44)
        return True
    if key_chord_fn is not None:
        key_chord_fn()
        return True

    _require_windows()
    import ctypes
    user32 = ctypes.windll.user32

    VK_LWIN = 0x5B
    VK_D = 0x44
    KEYEVENTF_KEYUP = 0x0002

    # Win down, D down, D up, Win up
    user32.keybd_event(VK_LWIN, 0, 0, 0)
    user32.keybd_event(VK_D, 0, 0, 0)
    user32.keybd_event(VK_D, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
    return True
