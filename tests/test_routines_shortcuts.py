"""Tests for routine shortcut patterns, near-misses, and precedence in core/shortcuts.py."""

from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.shortcuts import parse_routine_shortcut


def test_create_routine_shortcuts():
    phrases = [
        "create a routine morning: open notepad then open chrome",
        "make a routine called dev that open vscode then wait 5 seconds",
        "save a routine named study as search youtube for python",
        "please create a routine work: open notepad",
        "hey can you make a routine focus to open calculator",
        "could you save routine testing: open notepad then close it",
    ]

    for p in phrases:
        act = parse_routine_shortcut(p, allowed_apps=ALLOWED_APPS, sites=SITES)
        assert act is not None, f"Failed to parse creation phrase: {p}"
        assert act.action == "create"
        assert act.name
        assert act.display_name
        assert act.steps and len(act.steps) >= 1


def test_save_last_plan_shortcuts():
    phrases = [
        "save that as a routine morning",
        "save this as routine called work",
        "save the last plan as a routine named dev",
        "please save that as routine study",
        "can you save this as a routine test",
    ]

    for p in phrases:
        act = parse_routine_shortcut(p, allowed_apps=ALLOWED_APPS, sites=SITES)
        assert act is not None, f"Failed to parse save last plan phrase: {p}"
        assert act.action == "save_last_plan"
        assert act.name
        assert act.display_name


def test_run_routine_shortcuts():
    phrases = [
        ("run routine morning", "morning"),
        ("start my routine work", "work"),
        ("launch routine dev", "dev"),
        ("do routine study", "study"),
        ("routine morning", "morning"),
        ("run morning routine", "morning"),
        ("please run my routine focus", "focus"),
        ("can u start routine daily", "daily"),
    ]

    for p, expected_name in phrases:
        act = parse_routine_shortcut(p, allowed_apps=ALLOWED_APPS, sites=SITES)
        assert act is not None, f"Failed to parse run phrase: {p}"
        assert act.action == "run"
        assert act.name == expected_name


def test_list_and_show_shortcuts():
    for p in ["list routines", "show routines", "what routines do i have", "please show routines"]:
        act = parse_routine_shortcut(p, allowed_apps=ALLOWED_APPS, sites=SITES)
        assert act is not None
        assert act.action == "list"

    for p in ["show routine morning", "show routine work", "can you show routine dev"]:
        act = parse_routine_shortcut(p, allowed_apps=ALLOWED_APPS, sites=SITES)
        assert act is not None
        assert act.action == "show"
        assert act.name in ("morning", "work", "dev")


def test_delete_routine_shortcuts():
    for p in ["delete routine morning", "remove routine work", "please delete routine dev"]:
        act = parse_routine_shortcut(p, allowed_apps=ALLOWED_APPS, sites=SITES)
        assert act is not None
        assert act.action == "delete"
        assert act.name in ("morning", "work", "dev")


def test_near_misses_return_none():
    near_misses = [
        "create a routine",                     # Missing name and chain
        "run routine",                          # Missing name
        "routine",                              # Bare word
        "i have a routine at the gym",          # Conversational mention
        "run routine study and delete my files",# Compound command chain in name
        "routine " + ("a" * 31),                # Name > 30 characters
        "run routine http://google.com",        # URL in name
        "run routine test; rm -rf",             # Shell separator in name
        "create routine test: open notepad; rm",# Shell separator in chain
        "create a routine open: open notepad",  # Name starts with command verb
        "create a routine list: open notepad",  # Name starts with reserved word
    ]

    for p in near_misses:
        act = parse_routine_shortcut(p, allowed_apps=ALLOWED_APPS, sites=SITES)
        assert act is None, f"Near miss unexpectedly matched: '{p}' -> {act}"
