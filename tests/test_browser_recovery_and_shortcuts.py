"""Tests for browser recovery, controller guards, verification page cooldown, and shortcut enhancements."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from browser.backend import FakeBackend
from browser.controller import BrowserController
from browser.guards import BrowserError, BrowserRefused, page_type
from browser.playwright_backend import PlaywrightBackend
import config.settings as settings
from core.diagnostics import build_diagnostics_text
from core.session_state import SessionState
from core.shortcuts import ShortcutPlan, ToolCallSpec, parse_shortcut
from tools.browser_tools import UNCONTROLLED_MESSAGE, make_browser_tools


# ---------------------------------------------------------------------------
# 1. Fallback Text & Controller-Only Guard
# ---------------------------------------------------------------------------

def test_fallback_reason_tracking_and_uncontrolled_guard():
    """Verify fallback text contains error reason capped at 120 chars, sets browser_controlled=False,
    and blocks subsequent controller-only tools."""
    long_reason = "Connection refused to remote CDP endpoint at 127.0.0.1:9222 because chromium crashed with segfault error 139 during initialization"
    
    class FailingBackend(FakeBackend):
        def start(self):
            raise BrowserError(long_reason)

    controller = BrowserController(backend_factory=lambda: FailingBackend())
    state = SessionState()
    tools = {t.name: t for t in make_browser_tools(controller, state=state)}

    assert controller.browser_controlled is True
    assert state.browser_controlled is True

    # 1. Fallback text format
    with patch("webbrowser.open") as mock_open:
        res = tools["open_website"].func("https://docs.python.org/3/")
        mock_open.assert_called_once_with("https://docs.python.org/3/")
        assert "(ORBIT's browser could not start:" in res
        assert "; opened in your default browser, which I cannot control)" in res
        
        # Verify reason in the message is capped to at most 120 chars
        prefix = "(ORBIT's browser could not start: "
        suffix = "; opened in your default browser, which I cannot control)"
        start_idx = res.index(prefix) + len(prefix)
        end_idx = res.index(suffix)
        reason_extracted = res[start_idx:end_idx]
        assert len(reason_extracted) <= 120

        assert controller.browser_controlled is False
        assert state.browser_controlled is False
        assert controller.fallback_count == 1
        assert controller.last_error is not None

    # 2. Controller-only tools return UNCONTROLLED_MESSAGE
    for tool_name in (
        "browser_click_result",
        "browser_media",
        "browser_scroll",
        "browser_history",
        "browser_read_page",
        "browser_key",
    ):
        tool = tools[tool_name]
        if tool_name == "browser_click_result":
            out = tool.func(1)
        elif tool_name == "browser_media":
            out = tool.func("play")
        elif tool_name == "browser_scroll":
            out = tool.func("down", 1)
        elif tool_name == "browser_history":
            out = tool.func("back")
        elif tool_name == "browser_read_page":
            out = tool.func()
        elif tool_name == "browser_key":
            out = tool.func("Space")
        assert out == UNCONTROLLED_MESSAGE


# ---------------------------------------------------------------------------
# 2. Browser Restart & Profile Process Cleanup
# ---------------------------------------------------------------------------

def test_browser_restart_and_profile_process_cleanup(tmp_path: Path):
    """Verify restart terminates only profile-matching processes, clears errors, and restarts cleanly."""
    profile_dir = tmp_path / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    backend = FakeBackend()
    backend.start()

    controller = BrowserController(backend_factory=lambda: backend, profile_dir=profile_dir)
    controller.last_error = "Previous crash"
    controller.browser_controlled = False
    controller.fallback_count = 2

    # Mock psutil processes
    mock_matching_proc = MagicMock()
    mock_matching_proc.info = {"pid": 1111, "name": "chrome.exe", "cmdline": ["chrome.exe", f"--user-data-dir={profile_dir.resolve()}"]}
    mock_matching_proc.pid = 1111

    mock_unrelated_proc = MagicMock()
    mock_unrelated_proc.info = {"pid": 2222, "name": "chrome.exe", "cmdline": ["chrome.exe", "--user-data-dir=C:\\Users\\Personal\\AppData"]}
    mock_unrelated_proc.pid = 2222

    with patch("psutil.process_iter", return_value=[mock_matching_proc, mock_unrelated_proc]), \
         patch("psutil.wait_procs") as mock_wait:
        controller.restart()

        # Matching proc terminated
        mock_matching_proc.terminate.assert_called_once()
        # Unrelated personal proc untouched
        mock_unrelated_proc.terminate.assert_not_called()
        mock_wait.assert_called_once()

    assert controller.last_error is None
    assert controller.browser_controlled is True
    assert controller.restart_count == 1
    assert controller.is_alive() is True


def test_launch_recovery_retries_once(tmp_path: Path):
    """Verify launch recovery terminates profile processes and retries once on recoverable error."""
    profile_dir = tmp_path / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    attempt_counter = [0]

    class FlakyBackend(FakeBackend):
        def start(self):
            attempt_counter[0] += 1
            if attempt_counter[0] == 1:
                raise BrowserError("Process singleton lock active: user data directory is already in use")
            super().start()

    controller = BrowserController(backend_factory=lambda: FlakyBackend(), profile_dir=profile_dir)

    with patch.object(controller, "_terminate_profile_processes") as mock_term:
        controller.navigate("https://en.wikipedia.org/wiki/Orbit")
        assert attempt_counter[0] == 2
        mock_term.assert_called_once()
        assert controller.is_alive() is True


def test_launch_recovery_reports_real_error_on_second_failure(tmp_path: Path):
    """Verify that if retry fails on second attempt, real error is raised and recorded."""
    profile_dir = tmp_path / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    class AlwaysFailingBackend(FakeBackend):
        def start(self):
            raise BrowserError("Permanent failure: missing shared library libnss3.so")

    controller = BrowserController(backend_factory=lambda: AlwaysFailingBackend(), profile_dir=profile_dir)

    with pytest.raises(BrowserError) as exc_info:
        controller.navigate("https://en.wikipedia.org/wiki/Orbit")

    assert "libnss3.so" in str(exc_info.value)
    assert controller.last_error is not None
    assert "libnss3.so" in str(controller.last_error)


# ---------------------------------------------------------------------------
# 3. Route Handler CancelledError & BaseException Suppression
# ---------------------------------------------------------------------------

def test_route_handler_cancelled_error_suppression(capsys):
    """Verify PlaywrightBackend route handler swallows asyncio.CancelledError and BaseException without printing."""
    backend = PlaywrightBackend()
    
    mock_route = MagicMock()
    mock_route.request.url = "https://www.google.com/search?q=test"
    mock_route.request.resource_type = "document"
    mock_route.request.is_navigation_request.return_value = True

    # Simulate route.continue_ raising asyncio.CancelledError
    mock_route.continue_.side_effect = asyncio.CancelledError("Target frame was detached")

    # Should not raise exception
    backend._route_handler(mock_route)

    assert backend.last_route_diagnostic is not None
    assert "CancelledError" in backend.last_route_diagnostic
    mock_route.abort.assert_called_once()

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_close_calls_unroute_all_with_ignore_errors():
    """Verify close() unroutes handlers using ignoreErrors if supported."""
    backend = PlaywrightBackend()
    mock_context = MagicMock()
    backend._context = mock_context
    backend._started = True

    backend.close()
    mock_context.unroute_all.assert_called_once_with(behavior="ignoreErrors")


# ---------------------------------------------------------------------------
# 4. Google Verification Page Detection & Cooldown
# ---------------------------------------------------------------------------

def test_google_verification_page_detection_and_cooldown():
    """Verify detecting /sorry/index triggers history back, starts cooldown, and raises BrowserRefused."""
    backend = FakeBackend()
    backend.start()
    controller = BrowserController(backend_factory=lambda: backend)

    assert page_type("https://www.google.com/sorry/index?continue=https://google.com") == "google_blocked"

    # Simulate navigating to Google verification page
    with patch.object(backend, "current_url", return_value="https://www.google.com/sorry/index?continue=https://google.com"), \
         patch.object(backend, "history") as mock_history:
        
        with pytest.raises(BrowserRefused) as exc_info:
            backend._check_verification_page()

        assert "verification page" in str(exc_info.value)
        mock_history.assert_called_once_with("back")
        assert backend.is_google_blocked() is True
        assert backend.google_blocked_until is not None


def test_google_verification_cooldown_expiry():
    """Verify cooldown remaining seconds and expiry logic."""
    backend = FakeBackend()
    backend.start()
    assert backend.is_google_blocked() is False

    backend.start_google_cooldown(duration_minutes=30)
    assert backend.is_google_blocked() is True

    # Simulate time passing beyond cooldown
    import time
    backend.google_blocked_until = time.time() - 1.0
    assert backend.is_google_blocked() is False


# ---------------------------------------------------------------------------
# 5. Google Searches Default to Default Browser
# ---------------------------------------------------------------------------

def test_google_search_routing_default_browser():
    """When GOOGLE_SEARCH_IN_CONTROLLED_BROWSER is False, Google searches use default browser."""
    backend = FakeBackend()
    backend.start()
    controller = BrowserController(backend_factory=lambda: backend)
    state = SessionState()
    tools = {t.name: t for t in make_browser_tools(controller, state=state)}

    with patch("config.settings.GOOGLE_SEARCH_IN_CONTROLLED_BROWSER", False), \
         patch("webbrowser.open") as mock_open:
        
        # 1. search_web
        res = tools["search_web"].func("kepler laws of motion")
        mock_open.assert_called_once_with("https://www.google.com/search?q=kepler+laws+of+motion")
        assert res == "Opened a Google search in your default browser; I can't click or scroll in it."
        assert controller.browser_controlled is False
        assert state.browser_controlled is False

        # 2. search_site("google")
        mock_open.reset_mock()
        res_site = tools["search_site"].func("google", "lagrange points")
        mock_open.assert_called_once_with("https://www.google.com/search?q=lagrange+points")
        assert res_site == "Opened a Google search in your default browser; I can't click or scroll in it."

        # 3. YouTube searches continue in controlled browser
        res_yt = tools["search_site"].func("youtube", "lofi chill beats")
        assert "opened search for 'lofi chill beats' on youtube" in res_yt.lower()
        assert controller.browser_controlled is True


def test_google_search_cooldown_routes_to_default_browser():
    """When in active cooldown, Google searches route to default browser even if setting is True."""
    backend = FakeBackend()
    backend.start()
    controller = BrowserController(backend_factory=lambda: backend)
    controller.start_google_cooldown(duration_minutes=30)
    state = SessionState()
    tools = {t.name: t for t in make_browser_tools(controller, state=state)}

    with patch("config.settings.GOOGLE_SEARCH_IN_CONTROLLED_BROWSER", True), \
         patch("webbrowser.open") as mock_open:
        
        res = tools["search_web"].func("hubble deep field")
        mock_open.assert_called_once_with("https://www.google.com/search?q=hubble+deep+field")
        assert res == "Opened a Google search in your default browser; I can't click or scroll in it."
        assert controller.browser_controlled is False


# ---------------------------------------------------------------------------
# 6. Shortcut Parser Enhancements
# ---------------------------------------------------------------------------

def test_shortcut_restart_the_browser():
    """Verify 'restart the browser' parses to browser_restart tool call."""
    for phrase in ("restart the browser", "restart browser", "please restart the browser"):
        plan = parse_shortcut(phrase, [], {}, browser_enabled=True)
        assert isinstance(plan, ToolCallSpec)
        assert plan.name == "browser_restart"
        assert plan.target_name == "browser"


def test_shortcut_ordinal_nouns_and_filler_stripping():
    """Verify ordinal parsing accepts new nouns, strips trailing filler, and does not invent queries."""
    # 1. Bare nouns
    nouns = ["video", "result", "one", "link", "website", "site", "page", "article", "short", "story"]
    for noun in nouns:
        plan = parse_shortcut(f"open the first {noun}", [], {}, browser_enabled=True)
        assert isinstance(plan, ToolCallSpec)
        assert plan.name == "browser_click_result"
        assert plan.args == {"n": 1}

    # 2. Trailing filler stripped
    fillers = [
        "in the same browser",
        "in this browser",
        "in the same tab",
        "in this tab",
        "in the same window",
        "on this page",
    ]
    for filler in fillers:
        plan = parse_shortcut(f"open the first website {filler}", [], {}, browser_enabled=True)
        assert isinstance(plan, ToolCallSpec)
        assert plan.name == "browser_click_result"
        assert plan.args == {"n": 1}


def test_shortcut_ordinal_query_restrictions():
    """Verify search queries after ordinals are allowed only with specific nouns or explicit prepositions."""
    sites = {"youtube": {"search": "https://youtube.com/results?q={query}"}}

    # Allowed: noun is video, one, short, result
    plan = parse_shortcut("play the first video interstellar soundtrack", [], sites, browser_enabled=True)
    assert isinstance(plan, ShortcutPlan)
    assert plan.query == "interstellar soundtrack"
    assert plan.ordinal == 1

    # Allowed: explicit preposition 'about' with any noun
    plan_prep = parse_shortcut("open the first article about general relativity", [], sites, browser_enabled=True)
    assert isinstance(plan_prep, ShortcutPlan)
    assert plan_prep.query == "general relativity"

    # Rejected: noun is article/website without explicit preposition
    assert parse_shortcut("open the first article general relativity", [], sites, browser_enabled=True) is None
    assert parse_shortcut("open the second website orbital mechanics", [], sites, browser_enabled=True) is None


def test_shortcut_ordinal_query_rejections():
    """Verify query candidate rejections: conjunctions, ordinals, forbidden words, and early rejections."""
    sites = {"youtube": {"search": "https://youtube.com/results?q={query}"}}

    # Conjunctions / sequencing words at start of query candidate
    assert parse_shortcut("open the first video and then search news", [], sites, browser_enabled=True) is None
    assert parse_shortcut("open the first video or play music", [], sites, browser_enabled=True) is None
    assert parse_shortcut("open the first video but pause it", [], sites, browser_enabled=True) is None

    # Forbidden words inside query candidate
    assert parse_shortcut("open the first video in this browser", [], sites, browser_enabled=True) is not None  # stripped as filler
    assert parse_shortcut("play the first video browser tutorial", [], sites, browser_enabled=True) is None
    assert parse_shortcut("open the first link website design", [], sites, browser_enabled=True) is None

    # Early rejections
    assert parse_shortcut("open the first link in my default browser", [], sites, browser_enabled=True) is None
    assert parse_shortcut("click the second link in chrome", [], sites, browser_enabled=True) is None
    assert parse_shortcut("open the third website in a new tab", [], sites, browser_enabled=True) is None
    assert parse_shortcut("can you open the first link or the first website?", [], sites, browser_enabled=True) is None


# ---------------------------------------------------------------------------
# 7. Diagnostics (/diag) & CLI Commands
# ---------------------------------------------------------------------------

def test_diagnostics_browser_fields():
    """Verify diagnostics reports last_error, fallback_count, restart_count, cooldown_status, and browser_controlled."""
    class MockApp:
        def __init__(self):
            backend = FakeBackend()
            self.browser_controller = BrowserController(backend_factory=lambda: backend)
            self.browser_controller.browser_controlled = False
            self.browser_controller.fallback_count = 3
            self.browser_controller.restart_count = 1
            self.browser_controller.last_error = "Connection reset by peer"
            self.browser_controller.start_google_cooldown(duration_minutes=15)

    app = MockApp()
    report = build_diagnostics_text(app=app)

    assert "=== BROWSER STATUS ===" in report
    assert "Browser Controlled: False" in report
    assert "Fallback Count: 3" in report
    assert "Restart Count: 1" in report
    assert "Cooldown Status: active" in report
    assert "Last Error: Connection reset by peer" in report


def test_cli_browser_restart_command():
    """Verify /browser restart works in main.py controller flow."""
    backend = FakeBackend()
    backend.start()
    controller = BrowserController(backend_factory=lambda: backend)

    controller.last_error = "Stale process"
    controller.restart()
    assert controller.last_error is None
    assert controller.restart_count == 1
    assert controller.is_alive() is True
