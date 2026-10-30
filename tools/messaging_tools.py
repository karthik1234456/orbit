"""Messaging and email draft tools with rate limiting, verification, and privacy guards."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional
import webbrowser

from config import settings
from config.paths import is_demo_mode
from contacts.store import ContactNotFoundError, ContactRef, ContactStore
from core.session_state import SessionState
from messaging.links import build_mailto, build_wame_url, build_whatsapp_uri
from messaging.service import (
    MessagingError,
    compose_email,
    compose_whatsapp,
    email_summary,
    summary,
)
from security.permissions import PermissionLevel
from system.winwindow import (
    DEFAULT_WHATSAPP_PROCESS_NAMES,
    foreground_window,
    verify_whatsapp_window,
    wait_for_foreground,
)
from tools.registry import Tool

VK_RETURN: int = 0x0D


class MessagingRateLimiter:
    """Sliding-window rate limiter for messaging operations.

    Enforces:
    - Maximum 5 messages or drafts per 10 minutes overall.
    - Minimum 10 seconds between actions to the same contact.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self.clock = clock or time.time
        self.overall_timestamps: list[float] = []
        self.contact_timestamps: dict[int, float] = {}

    def check(self, contact_id: int) -> Optional[str]:
        """Check if an action is permitted under rate limits. Returns error or None."""
        now = self.clock()
        self.overall_timestamps = [t for t in self.overall_timestamps if (now - t) <= 600.0]
        if len(self.overall_timestamps) >= 5:
            return "ERROR: too many messages, try again later."
        last_contact = self.contact_timestamps.get(contact_id)
        if last_contact is not None and (now - last_contact) < 10.0:
            return "ERROR: too many messages, try again later."
        return None

    def record(self, contact_id: int) -> None:
        """Record an executed messaging action."""
        now = self.clock()
        self.overall_timestamps.append(now)
        self.contact_timestamps[contact_id] = now


def make_messaging_tools(
    contacts: ContactStore,
    state_getter: Callable[[], Optional[SessionState]],
    opener: Optional[Callable[[str], bool]] = None,
    key_sender: Optional[Callable[[], None]] = None,
    foreground: Optional[Callable[[], tuple[Optional[str], Optional[str]]]] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> list[Tool]:
    """Construct safe messaging and email drafting tools.

    All tools:
    - model_visible = False
    - permission = CONFIRM
    - block_when_tainted = True
    - parameterized prechecks and describe summaries
    """
    get_time = clock or time.time
    do_sleep = sleep or time.sleep
    rate_limiter = MessagingRateLimiter(clock=get_time)

    # Track last approved text per contact to detect page drift between describe and execute
    approved_texts: dict[int, str] = {}

    def _default_opener(uri: str) -> bool:
        from system.winkeys import open_uri
        try:
            open_uri(uri)
            return True
        except Exception:
            return False

    def _default_key_sender() -> None:
        from system.winkeys import send_key
        send_key(VK_RETURN)

    actual_opener = opener or _default_opener
    actual_sender = key_sender or _default_key_sender
    actual_fg = foreground or foreground_window

    def _is_messaging_enabled() -> bool:
        if is_demo_mode():
            return False
        return bool(getattr(settings, "MESSAGING_ENABLED", True))

    def _extract_args(pos_args: tuple, kwargs: dict) -> dict[str, Any]:
        if pos_args and isinstance(pos_args[0], dict):
            return pos_args[0]
        return dict(kwargs)

    # --- send_whatsapp ---

    def _whatsapp_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        args = _extract_args(pos_args, kwargs)
        if not _is_messaging_enabled():
            if is_demo_mode():
                return "Messaging is disabled in demo mode."
            return "ERROR: messaging is disabled."

        contact_name = str(args.get("contact", "")).strip()
        if not contact_name:
            return "ERROR: Missing contact name. Add contacts in Contacts."

        try:
            ref = contacts.resolve(contact_name)
        except ContactNotFoundError:
            return f"ERROR: I don't have a contact called {contact_name}. Add it in Contacts."
        except Exception as exc:
            return f"ERROR: Could not resolve contact: {exc}"

        if not ref.phone:
            return f"ERROR: Contact '{ref.display_name}' has no phone number in Contacts."

        limit_err = rate_limiter.check(ref.id)
        if limit_err:
            return limit_err

        attach_link = bool(args.get("attach_link", False))
        if attach_link:
            state = state_getter()
            try:
                compose_whatsapp(ref, str(args.get("text", "")), attach_link=True, state=state, clock=get_time)
            except MessagingError as merr:
                return f"ERROR: {merr}"

        return None

    def _whatsapp_describe(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        contact_name = str(args.get("contact", "")).strip()
        try:
            ref = contacts.resolve(contact_name)
        except Exception:
            ref = ContactRef(id=0, display_name=contact_name or "Unknown")

        attach_link = bool(args.get("attach_link", False))
        state = state_getter()
        try:
            composed = compose_whatsapp(
                ref,
                str(args.get("text", "")),
                attach_link=attach_link,
                state=state,
                clock=get_time,
            )
        except Exception:
            composed = compose_whatsapp(ref, str(args.get("text", "")), attach_link=False)

        mode = getattr(settings, "WHATSAPP_SEND_MODE", "draft")
        approved_texts[ref.id] = composed.final_text
        return summary(composed, mode=mode)

    def _whatsapp_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_messaging_enabled():
            if is_demo_mode():
                return "Messaging is disabled in demo mode."
            return "ERROR: messaging is disabled."

        contact_name = str(args.get("contact", "")).strip()
        ref = contacts.resolve(contact_name)
        if not ref.phone:
            return f"ERROR: Contact '{ref.display_name}' has no phone number in Contacts."

        limit_err = rate_limiter.check(ref.id)
        if limit_err:
            return limit_err

        attach_link = bool(args.get("attach_link", False))
        state = state_getter()

        try:
            composed = compose_whatsapp(
                ref,
                str(args.get("text", "")),
                attach_link=attach_link,
                state=state,
                clock=get_time,
            )
        except MessagingError as merr:
            return f"ERROR: {merr}"

        # Re-check text equality against approved summary
        expected_text = approved_texts.get(ref.id)
        if expected_text is not None and composed.final_text != expected_text:
            return "The page changed since you approved; cancelled."

        # Record action in rate limiter
        rate_limiter.record(ref.id)

        # Build uri
        uri = build_whatsapp_uri(ref.phone, composed.final_text)

        # Attempt to open native uri
        open_ok = actual_opener(uri)
        if not open_ok:
            # Fallback to browser wa.me link
            fallback_url = build_wame_url(ref.phone, composed.final_text)
            try:
                webbrowser.open(fallback_url)
            except Exception:
                pass
            return "Opened the draft in the browser (wa.me); a browser cannot be controlled. Press Enter to send."

        # Check send mode
        mode = getattr(settings, "WHATSAPP_SEND_MODE", "draft").strip().lower()
        if mode == "send" and composed.final_text.strip():
            proc_names = getattr(settings, "WHATSAPP_PROCESS_NAMES", DEFAULT_WHATSAPP_PROCESS_NAMES)

            # Wait up to 6 seconds for verified foreground window
            passed = wait_for_foreground(
                lambda t, p: verify_whatsapp_window(t, p, proc_names),
                timeout=6.0,
                foreground_fn=actual_fg,
                clock=get_time,
                sleep=do_sleep,
            )
            if not passed:
                return "Draft is open but I could not verify the WhatsApp window, so I did not press Enter."

            # Wait 1.2 seconds settle time
            do_sleep(1.2)

            # Verify again
            title, proc = actual_fg()
            if not verify_whatsapp_window(title, proc, proc_names):
                return "Draft is open but I could not verify the WhatsApp window, so I did not press Enter."

            # Press Enter once
            actual_sender()
            return "Sent the draft by pressing Enter; I can't confirm delivery."

        return "Opened the draft in WhatsApp; press Enter to send."

    send_whatsapp_tool = Tool(
        name="send_whatsapp",
        description="Send or draft a WhatsApp message to an existing contact from the local contact book.",
        parameters={
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "Exact contact name or alias."},
                "text": {"type": "string", "description": "Message text (optional)."},
                "attach_link": {"type": "boolean", "description": "Attach current interactive page link."},
            },
            "required": ["contact"],
        },
        func=_whatsapp_exec,
        permission=PermissionLevel.CONFIRM,
        untrusted_output=False,
        precheck=_whatsapp_precheck,
        model_visible=False,
        block_when_tainted=True,
        describe=_whatsapp_describe,
    )

    # --- open_email_draft ---

    def _email_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        args = _extract_args(pos_args, kwargs)
        if not _is_messaging_enabled():
            if is_demo_mode():
                return "Messaging is disabled in demo mode."
            return "ERROR: messaging is disabled."

        contact_name = str(args.get("contact", "")).strip()
        if not contact_name:
            return "ERROR: Missing contact name. Add contacts in Contacts."

        try:
            ref = contacts.resolve(contact_name)
        except ContactNotFoundError:
            return f"ERROR: I don't have a contact called {contact_name}. Add it in Contacts."
        except Exception as exc:
            return f"ERROR: Could not resolve contact: {exc}"

        if not ref.email:
            return f"ERROR: Contact '{ref.display_name}' has no email address in Contacts."

        limit_err = rate_limiter.check(ref.id)
        if limit_err:
            return limit_err

        attach_link = bool(args.get("attach_link", False))
        if attach_link:
            state = state_getter()
            try:
                compose_email(
                    ref,
                    subject=args.get("subject"),
                    body=args.get("body"),
                    attach_link=True,
                    state=state,
                    clock=get_time,
                )
            except MessagingError as merr:
                return f"ERROR: {merr}"

        return None

    def _email_describe(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        contact_name = str(args.get("contact", "")).strip()
        try:
            ref = contacts.resolve(contact_name)
        except Exception:
            ref = ContactRef(id=0, display_name=contact_name or "Unknown")

        attach_link = bool(args.get("attach_link", False))
        state = state_getter()
        try:
            composed = compose_email(
                ref,
                subject=args.get("subject"),
                body=args.get("body"),
                attach_link=attach_link,
                state=state,
                clock=get_time,
            )
        except Exception:
            composed = compose_email(
                ref,
                subject=args.get("subject"),
                body=args.get("body"),
                attach_link=False,
            )

        approved_texts[ref.id] = f"{composed.subject}|{composed.body}"
        return email_summary(composed)

    def _email_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_messaging_enabled():
            if is_demo_mode():
                return "Messaging is disabled in demo mode."
            return "ERROR: messaging is disabled."

        contact_name = str(args.get("contact", "")).strip()
        ref = contacts.resolve(contact_name)
        if not ref.email:
            return f"ERROR: Contact '{ref.display_name}' has no email address in Contacts."

        limit_err = rate_limiter.check(ref.id)
        if limit_err:
            return limit_err

        attach_link = bool(args.get("attach_link", False))
        state = state_getter()

        try:
            composed = compose_email(
                ref,
                subject=args.get("subject"),
                body=args.get("body"),
                attach_link=attach_link,
                state=state,
                clock=get_time,
            )
        except MessagingError as merr:
            return f"ERROR: {merr}"

        expected_text = approved_texts.get(ref.id)
        actual_text = f"{composed.subject}|{composed.body}"
        if expected_text is not None and actual_text != expected_text:
            return "The page changed since you approved; cancelled."

        rate_limiter.record(ref.id)

        mailto_uri = build_mailto(ref.email, subject=composed.subject, body=composed.body)
        open_ok = actual_opener(mailto_uri)
        if not open_ok:
            try:
                webbrowser.open(mailto_uri)
            except Exception:
                pass

        return "Opened an email draft; press Send in your mail app."

    open_email_draft_tool = Tool(
        name="open_email_draft",
        description="Open an email draft in the default mail client for an existing contact.",
        parameters={
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "Exact contact name or alias."},
                "subject": {"type": "string", "description": "Email subject (optional)."},
                "body": {"type": "string", "description": "Email body (optional)."},
                "attach_link": {"type": "boolean", "description": "Attach current interactive page link."},
            },
            "required": ["contact"],
        },
        func=_email_exec,
        permission=PermissionLevel.CONFIRM,
        untrusted_output=False,
        precheck=_email_precheck,
        model_visible=False,
        block_when_tainted=True,
        describe=_email_describe,
    )

    return [send_whatsapp_tool, open_email_draft_tool]

