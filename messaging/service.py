"""Single source-of-truth composition, revalidation, and summary generation for messaging."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from browser.guards import is_blocked_url, is_interactive, is_signin_url
from contacts.store import ContactRef, mask_phone
from core.session_state import SessionState
from messaging.links import sanitize_message


class MessagingError(Exception):
    """Base exception for messaging operations."""
    pass


@dataclass(frozen=True)
class ComposedMessage:
    """Immutable composed message representation."""

    contact_ref: ContactRef
    final_text: str
    attached_url: Optional[str] = None


@dataclass(frozen=True)
class ComposedEmail:
    """Immutable composed email representation."""

    contact_ref: ContactRef
    subject: str
    body: str
    attached_url: Optional[str] = None


def _validate_attach_link(
    state: Optional[SessionState],
    clock: Optional[Callable[[], float]] = None,
) -> str:
    """Validate and extract the active page link from session artifacts.

    Guarantees:
    - https scheme required.
    - is_interactive host required.
    - No credentials in URL.
    - At most 300 characters.
    - TTL <= 30 minutes (1800 seconds).

    Raises:
        MessagingError: If link is missing, expired, non-https, or untrusted.
    """
    if state is None or state.artifacts is None:
        raise MessagingError("No active page link found to attach.")

    art = state.artifacts
    page_url = art.get("page_url", "")
    if not page_url:
        raise MessagingError("No active page link found to attach.")

    get_time = clock or time.time
    updated_at = float(art.get("updated_at", 0))
    if (get_time() - updated_at) > 1800.0:
        raise MessagingError("Page link has expired (> 30 minutes).")

    if len(page_url) > 300:
        raise MessagingError("Page link exceeds maximum length of 300 characters.")

    try:
        parsed = urlparse(page_url)
    except Exception as exc:
        raise MessagingError(f"Malformed page link: {exc}")

    if parsed.scheme.lower() != "https":
        raise MessagingError("Only secure HTTPS links can be attached.")

    if parsed.username or parsed.password or "@" in page_url:
        raise MessagingError("Page links with credentials cannot be attached.")

    if not is_interactive(page_url) or is_blocked_url(page_url) or is_signin_url(page_url):
        raise MessagingError("Page link does not belong to an allowed interactive site.")

    return page_url


def compose_whatsapp(
    contact_ref: ContactRef,
    text: str,
    attach_link: bool = False,
    state: Optional[SessionState] = None,
    clock: Optional[Callable[[], float]] = None,
) -> ComposedMessage:
    """Compose a WhatsApp message with optional revalidated link attachment.

    Single source of truth used by both describe() and tool execution.
    """
    clean_text = sanitize_message(text or "")
    attached_url: Optional[str] = None

    if attach_link:
        attached_url = _validate_attach_link(state, clock=clock)
        if clean_text:
            combined = f"{clean_text} {attached_url}"
        else:
            combined = attached_url
        final_text = sanitize_message(combined)
    else:
        final_text = clean_text

    return ComposedMessage(
        contact_ref=contact_ref,
        final_text=final_text,
        attached_url=attached_url,
    )


def summary(composed: ComposedMessage, mode: str = "draft") -> str:
    """Generate exact plain-text confirmation summary for a WhatsApp message.

    Returns:
        "WhatsApp to <display name> (<masked number>): will be DRAFTED, you press Enter to send"
        or
        "WhatsApp to <display name> (<masked number>): will be SENT immediately: '<final text>'"
    """
    masked = mask_phone(composed.contact_ref.phone)
    name = composed.contact_ref.display_name
    mode_clean = str(mode).strip().lower()

    if mode_clean == "send":
        return f"WhatsApp to {name} ({masked}): will be SENT immediately: '{composed.final_text}'"
    return f"WhatsApp to {name} ({masked}): will be DRAFTED, you press Enter to send"


def compose_email(
    contact_ref: ContactRef,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    attach_link: bool = False,
    state: Optional[SessionState] = None,
    clock: Optional[Callable[[], float]] = None,
) -> ComposedEmail:
    """Compose an email draft with optional revalidated link attachment."""
    clean_sub = sanitize_message(subject or "")[:80]
    clean_body = sanitize_message(body or "")[:500]
    attached_url: Optional[str] = None

    if attach_link:
        attached_url = _validate_attach_link(state, clock=clock)
        if clean_body:
            combined_body = f"{clean_body} {attached_url}"
        else:
            combined_body = attached_url
        clean_body = sanitize_message(combined_body)[:500]

    return ComposedEmail(
        contact_ref=contact_ref,
        subject=clean_sub,
        body=clean_body,
        attached_url=attached_url,
    )


def email_summary(composed: ComposedEmail) -> str:
    """Generate exact plain-text confirmation summary for an email draft."""
    name = composed.contact_ref.display_name
    email_addr = composed.contact_ref.email or "no email"
    return f"Email draft to {name} ({email_addr}): will be DRAFTED, you press Send in your mail app"

