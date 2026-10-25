"""Command shortcut parser for deterministic desktop actions without LLM calls.

Matches strict whitelisted phrase patterns for opening applications, opening websites,
opening workspace folders, and performing site-specific searches.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Optional

_DISALLOWED_CHARS = set(";&|`$><")


@dataclass
class ToolCallSpec:
    """Specification for a shortcut-invoked tool execution."""

    name: str
    args: dict[str, Any]
    target_name: str = ""
    id: Optional[str] = None


def parse_shortcut(
    text: str,
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
    state: Optional[Any] = None,
) -> Optional[ToolCallSpec]:
    """Parse user text into a ToolCallSpec if it matches a strict shortcut pattern.

    Returns None for any text that does not strictly match the whitelisted patterns,
    allowing the message to fall through to the model.
    """
    if not text or not isinstance(text, str):
        return None

    # Never match text containing newlines
    if "\n" in text or "\r" in text:
        return None

    # Never match text containing dangerous shell/command characters
    if any(ch in _DISALLOWED_CHARS for ch in text):
        return None

    # Never match text containing URLs
    if re.search(r"://|www\.|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?", text, re.IGNORECASE):
        return None

    raw = text.strip()
    if not raw:
        return None

    # Strip trailing punctuation
    cleaned = re.sub(r"[.!?,:\s]+$", "", raw).strip()
    if not cleaned:
        return None

    # Strip optional leading politeness prefixes (case-insensitive)
    while True:
        prev = cleaned
        cleaned = re.sub(
            r"^(?:please|can\s+you|can\s+u|could\s+you)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned == prev:
            break

    if not cleaned:
        return None

    # Pattern 1: Folder navigation
    # "open (my )?(workspace )?folders?" -> open_workspace_folder with path "."
    folder_match = re.match(
        r"^open\s+(?:my\s+)?(?:workspace\s+)?(folders?)$",
        cleaned,
        re.IGNORECASE,
    )
    if folder_match:
        matched_term = folder_match.group(1).lower()
        if "workspace" in cleaned.lower():
            target = f"workspace {matched_term}"
        else:
            target = matched_term
        return ToolCallSpec(
            name="open_workspace_folder",
            args={"path": "."},
            target_name=target,
        )

    if allowed_apps is None:
        from config.apps import ALLOWED_APPS
        allowed_apps = ALLOWED_APPS

    if sites is None:
        from config.sites import SITES
        sites = SITES

    from config.sites import resolve_site

    clean_apps_map = {k.lower(): k for k in allowed_apps}
    clean_sites_map = {k.lower(): k for k in sites}

    # Follow-up Pattern (c): "close (it|that|this)"
    close_it_match = re.match(r"^close\s+(it|that|this)$", cleaned, re.IGNORECASE)
    if close_it_match:
        if state and getattr(state, "last_app", None):
            last_app_key = str(state.last_app).lower()
            if last_app_key in clean_apps_map:
                app_canon = clean_apps_map[last_app_key]
                app_cfg = allowed_apps.get(app_canon)
                is_closeable = (
                    isinstance(app_cfg, Mapping)
                    and app_cfg.get("process") is not None
                )
                if is_closeable:
                    return ToolCallSpec(
                        name="close_application",
                        args={"name": app_canon},
                        target_name=app_canon,
                    )
        return None

    # Follow-up Pattern (d): "close <app>" for an exact allowed app key
    close_app_match = re.match(r"^close\s+(.+)$", cleaned, re.IGNORECASE)
    if close_app_match:
        cand_app = close_app_match.group(1).strip().lower()
        if cand_app in clean_apps_map:
            app_canon = clean_apps_map[cand_app]
            return ToolCallSpec(
                name="close_application",
                args={"name": app_canon},
                target_name=app_canon,
            )

    # Follow-up Pattern (b): "(search|look up|find|play) (it|that|this) (in|on) <site>"
    search_pronoun_match = re.match(
        r"^(?:search|look\s+up|find|play)\s+(?:it|that|this)\s+(?:in|on)\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if search_pronoun_match:
        cand_site = search_pronoun_match.group(1).strip()
        resolved_site = resolve_site(cand_site)
        if resolved_site and resolved_site in clean_sites_map:
            site_key = clean_sites_map[resolved_site]
            site_entry = sites.get(site_key)
            search_tmpl = None
            if isinstance(site_entry, dict):
                search_tmpl = site_entry.get("search") or site_entry.get("search_template")
            if search_tmpl:
                last_q = getattr(state, "last_query", None) if state else None
                if last_q:
                    return ToolCallSpec(
                        name="search_site",
                        args={"site": site_key, "query": last_q},
                        target_name=site_key,
                    )
        return None

    # Follow-up Pattern (a): "(in|on|at|using) <site>"
    in_site_match = re.match(r"^(in|on|at|using)\s+(.+)$", cleaned, re.IGNORECASE)
    if in_site_match:
        cand_site = in_site_match.group(2).strip()
        resolved_site = resolve_site(cand_site)
        if resolved_site and resolved_site in clean_sites_map:
            site_key = clean_sites_map[resolved_site]
            site_entry = sites.get(site_key)
            search_tmpl = None
            if isinstance(site_entry, dict):
                search_tmpl = site_entry.get("search") or site_entry.get("search_template")
            if search_tmpl:
                last_q = getattr(state, "last_query", None) if state else None
                if last_q:
                    return ToolCallSpec(
                        name="search_site",
                        args={"site": site_key, "query": last_q},
                        target_name=site_key,
                    )
        return None

    # Pattern 2: Open / Launch / Start application or website
    # "open|launch|start <name> [app|website|site]"
    open_match = re.match(
        r"^(open|launch|start)\s+(.+?)(?:\s+(app|website|site))?$",
        cleaned,
        re.IGNORECASE,
    )
    if open_match:
        cand_name = open_match.group(2).strip().lower()

        # If in allowed_apps (or both), use open_application
        if cand_name in clean_apps_map:
            app_key = clean_apps_map[cand_name]
            return ToolCallSpec(
                name="open_application",
                args={"name": app_key},
                target_name=app_key,
            )

        # Else if in sites, use open_website with site's home URL
        resolved_site = resolve_site(cand_name)
        if resolved_site and resolved_site in clean_sites_map:
            site_key = clean_sites_map[resolved_site]
            site_entry = sites[site_key]
            if isinstance(site_entry, dict):
                home_url = site_entry.get("url") or site_entry.get("home_url") or site_entry.get("home") or ""
            else:
                home_url = str(site_entry)
            return ToolCallSpec(
                name="open_website",
                args={"url": home_url},
                target_name=site_key,
            )
        elif cand_name in clean_sites_map:
            site_key = clean_sites_map[cand_name]
            site_entry = sites[site_key]
            if isinstance(site_entry, dict):
                home_url = site_entry.get("url") or site_entry.get("home_url") or site_entry.get("home") or ""
            else:
                home_url = str(site_entry)
            return ToolCallSpec(
                name="open_website",
                args={"url": home_url},
                target_name=site_key,
            )

    # Pattern 3: Site search
    # "search <site> for <query>" and "<query> on <site>"
    # ONLY when <site> is a SITES key with a search template and the query is 1-8 words
    search_for_match = re.match(r"^search\s+(.+?)\s+for\s+(.+)$", cleaned, re.IGNORECASE)
    query_on_match = re.match(r"^(.+)\s+on\s+(.+)$", cleaned, re.IGNORECASE)

    cand_site: Optional[str] = None
    cand_query: Optional[str] = None

    if search_for_match:
        cand_site = search_for_match.group(1).strip()
        cand_query = search_for_match.group(2).strip()
    elif query_on_match:
        q = query_on_match.group(1).strip()
        q_lower = q.lower()
        if q_lower == "search" or q_lower.startswith("search ") or q_lower.startswith("open "):
            return None
        cand_query = q
        cand_site = query_on_match.group(2).strip()

    if cand_site and cand_query:
        resolved_site = resolve_site(cand_site)
        site_lower = resolved_site if resolved_site else cand_site.lower()
        if site_lower in clean_sites_map:
            site_key = clean_sites_map[site_lower]
            site_entry = sites[site_key]
            search_tmpl = None
            if isinstance(site_entry, dict):
                search_tmpl = site_entry.get("search") or site_entry.get("search_template")

            if search_tmpl:
                words = cand_query.split()
                if 1 <= len(words) <= 8:
                    return ToolCallSpec(
                        name="search_site",
                        args={"site": site_key, "query": cand_query},
                        target_name=site_key,
                    )

    return None

