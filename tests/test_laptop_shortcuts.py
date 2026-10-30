"""Tests for laptop control shortcut patterns, near misses, ordinal conflict, and chains."""

from core.chains import parse_chain
from core.session_state import SessionState
from core.shortcuts import ShortcutReply, ToolCallSpec, parse_shortcut


def test_window_shortcuts_and_near_misses():
    """Verify window control shortcuts and refusal on near misses."""
    # Positive matches
    res1 = parse_shortcut("switch to chrome")
    assert isinstance(res1, ToolCallSpec) and res1.name == "focus_app" and res1.args == {"app": "chrome"}

    res2 = parse_shortcut("bring vscode to the front")
    assert isinstance(res2, ToolCallSpec) and res2.name == "focus_app" and res2.args == {"app": "vscode"}

    res3 = parse_shortcut("minimize notepad")
    assert isinstance(res3, ToolCallSpec) and res3.name == "minimize_app" and res3.args == {"app": "notepad"}

    res4 = parse_shortcut("snap chrome to the left")
    assert isinstance(res4, ToolCallSpec) and res4.name == "snap_app" and res4.args == {"app": "chrome", "side": "left"}

    res5 = parse_shortcut("put notepad on the right half")
    assert isinstance(res5, ToolCallSpec) and res5.name == "snap_app" and res5.args == {"app": "notepad", "side": "right"}

    res6 = parse_shortcut("show desktop")
    assert isinstance(res6, ToolCallSpec) and res6.name == "show_desktop"

    res7 = parse_shortcut("minimize everything")
    assert isinstance(res7, ToolCallSpec) and res7.name == "show_desktop"

    # Near misses (must return None)
    assert parse_shortcut("switch to the store") is None
    assert parse_shortcut("focus") is None
    assert parse_shortcut("minimize") is None
    assert parse_shortcut("snap out of it") is None


def test_file_shortcuts_and_ordinal_conflict():
    """Verify file finding, open, reveal, and ordinal conflict resolution."""
    # Positive search
    res_dl = parse_shortcut("find latest download")
    assert isinstance(res_dl, ToolCallSpec) and res_dl.name == "find_files"

    res_pdf = parse_shortcut("find my newest pdf")
    assert isinstance(res_pdf, ToolCallSpec) and res_pdf.name == "find_files"

    res_doc = parse_shortcut("find files named invoice")
    assert isinstance(res_doc, ToolCallSpec) and res_doc.name == "find_files"

    # Near misses
    assert parse_shortcut("find") is None
    assert parse_shortcut("find my keys") is None

    # Specific file open/reveal (always files)
    res_f1 = parse_shortcut("open the first file")
    assert isinstance(res_f1, ToolCallSpec) and res_f1.name == "open_found_file" and res_f1.args == {"index": 1}

    res_f2 = parse_shortcut("open file number 2")
    assert isinstance(res_f2, ToolCallSpec) and res_f2.name == "open_found_file" and res_f2.args == {"index": 2}

    res_rev = parse_shortcut("reveal the first file in folder")
    assert isinstance(res_rev, ToolCallSpec) and res_rev.name == "reveal_found_file" and res_rev.args == {"index": 1}

    # Ordinal conflict test: "open the first one"
    # State with files:
    state_files = SessionState()
    state_files.set_found_files(["C:/fake/report.pdf"])
    res_ord_files = parse_shortcut("open the first one", state=state_files)
    assert isinstance(res_ord_files, ToolCallSpec)
    assert res_ord_files.name == "open_found_file"
    assert res_ord_files.args == {"index": 1}

    # State with page / browser enabled:
    state_page = SessionState()
    state_page.last_result_kind = "page"
    res_ord_browser = parse_shortcut("open the first one", state=state_page, browser_enabled=True)
    assert isinstance(res_ord_browser, ToolCallSpec)
    assert res_ord_browser.name == "browser_click_result"


def test_screenshot_shortcuts():
    """Verify screenshot shortcuts and disabled settings response."""
    res1 = parse_shortcut("take a screenshot")
    assert isinstance(res1, ToolCallSpec) and res1.name == "take_screenshot"

    res2 = parse_shortcut("screenshot")
    assert isinstance(res2, ToolCallSpec) and res2.name == "take_screenshot"

    res3 = parse_shortcut("capture the screen")
    assert isinstance(res3, ToolCallSpec) and res3.name == "take_screenshot"

    res_off = parse_shortcut("take a screenshot", screenshots_enabled=False)
    assert isinstance(res_off, ShortcutReply)
    assert "Screenshots are off" in res_off.reply


def test_typing_shortcuts_allowlist_and_near_misses():
    """Verify typing shortcuts, allowlist enforcement, and near misses."""
    res_ok = parse_shortcut("type hello world into notepad", typing_enabled=True)
    assert isinstance(res_ok, ToolCallSpec)
    assert res_ok.name == "type_into_app"
    assert res_ok.args == {"app": "notepad", "text": "hello world"}

    # Not in allowlist (chrome) -> fixed allowlist reply
    res_bad_app = parse_shortcut("type hello in chrome", typing_enabled=True)
    assert isinstance(res_bad_app, ShortcutReply)
    assert "I only type into notepad, whatsapp." in res_bad_app.reply

    # Disabled -> fixed setting reply
    res_off = parse_shortcut("type hello in notepad", typing_enabled=False)
    assert isinstance(res_off, ShortcutReply)
    assert "Typing is off. Turn on TYPING_ENABLED in Settings." in res_off.reply

    # Near misses
    assert parse_shortcut("type") is None
    assert parse_shortcut("type hello") is None


def test_sleep_shortcuts_and_near_misses():
    """Verify sleep shortcuts and near misses."""
    res_ok = parse_shortcut("put the computer to sleep", sleep_enabled=True)
    assert isinstance(res_ok, ToolCallSpec)
    assert res_ok.name == "sleep_computer"

    res_laptop = parse_shortcut("put the laptop to sleep", sleep_enabled=True)
    assert isinstance(res_laptop, ToolCallSpec)
    assert res_laptop.name == "sleep_computer"

    res_off = parse_shortcut("put the computer to sleep", sleep_enabled=False)
    assert isinstance(res_off, ShortcutReply)
    assert "Sleep is off. Turn on SLEEP_ENABLED in Settings." in res_off.reply

    # Near misses
    assert parse_shortcut("go to sleep") is None
    assert parse_shortcut("i need sleep") is None
    assert parse_shortcut("take a break") is None


def test_laptop_chaining_rules():
    """Verify allowed chaining for window/file/screenshot and rejection of typing/sleep."""
    # 1. Window actions, screenshots, and file open can chain
    chain1 = parse_chain("minimize chrome then take a screenshot")
    assert chain1 is not None
    assert len(chain1.steps) == 2
    assert chain1.steps[0].name == "minimize_app"
    assert chain1.steps[1].name == "take_screenshot"

    chain2 = parse_chain("find latest download then open the first file")
    assert chain2 is not None
    assert len(chain2.steps) == 2
    assert chain2.steps[0].name == "find_files"
    assert chain2.steps[1].name == "open_found_file"

    # 2. Free-text find_files query NEVER appears in chain
    chain_free_find = parse_chain("find documents called budget then open the first file")
    assert chain_free_find is None

    # 3. Typing NEVER appears in chain
    chain_type = parse_chain("type hello in notepad, and then minimize notepad")
    assert chain_type is None

    chain_type_semi = parse_chain("type hello in notepad; calc")
    assert chain_type_semi is None

    # 4. Sleep NEVER appears in chain
    chain_sleep = parse_chain("minimize chrome then put the computer to sleep")
    assert chain_sleep is None

