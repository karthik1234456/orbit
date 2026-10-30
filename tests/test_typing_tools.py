"""Tests for guarded typing and dictation tool."""

from security.permissions import PermissionLevel
from system.winwindow import WindowItem
from tools.typing_tools import make_typing_tools, sanitize_typing_text


def test_sanitize_typing_text():
    """Verify whitespace collapse and removal of control, newline, tab, and bidi characters."""
    raw = "  Hello \t\n World! \x1b[31m  Test \u202eRTL  "
    clean = sanitize_typing_text(raw)
    assert clean == "Hello World! [31m Test RTL"
    assert "\n" not in clean
    assert "\t" not in clean


def test_typing_allowlist_enforcement():
    """Verify typing tool refuses applications outside the allowlist."""
    tools = {t.name: t for t in make_typing_tools(typing_enabled_getter=lambda: True)}
    type_tool = tools["type_into_app"]
    assert type_tool.permission == PermissionLevel.CONFIRM
    assert type_tool.block_when_tainted is True
    assert type_tool.model_visible is False

    # Chrome is not in default TYPING_ALLOWLIST
    res_bad = type_tool.func(app="chrome", text="hello")
    assert "I only type into notepad, whatsapp." in res_bad


def test_typing_chunking_and_foreground_abort():
    """Verify typing chunks characters in 20s and aborts if foreground window changes."""
    windows = [WindowItem(handle=10, process="notepad.exe", rect=(0, 0, 500, 500), state="normal")]
    current_fg = ["notepad.exe"]
    sent_chars = []

    def fake_fg_fn():
        return (current_fg[0], "notepad.exe")

    tools = {
        t.name: t
        for t in make_typing_tools(
            enumerator=lambda: windows,
            focus_fn=lambda h: True,
            foreground_fn=fake_fg_fn,
            char_sender=lambda ch: sent_chars.append(ch),
            is_elevated_fn=lambda h: False,
            clock=lambda: 1000.0,
            sleep=lambda s: None,
            typing_enabled_getter=lambda: True,
        )
    }
    type_tool = tools["type_into_app"]

    # 1. Normal typing (short text)
    res_ok = type_tool.func(app="notepad", text="hello")
    assert "Typed 5 characters into" in res_ok
    assert "".join(sent_chars) == "hello"

    # 2. Foreground window changes mid-typing (> 20 chars)
    sent_chars.clear()
    long_text = "A" * 25

    def fake_changing_fg():
        if len(sent_chars) >= 20:
            return ("chrome.exe", "chrome.exe")
        return ("notepad.exe", "notepad.exe")

    tools_abort = {
        t.name: t
        for t in make_typing_tools(
            enumerator=lambda: windows,
            focus_fn=lambda h: True,
            foreground_fn=fake_changing_fg,
            char_sender=lambda ch: sent_chars.append(ch),
            is_elevated_fn=lambda h: False,
            clock=lambda: 1000.0,
            sleep=lambda s: None,
            typing_enabled_getter=lambda: True,
        )
    }

    res_abort = tools_abort["type_into_app"].func(app="notepad", text=long_text)
    assert "Stopped typing because the window changed" in res_abort
    assert len(sent_chars) == 20  # aborted after first chunk


def test_typing_rate_limit():
    """Verify rate limit of at most 5 typing actions per 10 minutes."""
    windows = [WindowItem(handle=10, process="notepad.exe", rect=(0, 0, 500, 500), state="normal")]
    clock_time = [1000.0]

    tools = {
        t.name: t
        for t in make_typing_tools(
            enumerator=lambda: windows,
            focus_fn=lambda h: True,
            foreground_fn=lambda: ("notepad.exe", "notepad.exe"),
            char_sender=lambda ch: None,
            is_elevated_fn=lambda h: False,
            clock=lambda: clock_time[0],
            sleep=lambda s: None,
            typing_enabled_getter=lambda: True,
        )
    }
    t_type = tools["type_into_app"]

    for _ in range(5):
        res = t_type.func(app="notepad", text="hi")
        assert "Typed" in res

    # 6th attempt within 10 min
    res_limit = t_type.func(app="notepad", text="hi")
    assert "Rate limit reached" in res_limit

    # Advance clock by 601s -> resets
    clock_time[0] += 601.0
    res_reset = t_type.func(app="notepad", text="hi")
    assert "Typed" in res_reset
