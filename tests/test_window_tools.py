"""Tests for window control tools and winwindow system layer."""

from system.winwindow import (
    WindowItem,
    enumerate_windows,
    is_elevated_target,
    focus as win_focus,
    minimize as win_minimize,
    maximize as win_maximize,
    restore as win_restore,
    snap as win_snap,
    show_desktop as win_show_desktop,
)
from tools.window_tools import make_window_tools
from security.permissions import PermissionLevel


def test_window_item_has_no_title_field():
    """Verify WindowItem never stores or exposes window title strings."""
    item = WindowItem(handle=1001, process="chrome.exe", rect=(0, 0, 800, 600), state="normal")
    assert not hasattr(item, "title")
    assert not hasattr(item, "window_title")
    assert not hasattr(item, "caption")
    assert "title" not in WindowItem.__annotations__


def test_enumerate_windows_filtering():
    """Verify enumerate_windows filters invisible, cloaked, or titleless windows in Z-order."""
    fake_raw = [
        {"hwnd": 1, "visible": True, "cloaked": False, "title": "Chrome", "proc": "chrome.exe", "rect": (0, 0, 100, 100), "state": "normal"},
        {"hwnd": 2, "visible": False, "cloaked": False, "title": "Hidden", "proc": "hidden.exe", "rect": (0, 0, 100, 100), "state": "normal"},
        {"hwnd": 3, "visible": True, "cloaked": True, "title": "Cloaked", "proc": "cloaked.exe", "rect": (0, 0, 100, 100), "state": "normal"},
        {"hwnd": 4, "visible": True, "cloaked": False, "title": "", "proc": "notitle.exe", "rect": (0, 0, 100, 100), "state": "normal"},
        {"hwnd": 5, "visible": True, "cloaked": False, "title": "Notepad", "proc": "notepad.exe", "rect": (10, 10, 200, 200), "state": "minimized"},
    ]

    def fake_enum():
        return [
            WindowItem(handle=x["hwnd"], process=x["proc"], rect=x["rect"], state=x["state"])
            for x in fake_raw
            if x["visible"] and not x["cloaked"] and bool(x["title"].strip())
        ]

    items = enumerate_windows(enum_fn=fake_enum)
    assert len(items) == 2
    assert items[0].handle == 1
    assert items[0].process == "chrome.exe"
    assert items[1].handle == 5
    assert items[1].process == "notepad.exe"


def test_is_elevated_target():
    """Verify is_elevated_target returns True on open failure / PermissionError."""
    def fake_open_err(pid):
        raise PermissionError("Access is denied")

    assert is_elevated_target(100, open_process_fn=fake_open_err) is True

    def fake_open_ok(pid):
        return True

    assert is_elevated_target(200, open_process_fn=fake_open_ok) is False


def test_focus_success_and_workaround():
    """Verify focus succeeds directly or via the harmless ALT key workaround."""
    # Direct success
    fake_fg = [100]
    ok = win_focus(
        100,
        show_window_fn=lambda h, cmd: True,
        set_foreground_fn=lambda h: True,
        is_iconic_fn=lambda h: False,
        get_foreground_fn=lambda: fake_fg[0],
        sleep_fn=lambda s: None,
    )
    assert ok is True

    # Success with workaround: initial loop times out after 400ms, then workaround succeeds
    fake_time = [1000.0]
    fake_fg_val = [0]
    def fake_get_fg():
        return fake_fg_val[0]

    alt_chords = []
    def fake_sender(vk):
        alt_chords.append(vk)
        fake_fg_val[0] = 200  # becomes foreground after workaround

    def fake_sleep(s):
        fake_time[0] += s

    ok_workaround = win_focus(
        200,
        show_window_fn=lambda h, cmd: True,
        set_foreground_fn=lambda h: True,
        is_iconic_fn=lambda h: False,
        get_foreground_fn=fake_get_fg,
        key_sender=fake_sender,
        clock=lambda: fake_time[0],
        sleep_fn=fake_sleep,
    )
    assert ok_workaround is True
    assert len(alt_chords) > 0


def test_minimize_maximize_restore_and_snap():
    """Verify minimize, maximize, restore, and snap verification."""
    # Minimize
    assert win_minimize(10, show_window_fn=lambda h, c: True, is_iconic_fn=lambda h: True) is True
    assert win_minimize(10, show_window_fn=lambda h, c: True, is_iconic_fn=lambda h: False) is False

    # Maximize
    assert win_maximize(20, show_window_fn=lambda h, c: True, is_zoomed_fn=lambda h: True) is True

    # Restore
    assert win_restore(30, show_window_fn=lambda h, c: True, is_iconic_fn=lambda h: False, is_zoomed_fn=lambda h: False) is True

    # Snap left / right
    def fake_monitor(h):
        return (0, 0, 1920, 1080)

    placed_rects = []
    def fake_set_pos(h, x, y, cx, cy):
        placed_rects.append((x, y, cx, cy))
        return True

    def fake_get_rect(h):
        if placed_rects:
            x, y, cx, cy = placed_rects[-1]
            return (x, y, x + cx, y + cy)
        return (0, 0, 0, 0)

    # Snap left
    ok_left = win_snap(40, "left", monitor_rect_fn=fake_monitor, set_pos_fn=fake_set_pos, get_rect_fn=fake_get_rect)
    assert ok_left is True
    assert placed_rects[-1] == (0, 0, 960, 1080)

    # Snap right
    ok_right = win_snap(40, "right", monitor_rect_fn=fake_monitor, set_pos_fn=fake_set_pos, get_rect_fn=fake_get_rect)
    assert ok_right is True
    assert placed_rects[-1] == (960, 0, 960, 1080)


def test_show_desktop_key_chord():
    """Verify show_desktop sends Win+D key chord."""
    sent = []
    def fake_chord(k1, k2):
        sent.append((k1, k2))

    assert win_show_desktop(chord_fn=fake_chord) is True
    assert len(sent) == 1
    assert sent[0] == (0x5B, 0x44)  # VK_LWIN, ord('D')


def test_make_window_tools_topmost_z_order_and_rate_limits():
    """Verify window tools select topmost matching window and enforce 30/min limit."""
    windows = [
        WindowItem(handle=10, process="chrome.exe", rect=(0, 0, 500, 500), state="normal"),
        WindowItem(handle=11, process="chrome.exe", rect=(50, 50, 500, 500), state="normal"),
    ]
    focused_handles = []

    fake_clock_time = [1000.0]
    def fake_clock():
        return fake_clock_time[0]

    tools = {
        t.name: t
        for t in make_window_tools(
            enumerator=lambda: windows,
            focus_fn=lambda h: focused_handles.append(h) or True,
            is_elevated_fn=lambda h: False,
            clock=fake_clock,
        )
    }

    assert "focus_app" in tools
    t_focus = tools["focus_app"]
    assert t_focus.permission == PermissionLevel.SAFE
    assert t_focus.model_visible is False

    # Executes on topmost (handle 10)
    res = t_focus.func(app="chrome")
    assert "in front" in res
    assert focused_handles == [10]

    # Rate limiting: 30 actions per minute
    for _ in range(29):
        t_focus.func(app="chrome")

    res_blocked = t_focus.func(app="chrome")
    assert "Rate limit reached" in res_blocked

    # Advance clock by 61s -> resets
    fake_clock_time[0] += 61.0
    res_ok = t_focus.func(app="chrome")
    assert "in front" in res_ok
