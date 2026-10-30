"""Tests for messaging shortcuts, chains restrictions, near misses, and routine validation."""

from __future__ import annotations

import pytest

from core.chains import parse_chain
from core.shortcuts import (
    ShortcutReply,
    ToolCallSpec,
    parse_messaging_shortcut,
    parse_shortcut,
)
from routines.validate import RoutineValidationError, validate_routine_steps


CONTACTS = ["Mom", "Bob", "Alice Smith", "Bob Smith"]


# --- 1. Every Shortcut Pattern ---


def test_shortcut_pattern_a():
    """Verify Pattern A variants for sending whatsapp messages."""
    # text <name> saying <body>
    spec1 = parse_shortcut("text Mom saying I am on the way", contact_names=CONTACTS)
    assert isinstance(spec1, ToolCallSpec)
    assert spec1.name == "send_whatsapp"
    assert spec1.args == {"contact": "Mom", "text": "I am on the way", "attach_link": False}

    # message <name> that <body>
    spec2 = parse_shortcut("message Bob that we have arrived safely", contact_names=CONTACTS)
    assert isinstance(spec2, ToolCallSpec)
    assert spec2.name == "send_whatsapp"
    assert spec2.args == {"contact": "Bob", "text": "we have arrived safely", "attach_link": False}

    # whatsapp <name> to say <body>
    spec3 = parse_shortcut("whatsapp Alice Smith to say see you tomorrow", contact_names=CONTACTS)
    assert isinstance(spec3, ToolCallSpec)
    assert spec3.name == "send_whatsapp"
    assert spec3.args == {"contact": "Alice Smith", "text": "see you tomorrow", "attach_link": False}

    # send a whatsapp message to <name> on whatsapp saying <body>
    spec4 = parse_shortcut("send a whatsapp message to Mom on whatsapp saying dinner is ready", contact_names=CONTACTS)
    assert isinstance(spec4, ToolCallSpec)
    assert spec4.name == "send_whatsapp"
    assert spec4.args == {"contact": "Mom", "text": "dinner is ready", "attach_link": False}


def test_shortcut_pattern_b():
    """Verify Pattern B variants for opening a chat."""
    spec1 = parse_shortcut("open chat with Mom", contact_names=CONTACTS)
    assert isinstance(spec1, ToolCallSpec)
    assert spec1.name == "send_whatsapp"
    assert spec1.args == {"contact": "Mom", "text": "", "attach_link": False}

    spec2 = parse_shortcut("start a whatsapp chat with Bob", contact_names=CONTACTS)
    assert isinstance(spec2, ToolCallSpec)
    assert spec2.name == "send_whatsapp"
    assert spec2.args == {"contact": "Bob", "text": "", "attach_link": False}


def test_shortcut_pattern_c():
    """Verify Pattern C variants for sending link / page / video."""
    # send this link to <name>
    spec1 = parse_shortcut("send this link to Mom", contact_names=CONTACTS)
    assert isinstance(spec1, ToolCallSpec)
    assert spec1.name == "send_whatsapp"
    assert spec1.args == {"contact": "Mom", "text": "", "attach_link": True}

    # send the video to <name> saying <body>
    spec2 = parse_shortcut("send the video to Bob saying check this out", contact_names=CONTACTS)
    assert isinstance(spec2, ToolCallSpec)
    assert spec2.name == "send_whatsapp"
    assert spec2.args == {"contact": "Bob", "text": "check this out", "attach_link": True}

    # share this article with <name> on whatsapp
    spec3 = parse_shortcut("share this article with Alice Smith on whatsapp", contact_names=CONTACTS)
    assert isinstance(spec3, ToolCallSpec)
    assert spec3.name == "send_whatsapp"
    assert spec3.args == {"contact": "Alice Smith", "text": "", "attach_link": True}

    # message <name> this link
    spec4 = parse_shortcut("message Mom this link", contact_names=CONTACTS)
    assert isinstance(spec4, ToolCallSpec)
    assert spec4.name == "send_whatsapp"
    assert spec4.args == {"contact": "Mom", "text": "", "attach_link": True}


def test_shortcut_pattern_d():
    """Verify Pattern D variants for email drafts."""
    # email <name> saying <body>
    spec1 = parse_shortcut("email Mom saying project status report", contact_names=CONTACTS)
    assert isinstance(spec1, ToolCallSpec)
    assert spec1.name == "open_email_draft"
    assert spec1.args == {
        "contact": "Mom",
        "subject": "project status report",
        "body": "project status report",
        "attach_link": False,
    }

    # mail <name> that <body>
    spec2 = parse_shortcut("mail Bob that the documents are ready", contact_names=CONTACTS)
    assert isinstance(spec2, ToolCallSpec)
    assert spec2.name == "open_email_draft"
    assert spec2.args["contact"] == "Bob"
    assert spec2.args["attach_link"] is False

    # email this link to <name>
    spec3 = parse_shortcut("email this link to Mom", contact_names=CONTACTS)
    assert isinstance(spec3, ToolCallSpec)
    assert spec3.name == "open_email_draft"
    assert spec3.args == {"contact": "Mom", "subject": "", "body": "", "attach_link": True}


# --- 2. Bare-Name and Unknown Contact Replies ---


def test_bare_name_and_unknown_contact_replies():
    """Verify fixed plain-text replies for bare name queries and unknown contacts."""
    # Bare name for known contact
    res1 = parse_shortcut("text Mom", contact_names=CONTACTS)
    assert isinstance(res1, ShortcutReply)
    assert res1.reply == "What should I say to Mom? Try: text Mom saying ..."

    res2 = parse_shortcut("message Bob", contact_names=CONTACTS)
    assert isinstance(res2, ShortcutReply)
    assert res2.reply == "What should I say to Bob? Try: text Bob saying ..."

    # Unknown contact
    res3 = parse_shortcut("text Charlie", contact_names=CONTACTS)
    assert isinstance(res3, ShortcutReply)
    assert res3.reply == "I don't have a contact called Charlie. Add it in Contacts."

    res4 = parse_shortcut("message Unknown Person", contact_names=CONTACTS)
    assert isinstance(res4, ShortcutReply)
    assert res4.reply == "I don't have a contact called Unknown Person. Add it in Contacts."


def test_longest_exact_contact_matching():
    """Verify matching prefers the longest exact name (e.g. 'Bob Smith' over 'Bob')."""
    spec = parse_shortcut("text Bob Smith saying hello", contact_names=CONTACTS)
    assert isinstance(spec, ToolCallSpec)
    assert spec.args["contact"] == "Bob Smith"


# --- 3. Near-Misses (Must return None) ---


@pytest.mark.parametrize(
    "near_miss",
    [
        "text me when you arrive",
        "message me later",
        "send a message",
        "tell mom that",
        "whatsapp",
        "open whatsapp",
        "send this link",
        "text mom saying delete my files; calc",
        "email",
    ],
)
def test_near_misses_return_none(near_miss: str):
    """Verify all prompt-specified near misses return None from messaging parser."""
    res_msg = parse_messaging_shortcut(near_miss, contact_names=CONTACTS)
    assert res_msg is None, f"Expected None from parse_messaging_shortcut for '{near_miss}', got {res_msg}"
    res_all = parse_shortcut(near_miss, contact_names=CONTACTS)
    if res_all is not None:
        # If parse_shortcut matched something (e.g. 'open whatsapp' as open_application),
        # ensure it is never a messaging tool call.
        assert getattr(res_all, "name", None) not in {"send_whatsapp", "open_email_draft"}


# --- 4. Chaining Restrictions ---


def test_chains_with_messaging_last_step_only():
    """Verify messaging is permitted strictly as the final step of a multi-step chain."""
    # Allowed: browse then send this link to Mom
    plan = parse_chain(
        "open youtube and search for lofi then send this link to Mom",
        contact_names=CONTACTS,
        browser_enabled=True,
    )
    assert plan is not None
    assert len(plan.steps) == 3
    assert plan.steps[-1].tool == "send_whatsapp"
    assert plan.steps[-1].args["attach_link"] is True

    # Allowed: open chat with Mom at end of chain
    plan2 = parse_chain(
        "open calculator and wait 2 seconds then open chat with Mom",
        contact_names=CONTACTS,
    )
    assert plan2 is not None
    assert plan2.steps[-1].tool == "send_whatsapp"
    assert plan2.steps[-1].args["attach_link"] is False
    assert plan2.steps[-1].args["text"] == ""

    # Forbidden: free-body patterns (a, d) in chains return None
    plan_body = parse_chain(
        "open youtube then text Mom saying hello",
        contact_names=CONTACTS,
        browser_enabled=True,
    )
    assert plan_body is None

    # Forbidden: messaging in the middle of a chain returns None
    plan_mid = parse_chain(
        "send this link to Mom then open youtube",
        contact_names=CONTACTS,
        browser_enabled=True,
    )
    assert plan_mid is None


# --- 5. Routine Validation Refuses Messaging Tools ---


def test_routines_refuse_messaging_tools():
    """Verify that routines validate refuses send_whatsapp and open_email_draft."""
    steps = [
        {"tool": "open_application", "args": {"name": "calculator"}},
        {"tool": "send_whatsapp", "args": {"contact": "Mom", "text": "hello"}},
    ]
    with pytest.raises(RoutineValidationError, match=r"not allowed in routines"):
        validate_routine_steps(steps)

    email_steps = [
        {"tool": "open_email_draft", "args": {"contact": "Mom", "subject": "hi"}},
    ]
    with pytest.raises(RoutineValidationError, match=r"not allowed in routines"):
        validate_routine_steps(email_steps)
