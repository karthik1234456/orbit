"""Tests for TabRail vertical component."""

import pytest

from ui.tabrail import TabRail
from ui.theme import get_palette


from tests.conftest import make_test_root


@pytest.fixture
def tk_root():
    root = make_test_root()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def test_tabrail_creation(tk_root):
    palette = get_palette("cyan")
    rail = TabRail(tk_root, palette=palette)
    rail.pack()
    tk_root.update_idletasks()

    assert rail.winfo_width() == TabRail.WIDTH or rail.WIDTH == 36
    tabs = rail.get_tabs()
    assert len(tabs) == 13
    assert tabs[0] == "today"
    assert tabs[1] == "activity"
    assert tabs[7] == "contacts"
    assert tabs[8] == "files"
    assert tabs[9] == "system"
    assert tabs[10] == "insights"
    assert tabs[11] == "verify"
    assert tabs[12] == "last_run"


def test_tabrail_selection_and_callback(tk_root):
    palette = get_palette("cyan")
    selected_log = []

    rail = TabRail(tk_root, palette=palette, on_select=lambda tid: selected_log.append(tid))
    rail.pack()

    rail.select_tab("insights")
    assert rail.get_active_tab() == "insights"
    assert selected_log == ["insights"]

    rail.select_tab(1)  # activity
    assert rail.get_active_tab() == "activity"
    assert selected_log == ["insights", "activity"]

    # Invalid tab ID should be ignored
    rail.select_tab("non_existent")
    assert rail.get_active_tab() == "activity"


def test_tabrail_badges(tk_root):
    palette = get_palette("cyan")
    rail = TabRail(tk_root, palette=palette)

    assert rail.get_badge("today") == 0
    rail.set_badge("today", 3)
    assert rail.get_badge("today") == 3

    rail.set_badge("today", 15)
    assert rail.get_badge("today") == 15

    rail.set_badge("today", -1)
    assert rail.get_badge("today") == 0


def test_tabrail_keyboard_navigation(tk_root):
    palette = get_palette("cyan")
    selected = []
    rail = TabRail(tk_root, palette=palette, on_select=lambda tid: selected.append(tid))
    rail.pack()
    tk_root.update_idletasks()

    rail.select_tab("today", trigger_callback=False)
    assert rail._focused_idx == 0

    # Key down
    rail._on_key_down(None)
    assert rail._focused_idx == 1

    # Enter triggers selection
    rail._on_key_enter(None)
    assert rail.get_active_tab() == "activity"
    assert selected == ["activity"]

    # Key up
    rail._on_key_up(None)
    assert rail._focused_idx == 0
    rail._on_key_enter(None)
    assert rail.get_active_tab() == "today"


def test_tabrail_mousewheel_scrolling(tk_root):
    palette = get_palette("cyan")
    rail = TabRail(tk_root, palette=palette)
    rail.pack()
    tk_root.update_idletasks()

    event = type("MockEvent", (), {"delta": -120})()
    rail._on_mousewheel(event)
    # Scroll offset should be negative or bounded
    assert rail._scroll_offset_y <= 0
