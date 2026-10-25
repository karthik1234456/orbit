"""Web browsing and search tools for ORBIT with strict URL scheme and credential validation."""

from typing import Any, Optional
from urllib.parse import quote_plus, urlparse
import webbrowser

from security.permissions import PermissionLevel
from tools.registry import Tool

_MAX_URL_LENGTH = 2000
_MAX_QUERY_LENGTH = 200
_ALLOWED_SCHEMES = {"http", "https"}


def validate_website_url(url: Any) -> tuple[Optional[str], Optional[str]]:
    """Validate a website URL for open_website. Returns (clean_url, error_message)."""
    if not url or not isinstance(url, str):
        return None, "ERROR: URL must be a valid non-empty string."

    clean_url = url.strip()
    if len(clean_url) > _MAX_URL_LENGTH:
        return None, f"ERROR: URL exceeds maximum allowed length of {_MAX_URL_LENGTH} characters."

    # Reject embedded credentials (e.g. user:pass@host)
    if "@" in clean_url:
        return None, "ERROR: URLs with embedded credentials are not permitted."

    parsed = urlparse(clean_url)

    # Default to https scheme if no protocol scheme was provided
    if not parsed.scheme:
        clean_url = f"https://{clean_url}"
        parsed = urlparse(clean_url)

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None, (
            f"ERROR: URL scheme '{scheme}:' is not permitted. "
            f"Only http: and https: protocols are allowed."
        )

    if not parsed.netloc:
        return None, "ERROR: Invalid URL: missing host or domain name."

    return clean_url, None


def make_browser_tools() -> list[Tool]:
    """Create and return browser navigation and search tools with strict security controls."""

    def precheck_open_website(args: dict) -> Optional[str]:
        """Validate website URL without launching a browser."""
        url = args.get("url")
        _, err = validate_website_url(url)
        return err

    def open_website(url: str) -> str:
        """Open a validated website URL in the user's default browser."""
        clean_url, err = validate_website_url(url)
        if err:
            return err

        try:
            webbrowser.open(clean_url)
            return f"Opened website: {clean_url}"
        except Exception as exc:
            return f"ERROR: Failed to open browser for '{clean_url}': {exc}"

    def search_web(query: str) -> str:
        """Perform a Google search by safely encoding query parameters."""
        if not query or not isinstance(query, str) or not query.strip():
            return "ERROR: Search query cannot be empty."

        clean_query = query.strip()
        if len(clean_query) > _MAX_QUERY_LENGTH:
            return f"ERROR: Search query exceeds maximum length of {_MAX_QUERY_LENGTH} characters."

        encoded = quote_plus(clean_query)
        search_url = f"https://www.google.com/search?q={encoded}"

        try:
            webbrowser.open(search_url)
            return f"Opened search for '{clean_query}' in browser."
        except Exception as exc:
            return f"ERROR: Failed to open browser for search: {exc}"

    return [
        Tool(
            name="open_website",
            description=(
                "Open a website in the system default web browser. "
                "Only http and https protocols are permitted. "
                "URLs without a scheme default to https. Embedded credentials are rejected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The web URL to open (e.g. 'https://docs.python.org' or 'wikipedia.org').",
                    }
                },
                "required": ["url"],
            },
            func=open_website,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_open_website,
        ),
        Tool(
            name="search_web",
            description=(
                "Perform a web search using Google in the default browser. "
                "Query is automatically escaped and encoded. Max query length is 200 characters."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms to look up on the web (e.g. 'Python asyncio tutorial').",
                    }
                },
                "required": ["query"],
            },
            func=search_web,
            permission=PermissionLevel.SAFE,
        ),
    ]
