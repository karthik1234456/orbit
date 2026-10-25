"""Website allowlist and search template configuration for ORBIT.

Provides configured website destinations and optional URL search templates
for quick site navigation and direct search shortcuts.
"""

from typing import Any, Optional, TypedDict


class SiteConfig(TypedDict):
    """Configuration structure for an allowed website shortcut."""

    url: str
    search: Optional[str]


# Default allowed sites mapping friendly lowercase names to home URLs and search templates.
SITES: dict[str, dict[str, Any]] = {
    "youtube": {
        "url": "https://www.youtube.com",
        "search": "https://www.youtube.com/results?search_query={query}",
    },
    "google": {
        "url": "https://www.google.com",
        "search": "https://www.google.com/search?q={query}",
    },
    "github": {
        "url": "https://github.com",
        "search": "https://github.com/search?q={query}",
    },
    "wikipedia": {
        "url": "https://www.wikipedia.org",
        "search": "https://en.wikipedia.org/w/index.php?search={query}",
    },
    "gmail": {
        "url": "https://mail.google.com",
        "search": None,
    },
    "maps": {
        "url": "https://www.google.com/maps",
        "search": "https://www.google.com/maps/search/{query}",
    },
}

ALIASES: dict[str, str] = {
    "yt": "youtube",
    "you tube": "youtube",
    "gh": "github",
    "wiki": "wikipedia",
    "google maps": "maps",
}


def resolve_site(name: Any) -> Optional[str]:
    """Resolve a site name or alias to an exact SITES key (case-insensitive), or None."""
    if not name or not isinstance(name, str):
        return None
    clean = name.strip().lower()
    if clean in SITES:
        return clean
    if clean in ALIASES:
        return ALIASES[clean]
    return None
