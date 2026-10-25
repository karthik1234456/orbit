"""Command shortcut parser for deterministic desktop actions without LLM calls.

Matches strict whitelisted phrase patterns for opening applications, opening websites,
opening workspace folders, and performing site-specific searches.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Optional, Union

_DISALLOWED_CHARS = set(";&|`$><")


@dataclass
class ToolCallSpec:
    """Specification for a shortcut-invoked tool execution."""

    name: str
    args: dict[str, Any]
    target_name: str = ""
    id: Optional[str] = None


@dataclass
class ShortcutPlan:
    """Specification for a multi-step shortcut execution chain."""

    steps: list[ToolCallSpec]
    site: str = ""
    query: str = ""
    is_play: bool = False
    ordinal: int = 1
    style: str = ""


def parse_shortcut(
    text: str,
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
    state: Optional[Any] = None,
    browser_enabled: bool = False,
) -> Optional[Union[ToolCallSpec, ShortcutPlan]]:
    """Parse user text into a ToolCallSpec or ShortcutPlan if it matches a strict shortcut pattern.

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

    if browser_enabled:
        if sites is None:
            from config.sites import SITES
            sites = SITES
        from config.sites import resolve_site
        clean_sites_map = {k.lower(): k for k in sites}

        # Pattern B: Media controls (bare)
        # "pause" or "pause the video", "play" or "resume" or "play the video" (bare), "mute", "unmute"
        if re.match(r"^(?:pause|pause\s+the\s+video)$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "pause"},
                target_name="pause",
            )

        if re.match(r"^(?:play|resume|play\s+the\s+video)$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "play"},
                target_name="play",
            )

        if re.match(r"^mute$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "mute"},
                target_name="mute",
            )

        if re.match(r"^unmute$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "unmute"},
                target_name="unmute",
            )

        # Pattern C: History controls
        # "go back", "go forward", "reload" or "refresh"
        if re.match(r"^go\s+back$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_history",
                args={"action": "back"},
                target_name="go back",
            )

        if re.match(r"^go\s+forward$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_history",
                args={"action": "forward"},
                target_name="go forward",
            )

        if re.match(r"^(?:reload|refresh)$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_history",
                args={"action": "reload"},
                target_name="reload",
            )

        # Pattern A: Scrolling
        # "scroll down|up", "scroll to the top|bottom", "scroll down N screens|pages" (1-5), and bare "down"|"up"
        if re.match(r"^(?:down|up)$", cleaned, re.IGNORECASE):
            d = cleaned.lower()
            return ToolCallSpec(
                name="browser_scroll",
                args={"direction": d, "screens": 1},
                target_name=f"scroll {d}",
            )

        scroll_dir_match = re.match(r"^scroll\s+(down|up)$", cleaned, re.IGNORECASE)
        if scroll_dir_match:
            d = scroll_dir_match.group(1).lower()
            return ToolCallSpec(
                name="browser_scroll",
                args={"direction": d, "screens": 1},
                target_name=f"scroll {d}",
            )

        scroll_to_match = re.match(r"^scroll\s+to\s+(?:the\s+)?(top|bottom)$", cleaned, re.IGNORECASE)
        if scroll_to_match:
            d = scroll_to_match.group(1).lower()
            return ToolCallSpec(
                name="browser_scroll",
                args={"direction": d, "screens": 1},
                target_name=f"scroll to {d}",
            )

        scroll_n_match = re.match(r"^scroll\s+(down|up)\s+(\w+)\s+(?:screens?|pages?)$", cleaned, re.IGNORECASE)
        if scroll_n_match:
            d = scroll_n_match.group(1).lower()
            n_str = scroll_n_match.group(2).lower()
            n_map = {
                "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
                "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            }
            if n_str in n_map:
                sc = n_map[n_str]
                return ToolCallSpec(
                    name="browser_scroll",
                    args={"direction": d, "screens": sc},
                    target_name=f"scroll {d} {sc} screens",
                )
            return None

        # Pattern D: Click Result & Ordinal Search Chains
        # "play|open|click (the)? <ordinal> (video|result|one)? [of|for|from]? <query> [on|in <site>]"
        ord_token_pattern = r"(10th|[1-9](?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|top)"
        num_token_pattern = r"(10th|10|[1-9](?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|top|one|two|three|four|five|six|seven|eight|nine|ten)"
        click_ord_match = re.match(
            rf"^(?:play|open|click)(?:\s+the)?\s+{ord_token_pattern}(?:\s+(?:video|result|one))?(?:\s+(.*))?$",
            cleaned,
            re.IGNORECASE,
        )
        if not click_ord_match:
            click_ord_match = re.match(
                rf"^(?:play|open|click)\s+number\s+{num_token_pattern}(?:\s+(.*))?$",
                cleaned,
                re.IGNORECASE,
            )

        if click_ord_match:
            ord_map = {
                "first": 1, "1st": 1, "top": 1, "one": 1, "1": 1,
                "second": 2, "2nd": 2, "two": 2, "2": 2,
                "third": 3, "3rd": 3, "three": 3, "3": 3,
                "fourth": 4, "4th": 4, "four": 4, "4": 4,
                "fifth": 5, "5th": 5, "five": 5, "5": 5,
                "sixth": 6, "6th": 6, "six": 6, "6": 6,
                "seventh": 7, "7th": 7, "seven": 7, "7": 7,
                "eighth": 8, "8th": 8, "eight": 8, "8": 8,
                "ninth": 9, "9th": 9, "nine": 9, "9": 9,
                "tenth": 10, "10th": 10, "ten": 10, "10": 10,
            }
            n = ord_map[click_ord_match.group(1).lower()]
            rem = (click_ord_match.group(2) or "").strip()

            if not rem:
                return ToolCallSpec(
                    name="browser_click_result",
                    args={"n": n},
                    target_name=f"result #{n}",
                )

            if rem.lower() in ("of", "for", "from"):
                return None

            conn_match = re.match(r"^(?:of|for|from)\s+(.+)$", rem, re.IGNORECASE)
            if conn_match:
                rem = conn_match.group(1).strip()

            if not rem:
                return None

            target_site = None
            query_candidate = rem

            site_suffix_match = re.search(r"\s+(?:on|in)\s+([a-zA-Z0-9_-]+)$", rem, re.IGNORECASE)
            if site_suffix_match:
                cand_site = site_suffix_match.group(1).strip()
                resolved_s = resolve_site(cand_site) or cand_site.lower()
                if resolved_s in clean_sites_map:
                    s_cfg = sites.get(resolved_s)
                    if isinstance(s_cfg, dict) and s_cfg.get("search"):
                        target_site = resolved_s
                        query_candidate = rem[: site_suffix_match.start()].strip()

            if not query_candidate:
                return None

            words = query_candidate.split()
            if not (1 <= len(words) <= 8):
                return None

            if target_site is None:
                last_s = getattr(state, "last_site", None) if state else None
                if last_s:
                    from config.browser_sites import INTERACTIVE_SITES
                    resolved_last = resolve_site(str(last_s)) or str(last_s).lower()
                    if resolved_last in INTERACTIVE_SITES and resolved_last in clean_sites_map:
                        s_cfg = sites.get(resolved_last)
                        if isinstance(s_cfg, dict) and s_cfg.get("search"):
                            target_site = resolved_last

            if target_site is None:
                target_site = "youtube"

            site_entry = sites.get(target_site)
            if not (isinstance(site_entry, dict) and site_entry.get("search")):
                return None

            steps = [
                ToolCallSpec(name="search_site", args={"site": target_site, "query": query_candidate}, target_name=target_site),
                ToolCallSpec(name="browser_click_result", args={"n": n}, target_name=f"result #{n}"),
                ToolCallSpec(name="browser_media", args={"action": "play"}, target_name="play"),
            ]
            return ShortcutPlan(
                steps=steps,
                site=target_site,
                query=query_candidate,
                is_play=True,
                ordinal=n,
                style="ordinal",
            )

        # Pattern E: Read Page
        # "what's on this page" and "read the page" -> browser_read_page
        if re.match(r"^(?:what'?s\s+on\s+this\s+page|read\s+the\s+page)$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_read_page",
                args={},
                target_name="page",
            )

        # Pattern F: Close Browser
        if re.match(r"^close\s+(?:the\s+)?browser$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_close",
                args={},
                target_name="browser",
            )

        # Pattern G: Chains
        # "open|go to <site> and play|search for <query>" and "play <query> on <site>"
        chain_a_match = re.match(
            r"^(?:open|go\s+to)\s+(.+?)\s+and\s+(play|search\s+for)\s+(.+)$",
            cleaned,
            re.IGNORECASE,
        )
        chain_b_match = re.match(
            r"^play\s+(.+?)\s+on\s+(.+)$",
            cleaned,
            re.IGNORECASE,
        )

        cand_site_chain: Optional[str] = None
        cand_query_chain: Optional[str] = None
        is_play_chain: bool = False

        if chain_a_match:
            cand_site_chain = chain_a_match.group(1).strip()
            act_word = chain_a_match.group(2).strip().lower()
            is_play_chain = act_word == "play"
            cand_query_chain = chain_a_match.group(3).strip()
        elif chain_b_match:
            cand_query_chain = chain_b_match.group(1).strip()
            cand_site_chain = chain_b_match.group(2).strip()
            is_play_chain = True

        if cand_site_chain is not None and cand_query_chain is not None:
            resolved = resolve_site(cand_site_chain)
            site_key = (resolved or cand_site_chain).lower()
            if site_key in clean_sites_map:
                site_cfg = sites[site_key]
                search_tmpl = None
                if isinstance(site_cfg, dict):
                    search_tmpl = site_cfg.get("search") or site_cfg.get("search_template")
                if search_tmpl:
                    words = cand_query_chain.split()
                    if 1 <= len(words) <= 8:
                        if is_play_chain:
                            steps = [
                                ToolCallSpec(name="search_site", args={"site": site_key, "query": cand_query_chain}, target_name=site_key),
                                ToolCallSpec(name="browser_click_result", args={"n": 1}, target_name="first result"),
                                ToolCallSpec(name="browser_media", args={"action": "play"}, target_name="play"),
                            ]
                            return ShortcutPlan(steps=steps, site=site_key, query=cand_query_chain, is_play=True)
                        else:
                            steps = [
                                ToolCallSpec(name="search_site", args={"site": site_key, "query": cand_query_chain}, target_name=site_key),
                            ]
                            return ShortcutPlan(steps=steps, site=site_key, query=cand_query_chain, is_play=False)

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

