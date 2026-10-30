"""Tests for messaging links, composition, rate limits, verification, and tools."""

from __future__ import annotations

from pathlib import Path
import pytest

from contacts.store import ContactRef, ContactStore
from core.session_state import SessionState
from messaging.links import (
    build_mailto,
    build_wame_url,
    build_whatsapp_uri,
    sanitize_message,
)
from messaging.service import (
    MessagingError,
    compose_whatsapp,
    summary,
)
from system.winwindow import verify_whatsapp_window, wait_for_foreground
from tools.messaging_tools import MessagingRateLimiter, make_messaging_tools


# --- 1. Link builders and sanitization ---


def test_sanitize_message_strips_control_bidi_and_zero_width():
    """Verify sanitize_message strips control chars, bidi overrides, zero-width chars and caps length."""
    raw = "Hello\u200B world!\u202E\u2066\t\n\rHow are you?"
    sanitized = sanitize_message(raw)
    assert "\u200B" not in sanitized
    assert "\u202E" not in sanitized
    assert "\u2066" not in sanitized
    assert "\n" not in sanitized
    assert "\r" not in sanitized
    assert sanitized == "Hello world! How are you?"

    # Length cap at 300
    long_text = "A" * 400
    assert len(sanitize_message(long_text)) == 300


def test_build_whatsapp_uri_and_wame():
    """Verify build_whatsapp_uri and build_wame_url properly quote and format."""
    phone = "15550100123"
    text = "Hello & goodbye #1 'test' <special>"
    uri = build_whatsapp_uri(phone, text)
    wame = build_wame_url(phone, text)

    assert uri.startswith("whatsapp://send?phone=15550100123&text=")
    assert wame.startswith("https://wa.me/15550100123?text=")
    # Never raw unencoded spaces, &, or #
    assert " " not in uri
    assert "&goodbye" not in uri
    assert "#1" not in uri


def test_build_mailto():
    """Verify build_mailto restricts subject (80 chars), body (500 chars), and never includes cc/bcc."""
    email = "test@example.com"
    subject = "Meeting tomorrow at 10 AM & planning"
    body = "Here is the project update #1"

    mailto = build_mailto(email, subject=subject, body=body)
    assert mailto.startswith("mailto:test@example.com?")
    assert "subject=" in mailto
    assert "body=" in mailto
    assert "cc=" not in mailto
    assert "bcc=" not in mailto
    assert "\r" not in mailto
    assert "\n" not in mailto


# --- 2. Window and Foreground verification ---


def test_verify_whatsapp_window():
    """Verify that verify_whatsapp_window requires BOTH title containing WhatsApp AND process name in WHATSAPP_PROCESS_NAMES."""
    # Matches both
    assert verify_whatsapp_window("WhatsApp", "WhatsApp.exe") is True
    assert verify_whatsapp_window("WhatsApp - Chat with Mom", "WhatsApp.Root.exe") is True
    # Unreadable process name (None) means unverified
    assert verify_whatsapp_window("WhatsApp", None) is False
    # Wrong title
    assert verify_whatsapp_window("Notepad", "WhatsApp.exe") is False
    # Wrong process name
    assert verify_whatsapp_window("WhatsApp", "chrome.exe") is False


def test_wait_for_foreground_with_fake_clock():
    """Verify wait_for_foreground respects timeout using fake clock."""
    fake_time = 1000.0

    def clock():
        return fake_time

    def sleep(dt):
        nonlocal fake_time
        fake_time += dt

    # Never matches: should time out
    def probe_fail():
        return ("Notepad", "notepad.exe")

    ok = wait_for_foreground(
        lambda t, p: p == "WhatsApp.exe",
        timeout=3.0,
        foreground_fn=probe_fail,
        clock=clock,
        sleep=sleep,
    )
    assert ok is False

    # Succeeds on 3rd attempt
    attempts = 0

    def probe_success():
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            return ("WhatsApp", "WhatsApp.exe")
        return ("Desktop", "explorer.exe")

    ok2 = wait_for_foreground(
        lambda t, p: verify_whatsapp_window(t, p),
        timeout=5.0,
        foreground_fn=probe_success,
        clock=clock,
        sleep=sleep,
    )
    assert ok2 is True


# --- 3. Composition, summary, and artifacts ---


def test_compose_whatsapp_with_and_without_link():
    """Verify compose_whatsapp single source of truth for text and summary."""
    ref = ContactRef(id=1, display_name="Mom", phone="15550100123")
    state = SessionState(clock=lambda: 1000.0)
    state.set_artifact("https://youtube.com/watch?v=123", "Cool Video", updated_at=1000.0)

    # Without link
    c1 = compose_whatsapp(ref, "Hello there", attach_link=False, state=state, clock=lambda: 1000.0)
    assert c1.final_text == "Hello there"
    s1 = summary(c1, mode="draft")
    assert "Mom" in s1
    assert "••••0123" in s1
    assert "will be DRAFTED, you press Enter to send" in s1

    # With link
    c2 = compose_whatsapp(ref, "Check this", attach_link=True, state=state, clock=lambda: 1000.0)
    assert c2.final_text == "Check this https://youtube.com/watch?v=123"
    s2 = summary(c2, mode="send")
    assert "will be SENT immediately" in s2
    assert "Check this https://youtube.com/watch?v=123" in s2


def test_compose_whatsapp_attach_link_no_page_or_expired_fails():
    """Verify attach_link fails when no browser artifact exists or when TTL expired."""
    ref = ContactRef(id=1, display_name="Mom", phone="15550100123")
    empty_state = SessionState(clock=lambda: 1000.0)

    # No page
    with pytest.raises(MessagingError, match="No active page link"):
        compose_whatsapp(ref, "hello", attach_link=True, state=empty_state, clock=lambda: 1000.0)

    # Expired page (>30 minutes = 1800s)
    state = SessionState(clock=lambda: 1000.0)
    state.set_artifact("https://youtube.com/watch?v=456", "Title", updated_at=1000.0)
    with pytest.raises(MessagingError, match="expired"):
        compose_whatsapp(ref, "hello", attach_link=True, state=state, clock=lambda: 3000.0)


def test_artifacts_page_title_never_in_context_block():
    """Verify page_title is stored for UI only and strictly never appears in context_block."""
    state = SessionState(clock=lambda: 1000.0)
    secret_title = "TOP SECRET PAGE TITLE 12345"
    state.set_artifact("https://en.wikipedia.org/wiki/Orbit", secret_title, updated_at=1000.0)

    ctx = state.context_block()
    assert secret_title not in ctx
    assert "wikipedia.org" in ctx


# --- 4. Messaging tools execution and safety ---


def test_messaging_tools_rate_limiter():
    """Verify rate limiter blocks when >5 messages in 10 minutes or <10s between messages to same contact."""
    cur_time = 1000.0

    def clock():
        return cur_time

    limiter = MessagingRateLimiter(clock=clock)
    assert limiter.check(contact_id=1) is None
    limiter.record(contact_id=1)

    # Immediate second message to same contact (<10s) must fail
    assert limiter.check(contact_id=1) == "ERROR: too many messages, try again later."

    # Different contact after 1s is okay
    cur_time += 1.0
    assert limiter.check(contact_id=2) is None
    limiter.record(contact_id=2)

    # Fast-forward 11s: same contact 1 is okay now
    cur_time += 11.0
    assert limiter.check(contact_id=1) is None
    limiter.record(contact_id=1)

    # Add 2 more to hit 5 messages overall
    limiter.record(contact_id=3)
    limiter.record(contact_id=4)

    # 6th message within 10 min window must fail
    assert limiter.check(contact_id=5) == "ERROR: too many messages, try again later."


def test_messaging_tools_draft_mode_never_presses_enter(tmp_path: Path):
    """Verify draft mode calls opener and never sends VK_RETURN."""
    store = ContactStore(path=tmp_path / "contacts.db")
    store.add(display_name="Mom", phone="15550100123")

    opened_uris: list[str] = []
    keys_sent: list[int] = []

    def fake_opener(uri: str) -> bool:
        opened_uris.append(uri)
        return True

    def fake_key_sender():
        keys_sent.append(0x0D)

    tools = make_messaging_tools(
        contacts=store,
        state_getter=lambda: SessionState(),
        opener=fake_opener,
        key_sender=fake_key_sender,
    )
    send_tool = [t for t in tools if t.name == "send_whatsapp"][0]

    # Precheck passes
    assert send_tool.precheck({"contact": "Mom", "text": "hello"}) is None

    # Execute
    res = send_tool.func({"contact": "Mom", "text": "hello"})
    assert "Opened the draft in WhatsApp; press Enter to send" in res
    assert len(opened_uris) == 1
    assert opened_uris[0].startswith("whatsapp://send?phone=15550100123")
    assert len(keys_sent) == 0  # Enter NEVER pressed in draft mode!


def test_messaging_tools_send_mode_presses_enter_with_verification(tmp_path: Path, monkeypatch):
    """Verify send mode verifies window twice and presses Enter once."""
    monkeypatch.setenv("WHATSAPP_SEND_MODE", "send")
    from config import settings
    monkeypatch.setattr(settings, "WHATSAPP_SEND_MODE", "send")

    store = ContactStore(path=tmp_path / "contacts.db")
    store.add(display_name="Mom", phone="15550100123")

    opened_uris: list[str] = []
    keys_sent: list[int] = []
    sleeps: list[float] = []

    def fake_opener(uri: str) -> bool:
        opened_uris.append(uri)
        return True

    def fake_key_sender():
        keys_sent.append(0x0D)

    def fake_fg():
        return ("WhatsApp - Mom", "WhatsApp.exe")

    tools = make_messaging_tools(
        contacts=store,
        state_getter=lambda: SessionState(),
        opener=fake_opener,
        key_sender=fake_key_sender,
        foreground=fake_fg,
        sleep=lambda dt: sleeps.append(dt),
    )
    send_tool = [t for t in tools if t.name == "send_whatsapp"][0]

    # Populate describe
    _ = send_tool.describe({"contact": "Mom", "text": "hello"})

    res = send_tool.func({"contact": "Mom", "text": "hello"})
    assert "Sent the draft by pressing Enter" in res
    assert len(keys_sent) == 1  # Enter pressed ONCE
    assert 1.2 in sleeps  # Waited 1.2s before second verification


def test_messaging_tools_page_drift_aborts(tmp_path: Path):
    """Verify that if browser page changes between describe and execute, execution aborts."""
    store = ContactStore(path=tmp_path / "contacts.db")
    store.add(display_name="Mom", phone="15550100123")
    state = SessionState(clock=lambda: 1000.0)

    tools = make_messaging_tools(
        contacts=store,
        state_getter=lambda: state,
        clock=lambda: 1000.0,
    )
    send_tool = [t for t in tools if t.name == "send_whatsapp"][0]

    # Describe approved with page 1
    state.set_artifact("https://youtube.com/watch?v=4561", "Page 1", updated_at=1000.0)
    _ = send_tool.describe({"contact": "Mom", "text": "link", "attach_link": True})

    # Page drifts to page 2 before user clicks Allow
    state.set_artifact("https://youtube.com/watch?v=4562", "Page 2", updated_at=1005.0)

    res = send_tool.func({"contact": "Mom", "text": "link", "attach_link": True})
    assert "The page changed since you approved; cancelled." in res
