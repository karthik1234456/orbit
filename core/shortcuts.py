"""Command shortcut parser for deterministic desktop actions without LLM calls.

Matches strict whitelisted phrase patterns for opening applications, opening websites,
opening workspace folders, and performing site-specific searches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Optional, Union

_DISALLOWED_CHARS = set(";&|`$><")

BARE_BROWSER_VERBS = ("back", "forward", "pause", "play", "mute", "unmute", "scroll", "home")

CONJUNCTIONS_SEQUENCING: set[str] = {
    "and", "then", "also", "after", "afterwards", "now",
}

COMMAND_VERBS_SET: set[str] = {
    "open", "launch", "start", "play", "search", "click", "scroll",
    "pause", "resume", "mute", "unmute", "go", "back", "forward",
    "reload", "refresh", "close", "come", "read", "wait", "next",
    "previous", "home", "shorts", "run",
}

TYPO_VERBS: set[str] = {
    "open", "launch", "start", "search", "close", "play", "scroll",
}

NON_VERB_TYPOS: set[str] = {
    "oven", "omen", "oxen", "pen", "one", "opens", "oper",
    "lunch", "laud",
    "smart", "stars", "stare", "stark", "scart", "tart",
    "sear",
    "chose", "clone", "clove", "cloze", "lose",
    "plan", "pray", "plat", "ploy", "plea", "clay", "flay", "slay", "pay", "lay", "plays",
    "stroll", "scrawl", "scrolls",
    "stop", "shop", "step", "store", "car", "safe", "money", "files",
}


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Damerau-Levenshtein distance between two strings."""
    if s1 == s2:
        return 0
    if abs(len(s1) - len(s2)) > 1:
        return 2
    m, n = len(s1), len(s2)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,        # deletion
                dp[i][j - 1] + 1,        # insertion
                dp[i - 1][j - 1] + cost  # substitution
            )
            if i > 1 and j > 1 and s1[i - 1] == s2[j - 2] and s1[i - 2] == s2[j - 1]:
                dp[i][j] = min(dp[i][j], dp[i - 2][j - 2] + 1)
    return dp[m][n]


def _match_verb(token: str, target: str) -> bool:
    """Match token against a command verb with edit-distance <= 1."""
    if not token or not target:
        return False
    tok = token.lower()
    tgt = target.lower()
    if tok == tgt:
        return True
    if tok in NON_VERB_TYPOS:
        return False
    return _levenshtein_distance(tok, tgt) <= 1


def _match_typo_verb(token: str, target: str) -> bool:
    """Match token against a target verb with Levenshtein edit distance <= 1.

    Allowed only for verbs: open, launch, start, search, close, play, scroll.
    Never turns a different real word into a verb (e.g. oven, stop, plan).
    """
    if not token or not target:
        return False
    tok = token.lower()
    tgt = target.lower()
    if tok == tgt:
        return True
    if tgt not in TYPO_VERBS:
        return False
    if tok in NON_VERB_TYPOS:
        return False
    return _levenshtein_distance(tok, tgt) <= 1


def _is_invalid_query_boundary_word(word: str) -> bool:
    """Check if word cannot appear at the boundary of a search or ordinal query."""
    if not word:
        return True
    w = word.lower()
    if w in CONJUNCTIONS_SEQUENCING:
        return True
    if w in COMMAND_VERBS_SET:
        return True
    if any(_match_typo_verb(w, v) for v in TYPO_VERBS):
        return True
    return False


def is_valid_search_query(query: str, require_clean_tail: bool = False) -> bool:
    """Check if query is a valid search shortcut query.

    Must have 1 to 8 words and must not start with a conjunction/sequencing word or command verb.
    If require_clean_tail is True, must not end with a conjunction/sequencing word or command verb.
    """
    if not query:
        return False
    words = query.split()
    if not (1 <= len(words) <= 8):
        return False
    if _is_invalid_query_boundary_word(words[0]):
        return False
    if require_clean_tail and _is_invalid_query_boundary_word(words[-1]):
        return False
    return True


def is_bare_browser_verb(text: str) -> bool:
    """Return True if text is a single bare browser verb (with edit-distance <= 1)."""
    if not text or not isinstance(text, str):
        return False
    if "\n" in text or "\r" in text:
        return False
    if any(ch in _DISALLOWED_CHARS for ch in text):
        return False
    cleaned = re.sub(r"[.!?,:\s]+$", "", text.strip()).strip()
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
    words = cleaned.split()
    if len(words) != 1:
        return False
    w = words[0].lower()
    return any(_match_verb(w, v) for v in BARE_BROWSER_VERBS)


@dataclass
class ToolCallSpec:
    """Specification for a shortcut-invoked tool execution."""

    name: str
    args: dict[str, Any]
    target_name: str = ""
    id: Optional[str] = None

    @property
    def tool(self) -> str:
        return self.name

    @property
    def label(self) -> str:
        from core.chains import describe_step
        return describe_step(self.name, self.args)


@dataclass
class ShortcutPlan:
    """Specification for a multi-step shortcut execution chain."""

    steps: list[Any]
    site: str = ""
    query: str = ""
    is_play: bool = False
    ordinal: int = 1
    style: str = ""
    source: str = "builtin"
    labels: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.labels:
            from core.chains import describe_step
            self.labels = [
                step.label
                if hasattr(step, "label") and step.label
                else describe_step(
                    getattr(step, "tool", getattr(step, "name", "")),
                    getattr(step, "args", {}),
                )
                for step in self.steps
            ]


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

    # Strip optional leading and trailing fillers (case-insensitive)
    while True:
        prev = cleaned
        cleaned = re.sub(
            r"^(?:hey|hi|ok|okay|now|just|please|can\s+you|could\s+you|can\s+u|would\s+you)\b[,:\s]*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"[,:\s]+(?:please|for\s+me|now)[.!?,:\s]*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(r"[.!?,:\s]+$", "", cleaned).strip()
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

        words = cleaned.split()
        lower_cleaned = cleaned.lower()

        # Pattern C: History controls
        # "come back", "go back", "back", "previous page" -> browser_history back
        if (
            (len(words) == 1 and _match_verb(words[0], "back"))
            or (len(words) == 2 and words[0].lower() in ("come", "go") and _match_verb(words[1], "back"))
            or lower_cleaned == "previous page"
        ):
            return ToolCallSpec(
                name="browser_history",
                args={"action": "back"},
                target_name="back",
            )

        # "forward" -> history forward
        if (
            (len(words) == 1 and _match_verb(words[0], "forward"))
            or (len(words) == 2 and words[0].lower() == "go" and _match_verb(words[1], "forward"))
        ):
            return ToolCallSpec(
                name="browser_history",
                args={"action": "forward"},
                target_name="forward",
            )

        if len(words) == 1 and (_match_verb(words[0], "reload") or _match_verb(words[0], "refresh")):
            return ToolCallSpec(
                name="browser_history",
                args={"action": "reload"},
                target_name="reload",
            )

        # Home navigation:
        # "main menu", "home", "go home", "come to main menu", "youtube home"
        if (
            (len(words) == 1 and _match_verb(words[0], "home"))
            or (len(words) == 2 and words[0].lower() == "go" and _match_verb(words[1], "home"))
            or lower_cleaned in ("main menu", "come to main menu")
            or (len(words) == 2 and words[0].lower() == "youtube" and _match_verb(words[1], "home"))
        ):
            if "youtube" in lower_cleaned:
                site_key = "youtube"
            elif state and getattr(state, "last_site", None):
                site_key = str(state.last_site).lower()
            else:
                site_key = "youtube"

            site_cfg = sites.get(site_key, {})
            home_url = site_cfg.get("url") if isinstance(site_cfg, dict) else (site_cfg or "https://www.youtube.com")
            if not home_url:
                home_url = f"https://www.{site_key}.com"
            return ToolCallSpec(
                name="open_website",
                args={"url": home_url},
                target_name=site_key,
            )

        # Next video:
        # "next video", "play the next video", "next" on youtube_watch page
        if (
            lower_cleaned in ("next", "next video")
            or (len(words) == 4 and _match_verb(words[0], "play") and words[1].lower() == "the" and words[2].lower() == "next" and words[3].lower() == "video")
        ):
            return ToolCallSpec(
                name="browser_click_result",
                args={"n": 1, "next_video": True},
                target_name="next video",
            )

        # Shorts navigation:
        # "open shorts", "shorts", "open shorts section", "go to shorts"
        if (
            lower_cleaned in ("shorts", "go to shorts")
            or (len(words) == 2 and _match_verb(words[0], "open") and words[1].lower() == "shorts")
            or (len(words) == 3 and _match_verb(words[0], "open") and words[1].lower() == "shorts" and words[2].lower() == "section")
        ):
            return ToolCallSpec(
                name="open_website",
                args={"url": "https://www.youtube.com/shorts"},
                target_name="shorts",
            )

        # Shorts swiping:
        # "next short", "swipe up" -> ArrowDown; "previous short" -> ArrowUp
        if lower_cleaned == "next short" or (len(words) == 2 and _match_verb(words[0], "swipe") and words[1].lower() == "up"):
            return ToolCallSpec(
                name="browser_key",
                args={"name": "ArrowDown"},
                target_name="next short",
            )

        if lower_cleaned == "previous short":
            return ToolCallSpec(
                name="browser_key",
                args={"name": "ArrowUp"},
                target_name="previous short",
            )

        # Pattern B: Media controls
        # "pause", "pause the video", "pause it"
        if (
            (len(words) == 1 and _match_verb(words[0], "pause"))
            or lower_cleaned in ("pause the video", "pause it")
        ):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "pause"},
                target_name="pause",
            )

        # "play", "resume", "play the video", "play it", "play that"
        # with typo tolerance on play (e.g. paly)
        if (
            (len(words) == 1 and (_match_typo_verb(words[0], "play") or words[0].lower() == "resume"))
            or lower_cleaned in ("play the video", "play it", "play that")
            or (len(words) == 2 and _match_typo_verb(words[0], "play") and words[1].lower() in ("it", "that"))
            or (len(words) == 3 and _match_typo_verb(words[0], "play") and words[1].lower() == "the" and words[2].lower() == "video")
        ):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "play"},
                target_name="play",
            )

        # "mute", "mute it"
        if (
            (len(words) == 1 and _match_verb(words[0], "mute"))
            or lower_cleaned == "mute it"
        ):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "mute"},
                target_name="mute",
            )

        # "unmute", "unmute it"
        if (
            (len(words) == 1 and _match_verb(words[0], "unmute"))
            or lower_cleaned == "unmute it"
        ):
            return ToolCallSpec(
                name="browser_media",
                args={"action": "unmute"},
                target_name="unmute",
            )

        # Pattern A: Scrolling
        # bare "scroll" (default down 1), "down|up", "scroll down|up"
        if len(words) == 1 and _match_typo_verb(words[0], "scroll"):
            return ToolCallSpec(
                name="browser_scroll",
                args={"direction": "down", "screens": 1},
                target_name="scroll down",
            )

        if re.match(r"^(?:down|up)$", cleaned, re.IGNORECASE):
            d = cleaned.lower()
            return ToolCallSpec(
                name="browser_scroll",
                args={"direction": d, "screens": 1},
                target_name=f"scroll {d}",
            )

        if len(words) == 2 and _match_typo_verb(words[0], "scroll") and words[1].lower() in ("down", "up"):
            d = words[1].lower()
            return ToolCallSpec(
                name="browser_scroll",
                args={"direction": d, "screens": 1},
                target_name=f"scroll {d}",
            )

        scroll_to_match = re.match(r"^([a-zA-Z]+)\s+to\s+(?:the\s+)?(top|bottom)$", cleaned, re.IGNORECASE)
        if scroll_to_match and _match_typo_verb(scroll_to_match.group(1), "scroll"):
            d = scroll_to_match.group(2).lower()
            return ToolCallSpec(
                name="browser_scroll",
                args={"direction": d, "screens": 1},
                target_name=f"scroll to {d}",
            )

        scroll_n_match = re.match(r"^([a-zA-Z]+)\s+(down|up)\s+(\w+)\s+(?:screens?|pages?)$", cleaned, re.IGNORECASE)
        if scroll_n_match and _match_typo_verb(scroll_n_match.group(1), "scroll"):
            d = scroll_n_match.group(2).lower()
            n_str = scroll_n_match.group(3).lower()
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

            if not is_valid_search_query(query_candidate, require_clean_tail=True):
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
        # "read page", "read the page", "read page content", "read the page content",
        # "what's on the page", "what is on this page"
        if re.match(
            r"^(?:read\s+(?:the\s+)?page(?:\s+content)?|what(?:'?s|\s+is)\s+on\s+(?:the|this)\s+page)$",
            cleaned,
            re.IGNORECASE,
        ):
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
                    if is_valid_search_query(cand_query_chain):
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

    # Follow-up Pattern (c) & (d): "close (it|that|this)" or "close <app>"
    # Typo tolerance on "close"
    close_match = re.match(r"^([a-zA-Z]+)\s+(.+)$", cleaned, re.IGNORECASE)
    if close_match and _match_typo_verb(close_match.group(1), "close"):
        arg_cand = close_match.group(2).strip().lower()
        if arg_cand in ("it", "that", "this"):
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
        if arg_cand in clean_apps_map:
            app_canon = clean_apps_map[arg_cand]
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
                if last_q and is_valid_search_query(last_q):
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
                if last_q and is_valid_search_query(last_q):
                    return ToolCallSpec(
                        name="search_site",
                        args={"site": site_key, "query": last_q},
                        target_name=site_key,
                    )
        return None

    # Follow-up Pattern (e): "search for <query>" when state has last_site
    search_for_state_match = re.match(r"^([a-zA-Z]+)\s+for\s+(.+)$", cleaned, re.IGNORECASE)
    if search_for_state_match and _match_typo_verb(search_for_state_match.group(1), "search"):
        cand_query = search_for_state_match.group(2).strip()
        if is_valid_search_query(cand_query):
            if state and getattr(state, "last_site", None):
                last_site_key = str(state.last_site).lower()
                resolved = resolve_site(last_site_key)
                site_key = (resolved or last_site_key).lower()
                if site_key in clean_sites_map:
                    site_entry = sites.get(site_key)
                    search_tmpl = None
                    if isinstance(site_entry, dict):
                        search_tmpl = site_entry.get("search") or site_entry.get("search_template")
                    if search_tmpl:
                        return ToolCallSpec(
                            name="search_site",
                            args={"site": site_key, "query": cand_query},
                            target_name=site_key,
                        )
        return None

    # 2(c): "go to <site>" and "take me to <site>" map to open_website for a known site key or alias (not for apps)
    go_to_match = re.match(
        r"^(?:go\s+to|take\s+me\s+to)\s+(.+?)(?:\s+(?:website|site))?$",
        cleaned,
        re.IGNORECASE,
    )
    if go_to_match:
        target_raw = go_to_match.group(1).strip()
        resolved = resolve_site(target_raw)
        site_key = (resolved or target_raw).lower()
        if site_key in clean_sites_map:
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
        return None

    # Pattern 2: Open / Launch / Start / Run application or website
    # Multi-word open verbs: "fire up", "bring up", "pull up", "show me" (sites only)
    multi_open_match = re.match(
        r"^(fire\s+up|bring\s+up|pull\s+up|show\s+me)\s+(.+?)(?:\s+(app|website|site))?$",
        cleaned,
        re.IGNORECASE,
    )
    if multi_open_match:
        verb_phrase = multi_open_match.group(1).lower()
        cand_name = multi_open_match.group(2).strip().lower()

        # "show me" is sites only
        if verb_phrase == "show me":
            resolved_site = resolve_site(cand_name)
            site_key = (resolved_site or cand_name).lower()
            if site_key in clean_sites_map:
                site_entry = sites[site_key]
                home_url = site_entry.get("url") if isinstance(site_entry, dict) else str(site_entry)
                return ToolCallSpec(
                    name="open_website",
                    args={"url": home_url},
                    target_name=site_key,
                )
            return None

        # "fire up", "bring up", "pull up": apps first, then sites
        if cand_name in clean_apps_map:
            app_key = clean_apps_map[cand_name]
            return ToolCallSpec(
                name="open_application",
                args={"name": app_key},
                target_name=app_key,
            )
        resolved_site = resolve_site(cand_name)
        site_key = (resolved_site or cand_name).lower()
        if site_key in clean_sites_map:
            site_entry = sites[site_key]
            home_url = site_entry.get("url") if isinstance(site_entry, dict) else str(site_entry)
            return ToolCallSpec(
                name="open_website",
                args={"url": home_url},
                target_name=site_key,
            )
        return None

    # Single-word open verbs: open, launch, start, run (apps only)
    # With edit-distance-1 typo tolerance for open, launch, start
    single_open_match = re.match(
        r"^([a-zA-Z]+)\s+(.+?)(?:\s+(app|website|site))?$",
        cleaned,
        re.IGNORECASE,
    )
    if single_open_match:
        cand_verb = single_open_match.group(1).lower()
        cand_name = single_open_match.group(2).strip().lower()

        is_open = _match_typo_verb(cand_verb, "open")
        is_launch = _match_typo_verb(cand_verb, "launch")
        is_start = _match_typo_verb(cand_verb, "start")
        is_run = (cand_verb == "run")

        if is_run:
            # "run" is apps only
            if cand_name in clean_apps_map:
                app_key = clean_apps_map[cand_name]
                return ToolCallSpec(
                    name="open_application",
                    args={"name": app_key},
                    target_name=app_key,
                )
            return None

        if is_open or is_launch or is_start:
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
            site_key = (resolved_site or cand_name).lower()
            if site_key in clean_sites_map:
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
            return None

    # Pattern 3: Site search
    # "search <site> for <query>" and "<query> on <site>"
    # ONLY when <site> is a SITES key with a search template and the query is 1-8 words
    search_for_match = re.match(r"^([a-zA-Z]+)\s+(.+?)\s+for\s+(.+)$", cleaned, re.IGNORECASE)
    query_on_match = re.match(r"^(.+)\s+on\s+(.+)$", cleaned, re.IGNORECASE)

    cand_site: Optional[str] = None
    cand_query: Optional[str] = None

    if search_for_match and _match_typo_verb(search_for_match.group(1), "search"):
        cand_site = search_for_match.group(2).strip()
        cand_query = search_for_match.group(3).strip()
    elif query_on_match:
        q = query_on_match.group(1).strip()
        q_lower = q.lower()
        if q_lower == "search" or q_lower.startswith("search ") or q_lower.startswith("open "):
            return None
        cand_query = q
        cand_site = query_on_match.group(2).strip()

    if cand_site and cand_query:
        if not is_valid_search_query(cand_query):
            return None

        resolved_site = resolve_site(cand_site)
        site_lower = resolved_site if resolved_site else cand_site.lower()
        if site_lower in clean_sites_map:
            site_key = clean_sites_map[site_lower]
            site_entry = sites[site_key]
            search_tmpl = None
            if isinstance(site_entry, dict):
                search_tmpl = site_entry.get("search") or site_entry.get("search_template")

            if search_tmpl:
                return ToolCallSpec(
                    name="search_site",
                    args={"site": site_key, "query": cand_query},
                    target_name=site_key,
                )

    return None

