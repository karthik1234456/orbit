"""URI and URL builders for WhatsApp click-to-chat and email drafting.

Enforces strict input sanitization: stripping control characters, zero-width spaces,
and bidirectional overrides; capping lengths; and rejecting CR/LF injection.
"""

from __future__ import annotations

import re
import urllib.parse

# Phone digits: 8-15 digits, no leading zero
_PHONE_PATTERN = re.compile(r"^[1-9][0-9]{7,14}$")

# Email validation: simple address check, max 120 chars
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def sanitize_message(text: str) -> str:
    """Sanitize message content for links and drafts.

    Guarantees:
    - Collapses all whitespace into single spaces (no CR or LF).
    - Removes control characters (ord < 32 or 127 <= ord <= 159).
    - Removes zero-width and bidirectional-override characters:
      U+200B-U+200F, U+202A-U+202E, U+2066-U+2069.
    - Capped at 300 characters.
    """
    if not text or not isinstance(text, str):
        return ""

    cleaned_chars: list[str] = []
    for c in text:
        cp = ord(c)
        # Check zero-width characters (U+200B to U+200F)
        if 0x200B <= cp <= 0x200F:
            continue
        # Check bidirectional embedding / overrides (U+202A to U+202E)
        if 0x202A <= cp <= 0x202E:
            continue
        # Check directional isolates (U+2066 to U+2069)
        if 0x2066 <= cp <= 0x2069:
            continue
        # Check ASCII / Latin-1 control characters
        if cp < 32 or (127 <= cp <= 159):
            # Replace control char with a space
            cleaned_chars.append(" ")
            continue
        cleaned_chars.append(c)

    # Collapse whitespace and remove any leading/trailing spaces
    collapsed = " ".join("".join(cleaned_chars).split())
    # Cap length at 300 characters
    return collapsed[:300]


def build_whatsapp_uri(digits: str, text: str) -> str:
    """Build a whatsapp://send URI.

    Args:
        digits: Recipient phone number (8-15 digits, no leading zero).
        text: Message body to draft.

    Raises:
        ValueError: If phone digits are invalid.
    """
    clean_digits = str(digits).strip()
    if not _PHONE_PATTERN.match(clean_digits):
        raise ValueError(f"Invalid phone number for WhatsApp: '{clean_digits}'")

    sanitized = sanitize_message(text)
    encoded = urllib.parse.quote(sanitized, safe="")
    return f"whatsapp://send?phone={clean_digits}&text={encoded}"


def build_wame_url(digits: str, text: str) -> str:
    """Build a click-to-chat fallback URL (https://wa.me/...).

    Args:
        digits: Recipient phone number (8-15 digits, no leading zero).
        text: Message body to draft.

    Raises:
        ValueError: If phone digits are invalid.
    """
    clean_digits = str(digits).strip()
    if not _PHONE_PATTERN.match(clean_digits):
        raise ValueError(f"Invalid phone number for wa.me: '{clean_digits}'")

    sanitized = sanitize_message(text)
    encoded = urllib.parse.quote(sanitized, safe="")
    return f"https://wa.me/{clean_digits}?text={encoded}"


def build_mailto(email: str, subject: str = "", body: str = "") -> str:
    """Build a mailto: URI for drafting an email.

    Guarantees:
        - Subject capped at 80 characters.
        - Body capped at 500 characters.
        - Strictly NO cc or bcc parameters.
        - Strictly NO CR or LF in subject or body.

    Raises:
        ValueError: If email address is invalid.
    """
    clean_email = str(email).strip()
    if len(clean_email) > 120 or not _EMAIL_PATTERN.match(clean_email):
        raise ValueError(f"Invalid email address for mailto: '{clean_email}'")

    clean_sub = sanitize_message(subject)[:80]
    clean_body = sanitize_message(body)[:500]

    enc_sub = urllib.parse.quote(clean_sub, safe="")
    enc_body = urllib.parse.quote(clean_body, safe="")

    params: list[str] = []
    if enc_sub:
        params.append(f"subject={enc_sub}")
    if enc_body:
        params.append(f"body={enc_body}")

    query = f"?{'&'.join(params)}" if params else ""
    return f"mailto:{clean_email}{query}"

