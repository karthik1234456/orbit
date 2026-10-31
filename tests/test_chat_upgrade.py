"""Tests for Chat Panel upgrade: tags, 500-msg cap, code blocks, search, streaming caret, thinking indicator, hover actions, auto-scroll, status chips."""

import tkinter as tk
from tkinter import scrolledtext
import pytest

from tests.conftest import make_test_root
from ui.theme import get_palette, style_chat_tags
from ui.controller import UiEvent


@pytest.fixture
def tk_root():
    root = make_test_root()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


class FakeController:
    def __init__(self):
        self.is_busy = False
        self.events = []
        self.app = None

    def submit(self, text, source="ui"):
        return True

    def shutdown(self):
        pass


class FakeVoiceController:
    state = "idle"

    def is_speaking(self):
        return False

    def speak_reply(self, text):
        pass

    def shutdown(self):
        pass


def test_style_chat_tags(tk_root):
    palette = get_palette("cyan")
    text_widget = scrolledtext.ScrolledText(tk_root)
    style_chat_tags(text_widget, palette)

    configured_tags = text_widget.tag_names()
    assert "user" in configured_tags
    assert "orbit" in configured_tags
    assert "orbit_prefix" in configured_tags
    assert "code_block" in configured_tags
    assert "dim_ts" in configured_tags
    assert "search_highlight" in configured_tags
    assert "streaming_caret" in configured_tags
    assert "thinking" in configured_tags


def test_chat_500_message_cap(tk_root):
    from ui.app import OrbitWindow
    ctrl = FakeController()
    win = OrbitWindow(tk_root, ctrl, voice_controller=FakeVoiceController(), mode_label="MOCK")

    # Append 550 messages
    for i in range(550):
        win._append_chat(f"Message {i}\n", ("user",))

    # The widget message count must be bounded
    assert win._chat_message_count <= 500
    # Oldest message should not be Message 0
    raw_content = win.history.get("1.0", tk.END)
    assert "Message 0\n" not in raw_content
    assert "Message 549" in raw_content


def test_chat_code_block_formatting(tk_root):
    from ui.app import OrbitWindow
    ctrl = FakeController()
    win = OrbitWindow(tk_root, ctrl, voice_controller=FakeVoiceController(), mode_label="MOCK")

    win._append_chat("Here is code:\n```\nprint('hello')\n```\nDone", ("orbit",))
    raw = win.history.get("1.0", tk.END)
    assert "print('hello')" in raw

    # Check that code_block tag is applied
    ranges = win.history.tag_ranges("code_block")
    assert len(ranges) >= 2


def test_chat_streaming_caret_and_thinking(tk_root):
    from ui.app import OrbitWindow
    ctrl = FakeController()
    win = OrbitWindow(tk_root, ctrl, voice_controller=FakeVoiceController(), mode_label="MOCK")

    # Show thinking indicator
    win._show_thinking_indicator()
    assert "thinking" in win.history.get("1.0", tk.END).lower()

    # Streaming delta arrives
    win._handle_event(UiEvent("delta", {"delta": "Hello "}))
    assert win._streaming is True
    assert win._caret_mark is not None

    win._handle_event(UiEvent("delta", {"delta": "world!"}))
    # Reply finishes stream
    win._handle_event(UiEvent("reply", {"text": "Hello world!"}))
    assert win._streaming is False
    assert win._caret_mark is None
    assert getattr(win, "_thinking_mark", None) is None


def test_chat_search_bar(tk_root):
    from ui.app import OrbitWindow
    ctrl = FakeController()
    win = OrbitWindow(tk_root, ctrl, voice_controller=FakeVoiceController(), mode_label="MOCK")

    win._append_chat("First sentence with target keyword.\n", ("orbit",))
    win._append_chat("Second sentence without it.\n", ("user",))
    win._append_chat("Third sentence also has target keyword.\n", ("orbit",))

    win._show_search()
    assert win.search_frame.winfo_manager() == "pack"

    win.search_entry.delete(0, tk.END)
    win.search_entry.insert(0, "target")

    win._find_next()
    assert len(win._search_matches) == 2
    assert win._search_idx == 0

    win._find_next()
    assert win._search_idx == 1

    win._find_prev()
    assert win._search_idx == 0

    win._hide_search()
    assert win.search_frame.winfo_manager() != "pack"


def test_chat_jump_to_latest_pill(tk_root):
    from ui.app import OrbitWindow
    ctrl = FakeController()
    win = OrbitWindow(tk_root, ctrl, voice_controller=FakeVoiceController(), mode_label="MOCK")

    # Populate lots of lines
    for i in range(100):
        win._append_chat(f"Line {i}\n", ("orbit",))

    # Trigger jump to latest
    win._auto_scroll_enabled = False
    win._on_jump_to_latest()
    assert win._auto_scroll_enabled is True


def test_chat_hover_actions(tk_root):
    from ui.app import OrbitWindow
    ctrl = FakeController()
    win = OrbitWindow(tk_root, ctrl, voice_controller=FakeVoiceController(), mode_label="MOCK")

    win._append_chat("Hover test message\n", ("orbit",))
    assert len(win._recent_chat_messages) >= 1
    recent = win._recent_chat_messages[-1]
    assert recent["text"] == "Hover test message"

    win._hovered_msg_text = recent["text"]
    win._quote_hovered_msg()
    assert "> Hover test message" in win.input_text.get("1.0", tk.END)


def test_status_bar_chips(tk_root, monkeypatch):
    from config import settings
    from ui.app import OrbitWindow
    monkeypatch.setattr(settings, "UI_STATUS_CHIPS", 1)
    monkeypatch.setattr(settings, "UI_MOTION", "full")

    ctrl = FakeController()
    win = OrbitWindow(tk_root, ctrl, voice_controller=FakeVoiceController(), mode_label="MOCK")

    win._cpu_probe = lambda: 42.0
    win._mem_probe = lambda: 68.0
    win._battery_probe = lambda: type("FakeBat", (), {"percent": 90, "power_plugged": False})()

    win._tick_status_chips()
    assert "CPU: 42%" in win.chip_cpu.cget("text")
    assert "RAM: 68%" in win.chip_mem.cget("text")
    assert "BAT: 90%" in win.chip_bat.cget("text")
