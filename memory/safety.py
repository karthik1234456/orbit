"""Memory safety validation, secret detection heuristics, and prompt sanitization.

SECURITY ARCHITECTURE NOTE:
The validations in this module act as heuristic defense-in-depth filters against
accidental secret leakage (API keys, passwords, credentials) and prompt injection
attacks attempting to hijack agent behavior through stored memories.

WHY HEURISTICS ARE NOT A COMPLETE DEFENSE:
Regex and keyword blocklists cannot guarantee complete protection against adversarial
prompt injection. Attackers can obfuscate instructions using encoding (e.g. base64, hex),
unicode homoglyphs, linguistic indirection, or split-payload semantic attacks. Similarly,
custom or novel credential formats may evade simple pattern matching.

WHY CONFIRMATION IS STILL REQUIRED:
Because memory content is injected into LLM context, it must always be treated as
potentially untrusted data. Memory safety filtering reduces the attack surface, but the
primary security boundary in ORBIT remains user confirmation (PermissionLevel.CONFIRM).
Sensitive desktop, file, and application actions must always require explicit user approval
regardless of what instructions or preferences are stored in memory.
"""

import re
from typing import Tuple

# Allowed semantic categories for memory records
ALLOWED_CATEGORIES = {"preference", "fact", "project", "other"}

# Maximum allowed memory character length
MAX_MEMORY_LENGTH = 300


class MemoryRejected(ValueError):
    """Raised when memory content fails safety checks or secret detection heuristics."""
    pass


# Standalone secret formats and common key patterns
_SECRET_PATTERNS = [
    # Google API keys (starts with AIza followed by 20+ URL-safe characters)
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "Looks like a Google API key."),
    # "sk-" style keys (e.g. OpenAI / Anthropic API keys)
    (re.compile(r"\bsk-[0-9A-Za-z\-_]{16,}"), "Looks like an API secret key ('sk-...')."),
    # AWS access key IDs (AKIA...)
    (re.compile(r"\bAKIA[0-9A-Za-z]{16,}\b"), "Looks like an AWS access key ('AKIA...')."),
    # GitHub personal access tokens (ghp_...)
    (re.compile(r"\bghp_[0-9A-Za-z]{10,}\b"), "Looks like a GitHub personal access token ('ghp_...')."),
    # Slack tokens (xox...)
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "Looks like a Slack token ('xox...')."),
    (re.compile(r"\bxox[0-9A-Za-z_-]{10,}\b"), "Looks like a Slack token ('xox...')."),
    # JWT-like strings (eyJ plus two dots)
    (re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"), "Looks like a JWT token ('eyJ...')."),
    # Continuous hex strings of 32+ characters
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "Contains a long continuous hex token/hash (32+ characters)."),
    # PEM headers
    (re.compile(r"(?i)-----BEGIN[^-]+-----"), "Looks like a PEM key/certificate header."),
    # Continuous high-entropy runs of 32+ characters without whitespace
    (re.compile(r"[a-zA-Z0-9_\-]{32,}"), "Contains a long continuous token/hash (32+ characters)."),
]

# Credential keywords that indicate sensitive secrets when paired with a value
_CREDENTIAL_WORDS_RE = re.compile(
    r"(?i)\b("
    r"passwords?|passwd|pwd|passcodes?|pins?|secrets?|tokens?|"
    r"api[\s_-]*keys?|apikeys?|private[\s_-]*keys?|credentials?|"
    r"root[\s_-]*keys?|access[\s_-]*keys?|logins?|auth(?:entication)?|wifi[\s_-]*keys?"
    r")\b"
)

# Credential assignment/connector pattern (word followed by :, =, ->, or 'is')
_CREDENTIAL_ASSIGN_RE = re.compile(
    r"(?i)\b("
    r"passwords?|passwd|pwd|passcodes?|pins?|secrets?|tokens?|"
    r"api[\s_-]*keys?|apikeys?|private[\s_-]*keys?|credentials?|"
    r"root[\s_-]*keys?|access[\s_-]*keys?|logins?|auth(?:entication)?|wifi[\s_-]*keys?"
    r")\s*(?:[:=]|->|\bis\b)\s*(\S+)"
)

# Common benign state/status words that follow 'is' but do not represent credential values
_STATUS_WORDS = {
    "enabled", "disabled", "required", "optional", "allowed", "forbidden",
    "missing", "safe", "strong", "weak", "active", "inactive", "valid",
    "invalid", "expired", "not", "none", "true", "false", "on", "off",
    "supported", "unsupported",
}

# Heuristic patterns for prompt injection / agent instruction attempts
_INJECTION_PATTERNS = [
    (re.compile(r"(?i)\bignore\s+(?:all\s+)?previous\b"), "Contains prompt override instruction ('ignore previous')."),
    (re.compile(r"(?i)\bignore\s+all\b"), "Contains prompt override instruction ('ignore all')."),
    (re.compile(r"(?i)\bdisregard\s+(?:all\s+)?(?:the\s+)?rules\b"), "Contains rule override instruction ('disregard the rules')."),
    (re.compile(r"(?i)\byou\s+must\s+always\b"), "Contains imperative agent override ('you must always')."),
    (re.compile(r"(?i)\bsystem\s+prompt\b"), "References internal system prompt."),
    (re.compile(r"(?i)\bwithout\s+asking\b"), "Attempts to bypass user permission checks ('without asking')."),
    (re.compile(r"(?i)\bdo\s+not\s+ask\s+for\s+permission\b"), "Attempts to bypass user permission checks ('do not ask for permission')."),
]


def validate_memory(content: str, category: str = "fact") -> Tuple[str, str]:
    """Validate and normalize memory text against security heuristics and size limits.

    Args:
        content: The text of the memory to validate.
        category: Category tag for the memory ('preference', 'fact', 'project', 'other').
            Unrecognized categories fall back to 'other'.

    Returns:
        tuple[str, str]: Normalized content and normalized category.

    Raises:
        MemoryRejected: If content is empty, too long (> 300 characters), resembles a secret,
            or contains prompt injection patterns.
    """
    if not isinstance(content, str):
        raise MemoryRejected("Memory content must be a text string.")

    # Strip and collapse all whitespace/newlines into single spaces
    normalized_content = " ".join(content.strip().split())

    if not normalized_content:
        raise MemoryRejected("Memory content cannot be empty.")

    if len(normalized_content) > MAX_MEMORY_LENGTH:
        raise MemoryRejected(
            f"Memory content is too long ({len(normalized_content)} characters). Maximum allowed is {MAX_MEMORY_LENGTH}."
        )

    # Normalize category: unknown categories gracefully fall back to 'other'
    normalized_category = category.strip().lower() if isinstance(category, str) else "other"
    if normalized_category not in ALLOWED_CATEGORIES:
        normalized_category = "other"

    # Check for secrets and credential-like values
    # NOTE: These checks act as heuristic defense-in-depth filters against credential leakage.
    # Regex heuristics are not an absolute guarantee against novel or obfuscated payloads;
    # the user confirmation prompt remains the primary, final security boundary in ORBIT.
    for pattern, reason in _SECRET_PATTERNS:
        if pattern.search(normalized_content):
            raise MemoryRejected(f"Rejected sensitive content: {reason}")

    # Reject any single token of 16+ characters that mixes letters and digits (whatever the context)
    for raw_token in normalized_content.split():
        clean_tok = raw_token.strip(".,;:!?\"'()[]{}<>~`")
        if len(clean_tok) >= 16:
            has_letters = any(c.isalpha() for c in clean_tok)
            has_digits = any(c.isdigit() for c in clean_tok)
            if has_letters and has_digits:
                raise MemoryRejected(
                    "Rejected sensitive content: Contains a token/hash of 16+ characters mixing letters and digits."
                )

    # Check for credential words combined with a value-like token or assignment
    cred_match = _CREDENTIAL_WORDS_RE.search(normalized_content)
    if cred_match:
        # Check explicit assignment/connector following a credential word (:, =, ->, or 'is')
        assign_match = _CREDENTIAL_ASSIGN_RE.search(normalized_content)
        if assign_match:
            val_token = assign_match.group(2).strip(".,;:!?\"'()[]{}<>~`")
            val_lower = val_token.lower()
            if val_lower not in _STATUS_WORDS:
                has_digit_or_symbol = any(c.isdigit() or (not c.isalnum() and not c.isspace()) for c in val_token)
                has_mixed_case = any(c.isupper() for c in val_token) and any(c.islower() for c in val_token)
                if has_digit_or_symbol or has_mixed_case or len(val_token) >= 4:
                    raise MemoryRejected(
                        "Rejected sensitive content: Looks like a credential assignment (password/secret/token)."
                    )

        # Check for any value-like token in the text when a credential word is present:
        # - a token of 4+ characters containing a digit or symbol
        # - or an 8+ character token mixing upper and lower case
        for raw_token in normalized_content.split():
            clean_tok = raw_token.strip(".,;:!?\"'()[]{}<>~`")
            if not clean_tok or _CREDENTIAL_WORDS_RE.fullmatch(clean_tok):
                continue
            has_digit_or_symbol = any(c.isdigit() or (not c.isalnum() and not c.isspace()) for c in clean_tok)
            if len(clean_tok) >= 4 and has_digit_or_symbol:
                raise MemoryRejected(
                    "Rejected sensitive content: Credential word paired with a value-like token."
                )
            has_mixed_case = any(c.isupper() for c in clean_tok) and any(c.islower() for c in clean_tok)
            if len(clean_tok) >= 8 and has_mixed_case:
                # Disallow if uppercase occurs after index 0 or token occurs in value position
                if any(c.isupper() for c in clean_tok[1:]) or raw_token in normalized_content[cred_match.end():]:
                    raise MemoryRejected(
                        "Rejected sensitive content: Credential word paired with a mixed-case value token."
                    )

    # Check for prompt injection / override instructions
    for pattern, reason in _INJECTION_PATTERNS:
        if pattern.search(normalized_content):
            raise MemoryRejected(f"Rejected instruction-like content: {reason}")

    return normalized_content, normalized_category


def sanitize_for_prompt(text: str) -> str:
    """Sanitize memory content before embedding it into an LLM prompt.

    Guarantees:
    - Collapses into a single line with whitespace normalized.
    - Strips prompt-formatting delimiters: '<', '>', and backticks '`'.
    - Truncates to at most 300 characters.
    """
    if not text:
        return ""

    # Convert to single line and normalize whitespace
    single_line = " ".join(str(text).strip().split())

    # Remove '<', '>', and '`'
    for char in ("<", ">", "`"):
        single_line = single_line.replace(char, "")

    # Truncate to 300 characters
    return single_line[:MAX_MEMORY_LENGTH]
