"""Interactive browser site configuration and host resolution for ORBIT.

Restricts browser automation to approved public interactive web properties.
Email and messaging portals (such as Gmail and WhatsApp Web) are deliberately
excluded from interactive browser control to protect user privacy and sensitive data.
"""

from typing import Optional, Set
from urllib.parse import urlparse

from config.sites import SITES

# Only these site keys from SITES are permitted for interactive browser control.
INTERACTIVE_SITES: list[str] = ["youtube", "google", "wikipedia", "github"]

# Hostnames explicitly barred from interactive automation even if under an allowed domain.
NON_INTERACTIVE_HOSTS: Set[str] = {
    "mail.google.com",
    "web.whatsapp.com",
    "whatsapp.com",
}

# CSS selectors for finding clickable search result links on supported interactive sites.
# Note: Web site layouts and DOM structures change frequently over time.
# If search result clicking fails on updated sites, update these CSS selector lists here.
SELECTORS: dict[str, list[str]] = {
    "youtube": [
        "ytd-video-renderer a#video-title",
        "ytd-video-renderer a#video-title-link",
        "ytd-rich-item-renderer a#video-title-link",
        "a#video-title",
        "ytd-video-renderer a.ytd-video-renderer",
        "ytd-compact-video-renderer a#video-title",
    ],
    "google": [
        "#search .g a[href]:has(h3)",
        "#rso .g a[href]:has(h3)",
        ".g a[href]:has(h3)",
        "div[data-sokoban-container] a[href]:has(h3)",
    ],
    "wikipedia": [
        ".mw-search-result-heading a",
        ".mw-search-results a",
        "#mw-content-text .mw-search-result-title a",
        "ul.mw-search-results li a",
    ],
    "github": [
        "div[data-testid='results-list'] a[href]",
        ".repo-list-item a",
        ".search-title a",
        "a.v-align-middle",
    ],
}


def interactive_hosts() -> Set[str]:
    """Derive the set of allowed hostnames and domain roots from INTERACTIVE_SITES in SITES.

    Includes base domains (e.g. 'youtube.com', 'google.com', 'wikipedia.org', 'github.com')
    and exact hostnames defined in site configs (e.g. 'www.youtube.com', 'en.wikipedia.org').
    """
    hosts: Set[str] = set()

    for site_key in INTERACTIVE_SITES:
        cfg = SITES.get(site_key)
        if not cfg or not isinstance(cfg, dict):
            continue

        for field_name in ("url", "search"):
            raw_url = cfg.get(field_name)
            if not raw_url or not isinstance(raw_url, str):
                continue
            # Strip template parameters like {query} before parsing URL
            clean_url = raw_url.split("{")[0]
            try:
                parsed = urlparse(clean_url)
                hostname = (parsed.hostname or "").strip().lower()
                if hostname:
                    hosts.add(hostname)
                    if hostname.startswith("www."):
                        hosts.add(hostname[4:])
                    parts = hostname.split(".")
                    if len(parts) >= 2:
                        # Add root domain (e.g. 'wikipedia.org' from 'en.wikipedia.org')
                        base_domain = ".".join(parts[-2:])
                        hosts.add(base_domain)
            except Exception:
                continue

    return hosts
