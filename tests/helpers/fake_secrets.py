"""Runtime builders for synthetic credentials and tokens.

These helpers construct fake credentials dynamically from separate components at runtime
so that no complete token-shaped literals appear in test source files. This prevents
false positives from push protection systems, linters, and repository hygiene scanners.
"""

from typing import Optional


def fake_slack_token(kind: str = "b", team: str = "1234567890", token: str = "abcdef1234") -> str:
    """Build a synthetic Slack token at runtime."""
    prefix = "x" + "o" + "x" + kind + "-"
    return f"{prefix}{team}-{token}"


def fake_aws_access_key(middle: str = "IOSFODNN7", suffix: str = "EXAMPLE") -> str:
    """Build a synthetic AWS access key at runtime."""
    prefix = "AK" + "IA"
    return f"{prefix}{middle}{suffix}"


def fake_aws_secret_key(prefix: str = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY", suffix: str = "EXAMPLEKEY") -> str:
    """Build a synthetic AWS secret key at runtime."""
    return f"{prefix}{suffix}"


def fake_github_token(kind: str = "p", suffix: str = "1234567890abcdef1234") -> str:
    """Build a synthetic GitHub personal access token at runtime."""
    prefix = "gh" + kind + "_"
    return f"{prefix}{suffix}"


def fake_github_pat(suffix: str = "1234567890abcdefghijklmnopqrstuvwxyz") -> str:
    """Build a synthetic fine-grained GitHub token at runtime."""
    prefix = "github" + "_pat_"
    return f"{prefix}{suffix}"


def fake_jwt_token(
    header: Optional[str] = None,
    payload: Optional[str] = None,
    signature: str = "abc",
) -> str:
    """Build a synthetic JWT token string at runtime."""
    ey_j = "ey" + "J"
    h = header if header is not None else f"{ey_j}hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    p = payload if payload is not None else f"{ey_j}zdWIiOiIxMjM0NTY3ODkwIn0"
    return f"{h}.{p}.{signature}"


def fake_pem_header(key_type: str = "RSA") -> str:
    """Build a synthetic PEM private key header at runtime."""
    dashes = "-" * 5
    begin = "BEGIN"
    priv = "PRIVATE"
    key = "KEY"
    mid = f" {key_type}" if key_type else ""
    return f"{dashes}{begin}{mid} {priv} {key}{dashes}"


def fake_pem_private_key(key_type: str = "RSA", body: str = "MIIEow...") -> str:
    """Build a synthetic PEM private key block at runtime."""
    dashes = "-" * 5
    mid = f" {key_type}" if key_type else ""
    priv = "PRIVATE"
    key = "KEY"
    header = f"{dashes}BEGIN{mid} {priv} {key}{dashes}"
    footer = f"{dashes}END{mid} {priv} {key}{dashes}"
    return f"{header}\n{body}\n{footer}"


def fake_sk_key(suffix: str = "proj-1234567890abcdef12345678") -> str:
    """Build a synthetic secret key at runtime."""
    prefix = "s" + "k-"
    return f"{prefix}{suffix}"


# Alias for OpenAI style keys
fake_openai_key = fake_sk_key


def fake_google_api_key(suffix: str = "SyA1234567890abcdef123456") -> str:
    """Build a synthetic Google API key at runtime."""
    prefix = "AI" + "za"
    return f"{prefix}{suffix}"


def fake_generic_api_key(prefix: str = "key", suffix: str = "4a9f-88b1-c302-ef99") -> str:
    """Build a synthetic generic API key at runtime."""
    return f"{prefix}_{suffix}"
