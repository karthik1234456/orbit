"""Unit tests for browser navigation, container fallback, shorts separation,
shortcut error handling, bare verbs interception, and controller keyboard input.
"""

from pathlib import Path
import pytest

from browser.backend import FakeBackend
from browser.controller import BrowserController
from browser.guards import BrowserRefused
from browser.playwright_backend import _TAG_RESULTS_SCRIPT
from config.browser_sites import ALLOWED_BROWSER_KEYS
from config.sites import SITES
from core.agent import Agent
from core.shortcuts import (
    BARE_BROWSER_VERBS,
    ToolCallSpec,
    is_bare_browser_verb,
    parse_shortcut,
)
from tools.browser_tools import make_browser_tools
from tools.registry import ToolRegistry


class DummyProvider:
    def __init__(self):
        self.call_count = 0
        self.name = "MOCK"

    def generate(self, messages, tools=None, on_delta=None, should_stop=None):
        self.call_count += 1
        raise AssertionError("Model should NOT be called on shortcuts or bare browser verbs!")


def _create_test_agent(tmp_path: Path, backend=None):
    from security.audit import AuditLogger

    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    controller = BrowserController(
        backend_factory=(lambda: backend) if backend else (lambda: FakeBackend()),
        timeout_s=5.0,
    )
    if backend:
        controller._backend = backend

    registry = ToolRegistry()
    for t in make_browser_tools(controller):
        registry.register(t)

    provider = DummyProvider()
    agent = Agent(
        provider=provider,
        registry=registry,
        audit=audit,
        browser_controller=controller,
        command_shortcuts=True,
    )
    return agent, controller, provider


# --- 1. Container Fallback and Exclusion Rules in _TAG_RESULTS_SCRIPT ---

def test_tag_script_structure_and_fallback():
    """Verify _TAG_RESULTS_SCRIPT contains container fallback to document while keeping exclusions."""
    # Strict order
    assert "watchSelectors" in _TAG_RESULTS_SCRIPT
    assert "rendererSelectors" in _TAG_RESULTS_SCRIPT
    assert "shortsSelectors" in _TAG_RESULTS_SCRIPT
    assert "document.body" in _TAG_RESULTS_SCRIPT
    # Exclusions
    assert "navigation_guide" in _TAG_RESULTS_SCRIPT
    assert "ads_promoted" in _TAG_RESULTS_SCRIPT
    assert "shorts_shelves" in _TAG_RESULTS_SCRIPT
    assert "mix_playlist" in _TAG_RESULTS_SCRIPT
    assert "hidden_or_zero_size" in _TAG_RESULTS_SCRIPT
    assert "shorts_excluded" in _TAG_RESULTS_SCRIPT


# --- 2. Zero Results Telemetry Record in Controller ---

def test_zero_count_telemetry_in_controller(tmp_path: Path):
    """When click_result encounters 0 results, controller records last_failure telemetry."""
    backend = FakeBackend()
    backend.start()
    backend._current_url = "https://www.youtube.com/"
    backend._items = []  # 0 results
    backend._exclusions = {"shorts_shelf": 1, "ads": 2}
    backend._anchors_per_selector = {"a[href*='/watch?v=']": 0}

    controller = BrowserController(backend_factory=lambda: backend, timeout_s=5.0)
    controller._backend = backend

    with pytest.raises(BrowserRefused) as exc_info:
        controller.click_result(1)

    assert "No videos found on this page (youtube_home)." in str(exc_info.value)
    assert controller.last_failure is not None
    assert controller.last_failure["page_type"] == "youtube_home"
    assert controller.last_failure["url_host"] == "www.youtube.com"
    assert controller.last_failure["exclusions"]["ads"] == 2


# --- 3. New Shortcuts Mapping ---

def test_new_browser_shortcuts_parsing():
    """Verify new browser shortcuts map to correct tools and arguments."""
    apps = {}
    sites = dict(SITES)

    # History
    for phrase in ("come back", "go back", "back", "previous page"):
        spec = parse_shortcut(phrase, apps, sites, browser_enabled=True)
        assert spec is not None, f"Failed for {phrase}"
        assert spec.name == "browser_history"
        assert spec.args == {"action": "back"}

    for phrase in ("forward", "go forward"):
        spec = parse_shortcut(phrase, apps, sites, browser_enabled=True)
        assert spec is not None, f"Failed for {phrase}"
        assert spec.name == "browser_history"
        assert spec.args == {"action": "forward"}

    # Home
    for phrase in ("main menu", "home", "go home", "come to main menu", "youtube home"):
        spec = parse_shortcut(phrase, apps, sites, browser_enabled=True)
        assert spec is not None, f"Failed for {phrase}"
        assert spec.name == "open_website"
        assert "youtube" in spec.args["url"]

    # Next video
    for phrase in ("next video", "play the next video", "next"):
        spec = parse_shortcut(phrase, apps, sites, browser_enabled=True)
        assert spec is not None, f"Failed for {phrase}"
        assert spec.name == "browser_click_result"
        assert spec.args == {"n": 1, "next_video": True}

    # Shorts navigation
    for phrase in ("open shorts", "shorts", "open shorts section", "go to shorts"):
        spec = parse_shortcut(phrase, apps, sites, browser_enabled=True)
        assert spec is not None, f"Failed for {phrase}"
        assert spec.name == "open_website"
        assert spec.args["url"] == "https://www.youtube.com/shorts"

    # Shorts swiping
    for phrase in ("next short", "swipe up"):
        spec = parse_shortcut(phrase, apps, sites, browser_enabled=True)
        assert spec is not None, f"Failed for {phrase}"
        assert spec.name == "browser_key"
        assert spec.args == {"name": "ArrowDown"}

    spec_prev = parse_shortcut("previous short", apps, sites, browser_enabled=True)
    assert spec_prev is not None
    assert spec_prev.name == "browser_key"
    assert spec_prev.args == {"name": "ArrowUp"}


# --- 4. Command Verb Typo Tolerance (edit distance 1) ---

def test_command_verb_typo_tolerance():
    """Verify Damerau-Levenshtein edit-distance-1 tolerance on command verbs."""
    apps = {}
    sites = dict(SITES)

    # Verbs: back, pause, play, scroll, forward, swipe
    assert parse_shortcut("bak", apps, sites, browser_enabled=True).args == {"action": "back"}
    assert parse_shortcut("paus", apps, sites, browser_enabled=True).args == {"action": "pause"}
    assert parse_shortcut("pasue", apps, sites, browser_enabled=True).args == {"action": "pause"}
    assert parse_shortcut("paly", apps, sites, browser_enabled=True).args == {"action": "play"}
    assert parse_shortcut("scrol", apps, sites, browser_enabled=True).name == "browser_scroll"
    assert parse_shortcut("forwrd", apps, sites, browser_enabled=True).args == {"action": "forward"}
    assert parse_shortcut("swpie up", apps, sites, browser_enabled=True).args == {"name": "ArrowDown"}


# --- 5. Near-Misses Return None ---

def test_near_misses_return_none():
    """Phrases with shell characters, extra instructions, or multi-lines return None."""
    apps = {}
    sites = dict(SITES)

    assert parse_shortcut("come back and delete files", apps, sites, browser_enabled=True) is None
    assert parse_shortcut("next video; calc", apps, sites, browser_enabled=True) is None
    assert parse_shortcut("open shorts && rm -rf", apps, sites, browser_enabled=True) is None
    assert parse_shortcut("go home\nrm -rf /", apps, sites, browser_enabled=True) is None
    assert parse_shortcut("open https://example.com/next video", apps, sites, browser_enabled=True) is None


# --- 6. Bare Browser Verbs Interception When Browser Not Running ---

def test_bare_browser_verbs_when_browser_closed(tmp_path: Path):
    """Bare browser verbs when browser is not running return fixed message without calling model."""
    backend = FakeBackend()
    backend._alive = False  # Browser closed
    agent, controller, provider = _create_test_agent(tmp_path, backend)

    for verb in ("back", "forward", "pause", "play", "mute", "unmute", "scroll", "home"):
        reply = agent.chat(verb)
        assert reply == "No browser is open yet. Try 'open youtube'."
        assert provider.call_count == 0


# --- 7. Fixed Reply on Browser Shortcut Failure ---

def test_fixed_reply_on_browser_shortcut_failure(tmp_path: Path):
    """When a browser shortcut fails with no videos found, agent formats fixed plain reply."""
    backend = FakeBackend()
    backend.start()
    backend._current_url = "https://www.youtube.com/"
    backend._items = []  # 0 videos found

    agent, controller, provider = _create_test_agent(tmp_path, backend)

    reply = agent.chat("open first video")
    assert reply == "I couldn't find any videos on this page. Try 'scroll down' or search for something first."
    assert provider.call_count == 0


# --- 8. Next Video Refusal on Non-Watch Pages ---

def test_next_video_refusal_on_non_watch_page(tmp_path: Path):
    """'next video' is refused when not on a youtube_watch page."""
    backend = FakeBackend()
    backend.start()
    backend._current_url = "https://www.youtube.com/results?search_query=lofi"

    controller = BrowserController(backend_factory=lambda: backend, timeout_s=5.0)
    controller._backend = backend

    with pytest.raises(BrowserRefused, match="Next video is only available on YouTube watch pages."):
        controller.click_result(1, next_video=True)


# --- 9. Controller.key(name) Allowlist and Interactive Checks ---

def test_controller_key_validation(tmp_path: Path):
    """controller.key allows permitted keys on interactive pages and refuses others."""
    backend = FakeBackend()
    backend.start()
    backend._current_url = "https://www.youtube.com/shorts"

    controller = BrowserController(backend_factory=lambda: backend, timeout_s=5.0)
    controller._backend = backend

    # Permitted keys
    for k in ("ArrowDown", "ArrowUp", "k", "space"):
        res = controller.key(k)
        assert f"Pressed {k}." in res

    # Disallowed keys
    for bad_key in ("Enter", "Tab", "Escape", "a", "F5"):
        with pytest.raises(BrowserRefused, match="is not allowed. Permitted keys:"):
            controller.key(bad_key)

    # Refuse on non-interactive pages
    backend._current_url = "https://mail.google.com/"
    with pytest.raises(BrowserRefused):
        controller.key("ArrowDown")
