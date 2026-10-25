"""Tests for command chaining, PlanStep, describe_step, and parse_chain."""

import pytest

from config import settings
from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.chains import PlanStep, describe_step, parse_chain
from core.session_state import SessionState
from core.shortcuts import ShortcutPlan, parse_shortcut


APPS = ALLOWED_APPS
SITES_MAP = SITES


def test_describe_step():
    """Verify describe_step formats all required labels properly."""
    assert describe_step("open_website", {"url": "https://www.youtube.com"}) == "Open youtube"
    assert describe_step("search_site", {"site": "youtube", "query": "lofi"}) == "Search YouTube for 'lofi'"
    assert describe_step("browser_click_result", {"n": 3}) == "Click result 3"
    assert describe_step("browser_media", {"action": "pause"}) == "Pause"
    assert describe_step("browser_scroll", {"direction": "down"}) == "Scroll down"
    assert describe_step("wait", {"seconds": 5}) == "Wait 5 seconds"
    assert describe_step("wait", {"seconds": 1}) == "Wait 1 second"
    assert describe_step("close_application", {"name": "notepad"}) == "Close notepad"
    assert describe_step("open_application", {"name": "notepad"}) == "Open notepad"
    assert describe_step("open_website", {"url": "https://www.youtube.com/shorts"}) == "Open shorts"


def test_plan_step():
    """Verify PlanStep initialization and backwards-compatible name property."""
    step = PlanStep(tool="wait", args={"seconds": 5})
    assert step.tool == "wait"
    assert step.name == "wait"
    assert step.args == {"seconds": 5}
    assert step.label == "Wait 5 seconds"

    step2 = PlanStep(tool="open_application", args={"name": "notepad"}, label="Open notepad")
    assert step2.label == "Open notepad"


def test_builtin_shortcut_plan_extension():
    """Verify ShortcutPlan defaults to source='builtin' and populates labels via describe_step."""
    spec = parse_shortcut("open youtube and play lo-fi hip hop", APPS, SITES_MAP, browser_enabled=True)
    assert isinstance(spec, ShortcutPlan)
    assert spec.source == "builtin"
    assert isinstance(spec.labels, list)
    assert len(spec.labels) == len(spec.steps)
    assert spec.labels[0] == "Search YouTube for 'lo-fi hip hop'"
    assert spec.labels[1] == "Click result 1"
    assert spec.labels[2] == "Play"


def test_chain_youtube_lofi_play_pause():
    """Verify multi-action browser chain with 'then' separators."""
    text = "open youtube then search for lofi then play the third one then pause"
    plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 4
    assert plan.steps[0].tool == "open_website"
    assert plan.steps[1].tool == "search_site"
    assert plan.steps[1].args == {"site": "youtube", "query": "lofi"}
    assert plan.steps[2].tool == "browser_click_result"
    assert plan.steps[2].args == {"n": 3}
    assert plan.steps[3].tool == "browser_media"
    assert plan.steps[3].args == {"action": "pause"}
    assert plan.labels == ["Open youtube", "Search YouTube for 'lofi'", "Click result 3", "Pause"]


def test_chain_comma_separated():
    """Verify comma-separated browser action chain."""
    text = "search youtube for lofi, play the second one, mute"
    plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 3
    assert plan.steps[0].tool == "search_site"
    assert plan.steps[0].args == {"site": "youtube", "query": "lofi"}
    assert plan.steps[1].tool == "browser_click_result"
    assert plan.steps[1].args == {"n": 2}
    assert plan.steps[2].tool == "browser_media"
    assert plan.steps[2].args == {"action": "mute"}
    assert plan.labels == ["Search YouTube for 'lofi'", "Click result 2", "Mute"]


def test_chain_open_notepad_and_open_calculator():
    """Verify desktop app chain joined with 'and' before command verb."""
    text = "open notepad and open calculator"
    plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=False)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 2
    assert plan.steps[0].tool == "open_application"
    assert plan.steps[0].args == {"name": "notepad"}
    assert plan.steps[1].tool == "open_application"
    assert plan.steps[1].args == {"name": "calculator"}
    assert plan.labels == ["Open notepad", "Open calculator"]


def test_chain_open_notepad_then_close_it_simulated_state():
    """Verify 'open notepad then close it' uses simulated state so 'close it' resolves."""
    text = "open notepad then close it"
    state = SessionState()
    plan = parse_chain(text, APPS, SITES_MAP, state=state, browser_enabled=False)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 2
    assert plan.steps[0].tool == "open_application"
    assert plan.steps[0].args == {"name": "notepad"}
    assert plan.steps[1].tool == "close_application"
    assert plan.steps[1].args == {"name": "notepad"}
    assert plan.labels == ["Open notepad", "Close notepad"]
    # Verify caller's original state was not mutated
    assert state.last_app is None


def test_chain_tom_and_jerry_preserves_query():
    """Verify 'and' not before a command verb is preserved within the search query."""
    text = "search youtube for tom and jerry then play the first one"
    plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 2
    assert plan.steps[0].tool == "search_site"
    assert plan.steps[0].args == {"site": "youtube", "query": "tom and jerry"}
    assert plan.steps[1].tool == "browser_click_result"
    assert plan.steps[1].args == {"n": 1}
    assert plan.labels == ["Search YouTube for 'tom and jerry'", "Click result 1"]


def test_chain_bread_and_open_source():
    """Verify candidate boundary before 'and open' is discarded because 'open source' fails, keeping full query."""
    text = "search youtube for bread and open source then play the first one"
    plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 2
    assert plan.steps[0].tool == "search_site"
    assert plan.steps[0].args == {"site": "youtube", "query": "bread and open source"}
    assert plan.steps[1].tool == "browser_click_result"
    assert plan.steps[1].args == {"n": 1}


def test_single_command_bread_and_open_source_returns_none():
    """Verify single commands return None from parse_chain (parse_shortcut's job)."""
    text = "search youtube for bread and open source"
    assert parse_chain(text, APPS, SITES_MAP, browser_enabled=True) is None
    # Verify parse_shortcut handles it
    assert parse_shortcut(text, APPS, SITES_MAP, browser_enabled=True) is not None


def test_open_youtube_and_play_free_fire_videos_returns_none():
    """Verify 'open youtube and play free fire videos' returns None from parse_chain (handled by parse_shortcut)."""
    text = "open youtube and play free fire videos"
    assert parse_chain(text, APPS, SITES_MAP, browser_enabled=True) is None
    # Handled by parse_shortcut as a single built-in ShortcutPlan
    builtin = parse_shortcut(text, APPS, SITES_MAP, browser_enabled=True)
    assert isinstance(builtin, ShortcutPlan)


def test_chain_scroll_down_twice_then_pause():
    """Verify multiple scroll actions followed by pause."""
    text = "scroll down then scroll down then pause"
    plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 3
    assert plan.steps[0].tool == "browser_scroll"
    assert plan.steps[1].tool == "browser_scroll"
    assert plan.steps[2].tool == "browser_media"
    assert plan.labels == ["Scroll down", "Scroll down", "Pause"]


def test_chain_wait_step():
    """Verify chain-only 'wait <N> seconds' step."""
    text = "play the first video, wait 5 seconds, then pause"
    plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
    assert plan is not None
    assert plan.source == "chain"
    assert len(plan.steps) == 3
    assert plan.steps[0].tool == "browser_click_result"
    assert plan.steps[0].args == {"n": 1}
    assert plan.steps[1].tool == "wait"
    assert plan.steps[1].args == {"seconds": 5}
    assert plan.steps[2].tool == "browser_media"
    assert plan.steps[2].args == {"action": "pause"}
    assert plan.labels == ["Click result 1", "Wait 5 seconds", "Pause"]


def test_chain_negatives():
    """Verify negative and malformed chain cases return None."""
    # Unknown command / invalid segment
    assert parse_chain("open youtube and delete my files", APPS, SITES_MAP, browser_enabled=True) is None

    # Disallowed shell characters
    assert parse_chain("open youtube then; calc", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open youtube then & calc", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open youtube then | calc", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open youtube then `calc`", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open youtube then $calc", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open youtube then > calc", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open youtube then < calc", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open youtube then \\ calc", APPS, SITES_MAP, browser_enabled=True) is None

    # URLs
    assert parse_chain("open youtube then open http://x.com", APPS, SITES_MAP, browser_enabled=True) is None

    # Trailing separator producing empty segment
    assert parse_chain("open youtube then", APPS, SITES_MAP, browser_enabled=True) is None

    # 7-segment chain (exceeds max 6 segments)
    seven_seg = "open notepad then open calculator then wait 1s then wait 2s then wait 3s then wait 4s then wait 5s"
    assert parse_chain(seven_seg, APPS, SITES_MAP, browser_enabled=True) is None

    # Wait step out of range (1-30)
    assert parse_chain("open notepad then wait 31 seconds", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open notepad then wait 0 seconds", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open notepad then wait", APPS, SITES_MAP, browser_enabled=True) is None

    # Two waits totalling over 60 seconds
    assert parse_chain("wait 30 seconds then wait 30 seconds then wait 5 seconds", APPS, SITES_MAP, browser_enabled=True) is None

    # Text with newlines
    assert parse_chain("open notepad\nthen open calculator", APPS, SITES_MAP, browser_enabled=True) is None
    assert parse_chain("open notepad\rthen open calculator", APPS, SITES_MAP, browser_enabled=True) is None

    # Segment with unknown app
    assert parse_chain("open youtube then open regedit", APPS, SITES_MAP, browser_enabled=True) is None

    # Browser disabled with browser-only steps
    assert parse_chain("open youtube then pause", APPS, SITES_MAP, browser_enabled=False) is None
    assert parse_chain("search youtube for lofi then play the third one", APPS, SITES_MAP, browser_enabled=False) is None

    # Text longer than 300 characters
    long_text = "open notepad then " + "wait 1 second then " * 30 + "close it"
    assert len(long_text) > 300
    assert parse_chain(long_text, APPS, SITES_MAP, browser_enabled=True) is None


def test_chain_enabled_setting(monkeypatch: pytest.MonkeyPatch):
    """Verify CHAINS_ENABLED=0 disables chain parsing."""
    text = "open notepad and open calculator"
    assert parse_chain(text, APPS, SITES_MAP, browser_enabled=False) is not None

    monkeypatch.setenv("CHAINS_ENABLED", "0")
    assert parse_chain(text, APPS, SITES_MAP, browser_enabled=False) is None

    monkeypatch.setenv("CHAINS_ENABLED", "false")
    assert parse_chain(text, APPS, SITES_MAP, browser_enabled=False) is None

    monkeypatch.delenv("CHAINS_ENABLED", raising=False)
    monkeypatch.setattr(settings, "CHAINS_ENABLED", False)
    assert parse_chain(text, APPS, SITES_MAP, browser_enabled=False) is None


def test_chain_determinism_20_runs():
    """Verify parse_chain is completely deterministic across 20 consecutive runs."""
    text = "open youtube then search for lofi then play the third one then pause"
    ref_plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
    assert ref_plan is not None

    for _ in range(20):
        plan = parse_chain(text, APPS, SITES_MAP, browser_enabled=True)
        assert plan is not None
        assert plan.source == ref_plan.source
        assert plan.labels == ref_plan.labels
        assert len(plan.steps) == len(ref_plan.steps)
        for s1, s2 in zip(plan.steps, ref_plan.steps):
            assert s1.tool == s2.tool
            assert s1.args == s2.args
            assert s1.label == s2.label
