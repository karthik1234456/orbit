"""Comprehensive tests for browser shortcuts, multi-step execution chains, audit logs, and UI/CLI."""

from pathlib import Path
import re
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from browser.backend import FakeBackend
from browser.controller import BrowserController
from browser.guards import BrowserError, BrowserRefused
from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.agent import Agent, RunStatus
from core.llm import LLMProvider, LLMResponse
from core.shortcuts import ShortcutPlan, ToolCallSpec, parse_shortcut
from security.audit import AuditLogger
from security.permissions import PermissionLevel, PermissionPolicy
from tools.browser_tools import make_browser_tools
from tools.registry import Tool, ToolRegistry


class ExplodingProvider(LLMProvider):
    """Provider that raises an error if generate() is called."""

    def __init__(self):
        self.call_count = 0

    def generate(self, messages, tools=None, on_delta=None, should_stop=None) -> LLMResponse:
        self.call_count += 1
        raise AssertionError("LLMProvider.generate was called when shortcuts should have bypassed it!")


# --- 1. Parser Tests: Patterns and Flags ---

def test_browser_shortcuts_disabled_by_default():
    """Verify that browser shortcuts return None when browser_enabled is False."""
    phrases = [
        "scroll down",
        "scroll up",
        "scroll to top",
        "scroll to the bottom",
        "scroll down 2 screens",
        "pause",
        "pause the video",
        "play",
        "resume",
        "play the video",
        "mute",
        "unmute",
        "go back",
        "go forward",
        "reload",
        "refresh",
        "play the first video",
        "open the second result",
        "click number 1",
        "what's on this page",
        "read the page",
        "close browser",
        "close the browser",
        "open youtube and play free fire videos",
        "open youtube and search for trending videos",
    ]
    for p in phrases:
        assert parse_shortcut(p, ALLOWED_APPS, SITES, browser_enabled=False) is None, f"Expected None for: {p}"

    # Default parameter must be False
    assert parse_shortcut("scroll down", ALLOWED_APPS, SITES) is None

    # Normal shortcuts still work when browser_enabled=False
    spec = parse_shortcut("open notepad", ALLOWED_APPS, SITES, browser_enabled=False)
    assert spec is not None
    assert spec.name == "open_application"


def test_scroll_patterns_map_correctly():
    """Verify scrolling patterns map to browser_scroll with direction and screens."""
    spec = parse_shortcut("scroll down", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "down", "screens": 1}

    spec = parse_shortcut("scroll up", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "up", "screens": 1}

    spec = parse_shortcut("scroll to the top", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "top", "screens": 1}

    spec = parse_shortcut("scroll to top", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "top", "screens": 1}

    spec = parse_shortcut("scroll to the bottom", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "bottom", "screens": 1}

    spec = parse_shortcut("scroll down 3 screens", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "down", "screens": 3}

    spec = parse_shortcut("scroll down 5 pages", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "down", "screens": 5}

    spec = parse_shortcut("scroll up two screens", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_scroll"
    assert spec.args == {"direction": "up", "screens": 2}


def test_media_patterns_map_correctly():
    """Verify media controls map to browser_media with exact actions."""
    spec = parse_shortcut("pause", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_media"
    assert spec.args == {"action": "pause"}

    spec = parse_shortcut("pause the video", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_media"
    assert spec.args == {"action": "pause"}

    spec = parse_shortcut("play", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_media"
    assert spec.args == {"action": "play"}

    spec = parse_shortcut("resume", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_media"
    assert spec.args == {"action": "play"}

    spec = parse_shortcut("play the video", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_media"
    assert spec.args == {"action": "play"}

    spec = parse_shortcut("mute", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_media"
    assert spec.args == {"action": "mute"}

    spec = parse_shortcut("unmute", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_media"
    assert spec.args == {"action": "unmute"}


def test_history_patterns_map_correctly():
    """Verify history navigation maps to browser_history."""
    spec = parse_shortcut("go back", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_history"
    assert spec.args == {"action": "back"}

    spec = parse_shortcut("go forward", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_history"
    assert spec.args == {"action": "forward"}

    spec = parse_shortcut("reload", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_history"
    assert spec.args == {"action": "reload"}

    spec = parse_shortcut("refresh", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_history"
    assert spec.args == {"action": "reload"}


def test_click_result_patterns_map_correctly():
    """Verify click result patterns map ordinals and numbers 1-10 to browser_click_result."""
    for verb in ("play", "open", "click"):
        # Ordinals
        spec = parse_shortcut(f"{verb} the first video", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 1}

        spec = parse_shortcut(f"{verb} the second result", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 2}

        spec = parse_shortcut(f"{verb} the third one", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 3}

        spec = parse_shortcut(f"{verb} the fourth video", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 4}

        spec = parse_shortcut(f"{verb} the fifth result", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 5}

        spec = parse_shortcut(f"{verb} the top one", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 1}

        # Numbers
        spec = parse_shortcut(f"{verb} number 1", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 1}

        spec = parse_shortcut(f"{verb} number 7", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 7}

        spec = parse_shortcut(f"{verb} number 10", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 10}

        spec = parse_shortcut(f"{verb} number two", ALLOWED_APPS, SITES, browser_enabled=True)
        assert isinstance(spec, ToolCallSpec)
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 2}


def test_read_page_and_close_patterns_map_correctly():
    """Verify read page and close patterns map to browser_read_page and browser_close."""
    spec = parse_shortcut("what's on this page", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_read_page"

    spec = parse_shortcut("whats on this page", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_read_page"

    spec = parse_shortcut("read the page", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_read_page"

    spec = parse_shortcut("close browser", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_close"

    spec = parse_shortcut("close the browser", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_close"


def test_chain_patterns_map_correctly():
    """Verify Form A and Form B chains map to ShortcutPlan with correct steps."""
    # Play chain Form A: "open|go to <site> and play <query>"
    plan = parse_shortcut("open youtube and play free fire videos", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(plan, ShortcutPlan)
    assert plan.site == "youtube"
    assert plan.query == "free fire videos"
    assert plan.is_play is True
    assert len(plan.steps) == 3
    assert plan.steps[0].name == "search_site"
    assert plan.steps[0].args == {"site": "youtube", "query": "free fire videos"}
    assert plan.steps[1].name == "browser_click_result"
    assert plan.steps[1].args == {"n": 1}
    assert plan.steps[2].name == "browser_media"
    assert plan.steps[2].args == {"action": "play"}

    # Play chain with alias "yt"
    plan = parse_shortcut("go to yt and play lofi beats", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(plan, ShortcutPlan)
    assert plan.site == "youtube"
    assert plan.query == "lofi beats"
    assert plan.is_play is True

    # Play chain Form B: "play <query> on <site>"
    plan = parse_shortcut("play lofi music on youtube", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(plan, ShortcutPlan)
    assert plan.site == "youtube"
    assert plan.query == "lofi music"
    assert plan.is_play is True
    assert len(plan.steps) == 3
    assert plan.steps[0].name == "search_site"
    assert plan.steps[0].args == {"site": "youtube", "query": "lofi music"}

    # Search chain: "open|go to <site> and search for <query>"
    plan = parse_shortcut("open youtube and search for trending videos", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(plan, ShortcutPlan)
    assert plan.site == "youtube"
    assert plan.query == "trending videos"
    assert plan.is_play is False
    assert len(plan.steps) == 1
    assert plan.steps[0].name == "search_site"
    assert plan.steps[0].args == {"site": "youtube", "query": "trending videos"}

    plan = parse_shortcut("go to wikipedia and search for albert einstein", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(plan, ShortcutPlan)
    assert plan.site == "wikipedia"
    assert plan.query == "albert einstein"
    assert plan.is_play is False
    assert len(plan.steps) == 1


def test_near_misses_return_none():
    """Verify that near-misses, dangerous characters, URLs, and out-of-range arguments return None."""
    near_misses = [
        "scroll down and delete my files",
        "play the first video; calc",
        "play https://x.com on youtube",
        "open youtube and play",
        "scroll down 6 screens",
        "scroll down 0 screens",
        "play number 11",
        "play number 0",
        "play something on unknownsite",
        "open gmail and play inbox",  # gmail has search: None
        "scroll sideways",
        "play one two three four five six seven eight nine on youtube",  # 9 words > 8
        "open youtube and play ",
    ]
    for nm in near_misses:
        assert parse_shortcut(nm, ALLOWED_APPS, SITES, browser_enabled=True) is None, f"Expected None for: {nm}"


# --- 2. Agent Execution Tests ---

def _build_test_agent(tmp_path: Path, backend: FakeBackend):
    """Helper to wire an Agent with full browser tools and audit logging."""
    controller = BrowserController(backend_factory=lambda: backend, timeout_s=5.0)
    registry = ToolRegistry()
    for t in make_browser_tools(controller, sites=SITES):
        registry.register(t)

    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(path=str(audit_file))
    provider = ExplodingProvider()

    agent = Agent(
        provider=provider,
        registry=registry,
        audit=audit,
        sites=SITES,
        command_shortcuts=True,
        browser_controller=controller,
    )
    return agent, controller, audit, provider


def test_play_chain_execution_success_and_audited(tmp_path: Path):
    """Verify full play chain executes 3 steps, audits all as 'shortcut', and returns success reply."""
    backend = FakeBackend()
    agent, controller, audit, provider = _build_test_agent(tmp_path, backend)

    try:
        reply = agent.chat("open youtube and play free fire videos")
        assert reply == "Searched YouTube for free fire videos and started the first result."
        assert provider.call_count == 0

        # Check last run
        assert agent.last_run is not None
        assert agent.last_run.status == RunStatus.COMPLETED
        assert len(agent.last_run.steps) == 3
        assert agent.last_run.steps[0].tool == "search_site"
        assert agent.last_run.steps[1].tool == "browser_click_result"
        assert agent.last_run.steps[2].tool == "browser_media"
        for step in agent.last_run.steps:
            assert step.status == "success"
            assert step.decision == "shortcut"

        # Check audit log
        records = audit.read_all()
        assert len(records) == 3
        assert records[0]["tool"] == "search_site"
        assert records[0]["decision"] == "shortcut"
        assert records[1]["tool"] == "browser_click_result"
        assert records[1]["decision"] == "shortcut"
        assert records[2]["tool"] == "browser_media"
        assert records[2]["decision"] == "shortcut"
    finally:
        controller.shutdown()


def test_play_chain_stops_at_first_failed_step(tmp_path: Path):
    """Verify chain stops immediately when click_result fails, without calling media."""
    backend = FakeBackend()
    # Mock click_result on backend to raise an error
    backend.click_result = MagicMock(side_effect=BrowserError("Result 1 not found"))
    agent, controller, audit, provider = _build_test_agent(tmp_path, backend)

    try:
        reply = agent.chat("play lofi music on youtube")
        assert (
            reply == "Searched YouTube for lofi music, but I couldn't start a video: Browser operation failed: Result 1 not found."
            or reply == "Searched YouTube for lofi music, but I couldn't start a video: Result 1 not found."
        )
        assert provider.call_count == 0

        # Steps: search_site succeeded, click_result failed, browser_media never called
        assert len(agent.last_run.steps) == 2
        assert agent.last_run.steps[0].tool == "search_site"
        assert agent.last_run.steps[0].status == "success"
        assert agent.last_run.steps[1].tool == "browser_click_result"
        assert agent.last_run.steps[1].status == "error"

        records = audit.read_all()
        assert len(records) == 2
        assert records[0]["tool"] == "search_site"
        assert records[1]["tool"] == "browser_click_result"
    finally:
        controller.shutdown()


def test_play_chain_media_failure_is_non_fatal(tmp_path: Path):
    """Verify final media(play) step failure is non-fatal and returns the fallback reply."""
    backend = FakeBackend()
    backend.media = MagicMock(side_effect=BrowserError("No HTML5 video element on page"))
    agent, controller, audit, provider = _build_test_agent(tmp_path, backend)

    try:
        reply = agent.chat("open youtube and play lofi beats")
        assert reply == "Searched YouTube for lofi beats and opened the first result; press play if it didn't start."
        assert provider.call_count == 0

        assert agent.last_run.status == RunStatus.COMPLETED
        assert len(agent.last_run.steps) == 3
        assert agent.last_run.steps[2].tool == "browser_media"
        assert agent.last_run.steps[2].status == "error"

        records = audit.read_all()
        assert len(records) == 3
    finally:
        controller.shutdown()


def test_refused_interaction_gives_friendly_refusal_text(tmp_path: Path):
    """Verify that a refused interaction (page not interactive) gives the friendly refusal message."""
    backend = FakeBackend()
    # Simulate current page being a non-interactive domain (e.g. example.com)
    backend.current_url = MagicMock(return_value="https://example.com")
    agent, controller, audit, provider = _build_test_agent(tmp_path, backend)

    try:
        reply = agent.chat("open youtube and play cute puppies")
        assert (
            "I can only control YouTube, Google, Wikipedia and GitHub pages" in reply
        )
        assert reply.startswith("Searched YouTube for cute puppies, but I couldn't start a video:")

        # Also verify single shortcut on non-interactive page returns friendly refusal
        reply_single = agent.chat("play the first video")
        assert reply_single == "I can only control YouTube, Google, Wikipedia and GitHub pages"
    finally:
        controller.shutdown()


def test_search_chain_execution(tmp_path: Path):
    """Verify search chain executes search_site and returns fixed search reply."""
    backend = FakeBackend()
    agent, controller, audit, provider = _build_test_agent(tmp_path, backend)

    try:
        reply = agent.chat("open youtube and search for trending videos")
        assert reply == "Searched YouTube for trending videos."
        assert provider.call_count == 0

        assert len(agent.last_run.steps) == 1
        assert agent.last_run.steps[0].tool == "search_site"
        assert agent.last_run.steps[0].status == "success"

        records = audit.read_all()
        assert len(records) == 1
        assert records[0]["tool"] == "search_site"
    finally:
        controller.shutdown()


def test_single_browser_shortcuts_execution(tmp_path: Path):
    """Verify single browser action shortcuts return their formatted output and model is never called."""
    backend = FakeBackend()
    agent, controller, audit, provider = _build_test_agent(tmp_path, backend)

    try:
        # Navigate first so page is interactive
        controller.navigate("https://www.youtube.com")

        # Scroll
        reply = agent.chat("scroll down")
        assert re.match(r"^Scrolled down \d+ px\.$", reply)
        assert provider.call_count == 0

        # Media pause
        reply = agent.chat("pause the video")
        assert reply in ("Paused.", "Media action 'pause' executed.")
        assert provider.call_count == 0

        # Media play
        reply = agent.chat("resume")
        assert reply in ("Playing.", "Media action 'play' executed.")
        assert provider.call_count == 0

        # History
        reply = agent.chat("go back")
        assert reply in ("No earlier page.", "Browser history action 'back' executed.", "youtube_home")
        assert provider.call_count == 0

        # Read page
        reply = agent.chat("what's on this page")
        assert "Page:" in reply or "Results:" in reply
        assert provider.call_count == 0

        # Close browser
        reply = agent.chat("close browser")
        assert reply == "Closed browser."
        assert provider.call_count == 0
    finally:
        controller.shutdown()


# --- 3. UI and CLI Tests ---

def test_ui_context_tab_browser_field():
    """Verify UI Context tab contains Browser field showing host and title when open."""
    import tkinter as tk
    from ui.app import OrbitWindow
    from ui.controller import ChatController

    root = tk.Tk()
    root.withdraw()
    try:
        ctrl = ChatController()
        backend = FakeBackend()
        backend.current_url = MagicMock(return_value="https://www.youtube.com/watch?v=123")
        backend.title = MagicMock(return_value="Orbit Video")
        backend.is_alive = MagicMock(return_value=True)

        browser_ctrl = BrowserController(backend_factory=lambda: backend, timeout_s=5.0)
        # Pre-seed backend so status() sees it alive
        browser_ctrl._backend = backend

        class DummyAgent:
            def __init__(self):
                from core.session_state import SessionState
                self.session_state = SessionState()
                self.browser_controller = browser_ctrl

        class DummyApp:
            def __init__(self, ag):
                self.agent = ag
                self.browser_controller = browser_ctrl

        ctrl.app = DummyApp(DummyAgent())
        win = OrbitWindow(root, ctrl, mode_label="MOCK")

        win._refresh_context_tab()
        assert "browser" in win.context_vars
        assert win.context_vars["browser"].get() == "www.youtube.com | Orbit Video"

        # When closed
        backend.is_alive = MagicMock(return_value=False)
        win._refresh_context_tab()
        assert win.context_vars["browser"].get() == "—"
    finally:
        root.destroy()


def test_intent_probe_phrases_included():
    """Verify BROWSER_PROBE_PHRASES are correctly defined in scripts/intent_probe.py."""
    from scripts.intent_probe import BROWSER_PROBE_PHRASES

    expected = [
        ("scroll down", ["browser_scroll"]),
        ("pause the video", ["browser_media"]),
        ("play the first video", ["browser_click_result"]),
        ("mute", ["browser_media"]),
        ("go back", ["browser_history"]),
        ("open youtube and play free fire videos", ["open_website", "search_site"]),
        ("play lofi music on youtube", ["search_site"]),
        ("what's on this page", ["browser_read_page"]),
        ("close the browser", ["browser_close"]),
    ]
    probe_dict = {phrase: accept for phrase, accept in BROWSER_PROBE_PHRASES}
    for phrase, accept in expected:
        assert phrase in probe_dict, f"Missing phrase: {phrase}"
        assert probe_dict[phrase] == accept, f"Wrong accept list for {phrase}: {probe_dict[phrase]} != {accept}"


def test_cli_browser_commands(capsys):
    """Verify /browser and /browser close CLI commands in main.py."""
    backend = FakeBackend()
    backend.start()
    backend._current_url = "https://en.wikipedia.org/wiki/Orbit"
    backend._title = "Orbit - Wikipedia"

    controller = BrowserController(backend_factory=lambda: backend, timeout_s=5.0)
    controller._backend = backend

    # Test /browser when open
    stat = controller.status()
    assert stat["alive"] is True
    from urllib.parse import urlparse
    host = urlparse(stat["url"]).netloc
    status_str = f"Browser: {host} | {stat['title']}"
    assert status_str == "Browser: en.wikipedia.org | Orbit - Wikipedia"

    # Test /browser close
    controller.close()
    assert backend.is_alive() is False

    # Test /browser when closed
    stat_closed = controller.status()
    assert stat_closed["alive"] is False


def test_ordinal_shortcuts_parsing_and_constraints():
    """Verify ordinal search+click+play pattern D and negative constraint cases."""
    # 1. "play the first video rathinamo" -> search + click 1 + play on youtube
    plan = parse_shortcut("play the first video rathinamo", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(plan, ShortcutPlan)
    assert plan.site == "youtube"
    assert plan.query == "rathinamo"
    assert plan.ordinal == 1
    assert plan.is_play is True
    assert getattr(plan, "style", "") == "ordinal"
    assert len(plan.steps) == 3
    assert plan.steps[0].name == "search_site"
    assert plan.steps[0].args == {"site": "youtube", "query": "rathinamo"}
    assert plan.steps[1].name == "browser_click_result"
    assert plan.steps[1].args == {"n": 1}
    assert plan.steps[2].name == "browser_media"
    assert plan.steps[2].args == {"action": "play"}

    # 2. "play second video" on current page -> single click step
    spec = parse_shortcut("play second video", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec, ToolCallSpec)
    assert spec.name == "browser_click_result"
    assert spec.args == {"n": 2}

    # 3. Explicit site with preposition: "open the third result for quantum computing on wikipedia"
    plan_wiki = parse_shortcut(
        "open the third result for quantum computing on wikipedia",
        ALLOWED_APPS,
        SITES,
        browser_enabled=True,
    )
    assert isinstance(plan_wiki, ShortcutPlan)
    assert plan_wiki.site == "wikipedia"
    assert plan_wiki.query == "quantum computing"
    assert plan_wiki.ordinal == 3

    # 4. Bare "down" and "up" scroll shortcuts
    spec_down = parse_shortcut("down", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec_down, ToolCallSpec)
    assert spec_down.name == "browser_scroll"
    assert spec_down.args == {"direction": "down", "screens": 1}

    spec_up = parse_shortcut("up", ALLOWED_APPS, SITES, browser_enabled=True)
    assert isinstance(spec_up, ToolCallSpec)
    assert spec_up.name == "browser_scroll"
    assert spec_up.args == {"direction": "up", "screens": 1}

    # 5. Invalid / near-misses must return None
    assert parse_shortcut("play the first video rathinamo; calc", ALLOWED_APPS, SITES, browser_enabled=True) is None
    assert parse_shortcut("play the first video of", ALLOWED_APPS, SITES, browser_enabled=True) is None
    assert parse_shortcut("play the first video from", ALLOWED_APPS, SITES, browser_enabled=True) is None
    assert parse_shortcut("play the first video for", ALLOWED_APPS, SITES, browser_enabled=True) is None


def test_ordinal_plan_agent_execution_and_verified_replies(tmp_path: Path):
    """Verify Agent execution of ordinal plans returns verified reply or failure explanation."""
    backend = FakeBackend()
    agent, controller, audit, provider = _build_test_agent(tmp_path, backend)

    try:
        # Case A: Success returns "Opened result 1: <title>. Playing."
        reply = agent.chat("play the first video rathinamo")
        assert reply == "Opened result 1: Result 1. Playing."
        assert provider.call_count == 0
        assert len(agent.last_run.steps) == 3
        assert agent.last_run.steps[0].tool == "search_site"
        assert agent.last_run.steps[1].tool == "browser_click_result"
        assert agent.last_run.steps[2].tool == "browser_media"

        # Case B: "play second video" on results page gives single click step
        reply_click = agent.chat("play second video")
        assert reply_click == "Opened result 2: Result 2"
        assert len(agent.last_run.steps) == 1
        assert agent.last_run.steps[0].tool == "browser_click_result"

        # Case C: Autoplay blocked leaves video paused and returns friendly fallback
        backend._autoplay_blocked = True
        backend._video_state["paused"] = True
        reply_autoplay = agent.chat("play the first video rathinamo")
        assert reply_autoplay == "Opened result 1: Result 1. The video is open but didn't start; press play."

        # Case D: click_result fails -> stops and explains why
        backend._autoplay_blocked = False
        backend.click_result = MagicMock(side_effect=BrowserRefused("I only found 0 results on this page."))
        reply_fail = agent.chat("play the first video rathinamo")
        assert reply_fail == "Searched YouTube for rathinamo, but I couldn't open result 1: I only found 0 results on this page."
        assert len(agent.last_run.steps) == 2
        assert agent.last_run.steps[1].tool == "browser_click_result"
        assert agent.last_run.steps[1].status == "error"
    finally:
        controller.shutdown()

