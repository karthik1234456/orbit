"""Tests for routines/validate.py: allowed tools, limits, app/site allowlists, and state resolution."""

import pytest

from config.apps import ALLOWED_APPS
from config.sites import SITES
from routines.validate import (
    ALLOWED_TOOLS,
    MAX_ROUTINE_STEPS,
    RoutineValidationError,
    validate_routine_steps,
)


def test_allowed_tools_list():
    expected = {
        "open_application",
        "close_application",
        "open_website",
        "search_site",
        "open_workspace_folder",
        "browser_click_result",
        "browser_media",
        "browser_scroll",
        "browser_history",
        "browser_read_page",
        "browser_close",
        "wait",
    }
    assert ALLOWED_TOOLS == expected


def test_forbidden_tools_rejected():
    forbidden = [
        "create_file",
        "write_file",
        "read_file",
        "delete_file",
        "list_files",
        "move_file",
        "remember_fact",
        "search_memory",
        "forget_fact",
        "set_reminder",
        "list_reminders",
        "cancel_reminder",
        "set_setting",
        "bash",
        "cmd",
        "powershell",
        "unknown_custom_tool",
    ]

    for tool in forbidden:
        steps = [{"tool": tool, "args": {}}]
        with pytest.raises(RoutineValidationError, match="not allowed in routines"):
            validate_routine_steps(steps, allowed_apps=ALLOWED_APPS, sites=SITES)


def test_step_count_limit():
    # 10 steps allowed
    steps_10 = [{"tool": "wait", "args": {"seconds": 2}} for _ in range(MAX_ROUTINE_STEPS)]
    cleaned = validate_routine_steps(steps_10, allowed_apps=ALLOWED_APPS, sites=SITES)
    assert len(cleaned) == 10

    # 11 steps rejected
    steps_11 = [{"tool": "wait", "args": {"seconds": 1}} for _ in range(MAX_ROUTINE_STEPS + 1)]
    with pytest.raises(RoutineValidationError, match="at most 10 steps"):
        validate_routine_steps(steps_11, allowed_apps=ALLOWED_APPS, sites=SITES)

    # 0 steps rejected
    with pytest.raises(RoutineValidationError, match="at least 1 step"):
        validate_routine_steps([], allowed_apps=ALLOWED_APPS, sites=SITES)


def test_wait_budget_limit():
    # Sum of wait <= 60 seconds is valid
    steps = [
        {"tool": "wait", "args": {"seconds": 30}},
        {"tool": "wait", "args": {"seconds": 30}},
    ]
    cleaned = validate_routine_steps(steps, allowed_apps=ALLOWED_APPS, sites=SITES)
    assert len(cleaned) == 2

    # Sum of wait > 60 seconds rejected
    steps_exceeded = [
        {"tool": "wait", "args": {"seconds": 35}},
        {"tool": "wait", "args": {"seconds": 26}},
    ]
    with pytest.raises(RoutineValidationError, match="at most 60 seconds"):
        validate_routine_steps(steps_exceeded, allowed_apps=ALLOWED_APPS, sites=SITES)


def test_app_allowlist_enforcement():
    # Known apps allowed
    app_key = next(iter(ALLOWED_APPS.keys()))
    steps = [{"tool": "open_application", "args": {"name": app_key}}]
    cleaned = validate_routine_steps(steps, allowed_apps=ALLOWED_APPS, sites=SITES)
    assert cleaned[0]["args"]["name"] == app_key

    # Unknown app rejected
    steps_bad = [{"tool": "open_application", "args": {"name": "malicious_app_xyz"}}]
    with pytest.raises(RoutineValidationError, match="allowed list"):
        validate_routine_steps(steps_bad, allowed_apps=ALLOWED_APPS, sites=SITES)


def test_site_allowlist_and_alias_resolution():
    # Canonical site key allowed
    steps = [{"tool": "open_website", "args": {"site": "youtube"}}]
    cleaned = validate_routine_steps(steps, allowed_apps=ALLOWED_APPS, sites=SITES)
    assert cleaned[0]["args"]["site"] == "youtube"

    # Alias resolved to canonical key
    steps_alias = [{"tool": "open_website", "args": {"site": "yt"}}]
    cleaned_alias = validate_routine_steps(steps_alias, allowed_apps=ALLOWED_APPS, sites=SITES)
    assert cleaned_alias[0]["args"]["site"] == "youtube"

    # Unknown site rejected
    steps_bad = [{"tool": "open_website", "args": {"site": "unknown_site_xyz"}}]
    with pytest.raises(RoutineValidationError, match="allowed list|not recognized"):
        validate_routine_steps(steps_bad, allowed_apps=ALLOWED_APPS, sites=SITES)


def test_search_site_query_validation():
    # 1 to 8 words sanitized
    steps = [{"tool": "search_site", "args": {"site": "youtube", "query": "python programming tutorials"}}]
    cleaned = validate_routine_steps(steps, allowed_apps=ALLOWED_APPS, sites=SITES)
    assert cleaned[0]["args"]["query"] == "python programming tutorials"

    # Empty query rejected
    with pytest.raises(RoutineValidationError, match="1-8 words|cannot be empty"):
        validate_routine_steps([{"tool": "search_site", "args": {"site": "youtube", "query": ""}}], allowed_apps=ALLOWED_APPS, sites=SITES)

    # > 8 words rejected
    long_query = "word " * 9
    with pytest.raises(RoutineValidationError, match="1-8 words|1 to 8 words"):
        validate_routine_steps([{"tool": "search_site", "args": {"site": "youtube", "query": long_query}}], allowed_apps=ALLOWED_APPS, sites=SITES)


def test_state_dependent_close_it_resolution():
    # Sequence: open notepad then close it -> resolves close_application(name="notepad")
    steps = [
        {"tool": "open_application", "args": {"name": "notepad"}},
        {"tool": "close_application", "args": {"name": "it"}},
    ]
    cleaned = validate_routine_steps(steps, allowed_apps=ALLOWED_APPS, sites=SITES)
    assert cleaned[1]["args"]["name"] == "notepad"

    # Close it without preceding app in simulated state -> refused with "name the app explicitly"
    steps_unresolved = [
        {"tool": "close_application", "args": {"name": "it"}},
    ]
    with pytest.raises(RoutineValidationError, match="name the app explicitly"):
        validate_routine_steps(steps_unresolved, allowed_apps=ALLOWED_APPS, sites=SITES)


def test_stored_step_structure_clean():
    # Extra unexpected fields like "raw_text", "url", etc. are stripped
    dirty_step = {
        "tool": "open_application",
        "args": {"name": "notepad", "raw_text": "open notepad", "extra": 123},
        "raw": "open notepad",
        "status": "pending",
    }
    cleaned = validate_routine_steps([dirty_step], allowed_apps=ALLOWED_APPS, sites=SITES)
    assert set(cleaned[0].keys()) == {"tool", "args"}
    assert set(cleaned[0]["args"].keys()) == {"name"}

    # URLs directly in args are stripped/rejected
    with pytest.raises(RoutineValidationError, match="URLs are not allowed in routine arguments"):
        validate_routine_steps([{"tool": "open_application", "args": {"name": "notepad", "url": "http://evil.com"}}], allowed_apps=ALLOWED_APPS, sites=SITES)
