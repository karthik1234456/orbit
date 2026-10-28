"""Command chain parser for multi-step desktop and browser actions.

Supports chaining commands with candidate boundary separators (e.g. "then", "and then",
"after that", "afterwards", comma, and "and" before command verbs).
Segments are parsed using simulated session state, with support for wait steps
and automatic step flattening.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import os
import re
from typing import Any, Mapping, Optional

from config import settings
from config.sites import SITES, resolve_site
from core.session_state import SessionState, _extract_site_key
from core.shortcuts import (
    ShortcutPlan,
    _levenshtein_distance,
    is_reminder_or_timer_pattern,
    parse_shortcut,
)


COMMAND_VERBS = (
    "open", "launch", "start", "play", "search", "click", "scroll",
    "pause", "resume", "mute", "unmute", "go", "back", "forward",
    "reload", "refresh", "close", "come", "home", "shorts", "next",
    "previous", "wait", "read", "run", "fire", "bring", "pull", "show",
    "look", "restart",
)

NON_VERB_TYPOS: set[str] = {
    "oven", "omen", "oxen", "pen", "one", "opens", "oper",
    "lunch", "laud",
    "smart", "stars", "stare", "stark", "scart", "tart",
    "sear",
    "chose", "clone", "clove", "cloze", "lose",
    "plan", "pray", "plat", "ploy", "plea", "clay", "flay", "slay", "pay", "lay", "plays",
    "stroll", "scrawl", "scrolls",
    "stop", "shop", "step", "store", "car", "safe", "money", "files",
    "book", "cook", "hook", "took", "lock",
}

_EXPLICIT_SEQUENCING_PATTERN = re.compile(
    r",|\b(?:and\s+then|after\s+that|afterwards|then)\b",
    re.IGNORECASE,
)


def has_explicit_sequencing_separator(text: str) -> bool:
    """Return True if text contains an explicit sequencing separator.

    Explicit sequencing separators are: 'then', 'and then', 'after that',
    'afterwards', or a comma.
    """
    if not text or not isinstance(text, str):
        return False
    return bool(_EXPLICIT_SEQUENCING_PATTERN.search(text))


_DISALLOWED_CHARS = set(";&|`$><\\")

_SITE_DISPLAY_NAMES = {
    "youtube": "YouTube",
    "google": "Google",
    "github": "GitHub",
    "wikipedia": "Wikipedia",
    "gmail": "Gmail",
    "maps": "Google Maps",
}

_CANDIDATE_SEP_PATTERN = re.compile(
    r"""
    (?P<comma_phrase>
        ,\s*(?:and\s+then|after\s+that|afterwards|then)\b\s*
    )
    |
    (?P<comma_and_verb>
        ,\s*and\s+(?=(?:please\s+)?(?P<verb1>[a-zA-Z]+))
    )
    |
    (?P<phrase>
        \s+(?:and\s+then|after\s+that|afterwards|then)\b\s*
    )
    |
    (?P<and_verb>
        \s+and\s+(?=(?:please\s+)?(?P<verb2>[a-zA-Z]+))
    )
    |
    (?P<comma>
        \s*,\s*
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_command_verb(word: str) -> bool:
    """Return True if word matches any command verb (exact or edit-distance <= 1)."""
    if not word:
        return False
    w = word.lower()
    if w in COMMAND_VERBS:
        return True
    if w in NON_VERB_TYPOS:
        return False
    return any(_levenshtein_distance(w, v) <= 1 for v in COMMAND_VERBS)


def describe_step(tool: str, args: Optional[dict[str, Any]] = None) -> str:
    """Generate a short human-readable label for a tool step."""
    if args is None:
        args = {}

    t = (tool or "").strip()

    if t == "open_website":
        raw_url = str(args.get("url", "")).strip()
        if "/shorts" in raw_url.lower():
            return "Open shorts"
        site_key, _ = _extract_site_key(raw_url, sites=SITES)
        if site_key:
            return f"Open {site_key.lower()}"
        return f"Open {raw_url or 'website'}"

    if t == "open_application":
        app_name = str(args.get("name", "")).strip()
        return f"Open {app_name.lower()}"

    if t == "close_application":
        app_name = str(args.get("name", "")).strip()
        return f"Close {app_name.lower()}"

    if t == "search_site":
        raw_site = str(args.get("site", "")).strip()
        query = str(args.get("query", "")).strip()
        resolved = resolve_site(raw_site) or raw_site.lower()
        site_disp = _SITE_DISPLAY_NAMES.get(resolved, resolved.title())
        return f"Search {site_disp} for '{query}'"

    if t == "search_web":
        query = str(args.get("query", "")).strip()
        return f"Search Google for '{query}'"

    if t == "browser_click_result":
        if args.get("next_video"):
            return "Next video"
        n = args.get("n", 1)
        return f"Click result {n}"

    if t == "browser_media":
        act = str(args.get("action", "")).strip().lower()
        if act == "pause":
            return "Pause"
        if act == "play":
            return "Play"
        if act == "mute":
            return "Mute"
        if act == "unmute":
            return "Unmute"
        return act.capitalize()

    if t == "browser_scroll":
        direction = str(args.get("direction", "down")).strip().lower()
        return f"Scroll {direction}"

    if t == "wait":
        sec = int(args.get("seconds", 1))
        if sec == 1:
            return "Wait 1 second"
        return f"Wait {sec} seconds"

    if t == "open_workspace_folder":
        return "Open workspace folder"

    if t == "browser_history":
        act = str(args.get("action", "back")).strip().lower()
        if act == "back":
            return "Back"
        if act == "forward":
            return "Forward"
        if act == "reload":
            return "Reload"
        return f"History {act}"

    if t == "browser_read_page":
        return "Read page"

    if t == "browser_close":
        return "Close browser"

    if t == "browser_restart":
        return "Restart the browser"

    if t == "browser_key":
        key_name = str(args.get("name", "")).strip()
        if key_name == "ArrowDown":
            return "Next short"
        if key_name == "ArrowUp":
            return "Previous short"
        return f"Key {key_name}"

    if t == "set_reminder":
        return f"Set reminder for '{args.get('text', '')}'"

    if t == "set_timer":
        return f"Set timer for {args.get('minutes', '')} minutes"

    if t == "list_reminders":
        return "List reminders"

    if t == "cancel_reminder":
        return f"Cancel reminder {args.get('id', '')}"

    if t == "cancel_all_reminders":
        return "Cancel all reminders"

    return t.replace("_", " ").capitalize()


@dataclass
class PlanStep:
    """A single executable step within a ShortcutPlan."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = describe_step(self.tool, self.args)

    @property
    def name(self) -> str:
        """Alias for tool name for backwards compatibility."""
        return self.tool


def _clone_state(state: Optional[Any]) -> SessionState:
    """Create an isolated copy of SessionState for simulated execution."""
    if state is None:
        return SessionState()
    if isinstance(state, SessionState):
        return SessionState(
            _last_site=state._last_site,
            _last_query=state._last_query,
            _last_app=state._last_app,
            _last_folder=state._last_folder,
            _last_url=state._last_url,
            _last_tool=state._last_tool,
            browser_controlled=state.browser_controlled,
            updated_at=state.updated_at,
            clock=state.clock,
            ttl=state.ttl,
        )
    import copy
    try:
        return copy.copy(state)
    except Exception:
        return SessionState()


def _clean_segment(seg: str) -> str:
    """Clean a segmented slice: strip whitespace, trailing punctuation, leading/trailing fillers."""
    s = seg.strip()
    s = re.sub(r"[.!?,:\s]+$", "", s).strip()
    while True:
        prev = s
        s = re.sub(
            r"^(?:hey|hi|ok|okay|now|just|please|can\s+you|could\s+you|can\s+u|would\s+you|and)\b[,:\s]*",
            "",
            s,
            flags=re.IGNORECASE,
        ).strip()
        s = re.sub(
            r"[,:\s]+(?:please|for\s+me|now)[.!?,:\s]*$",
            "",
            s,
            flags=re.IGNORECASE,
        ).strip()
        s = re.sub(r"[.!?,:\s]+$", "", s).strip()
        if s == prev:
            break
    return s


def _find_candidate_boundaries(text: str) -> list[tuple[int, int]]:
    """Scan text for candidate boundary separator spans (start, end)."""
    candidates: list[tuple[int, int]] = []
    for match in _CANDIDATE_SEP_PATTERN.finditer(text):
        m_dict = match.groupdict()
        if m_dict.get("and_verb") or m_dict.get("comma_and_verb"):
            # Check if lookahead word is a command verb
            next_word = m_dict.get("verb1") or m_dict.get("verb2")
            if not _is_command_verb(next_word or ""):
                continue
        candidates.append((match.start(), match.end()))
    return candidates


def _is_chains_enabled() -> bool:
    """Return whether chain execution is enabled via settings or env."""
    val = getattr(settings, "CHAINS_ENABLED", None)
    if val is not None and isinstance(val, bool):
        env_val = os.getenv("CHAINS_ENABLED")
        if env_val is not None and env_val.strip().lower() in ("0", "false", "no", "off"):
            return False
        return val
    env_val = os.getenv("CHAINS_ENABLED")
    if env_val is not None:
        return env_val.strip().lower() not in ("0", "false", "no", "off")
    return True


def _try_segmentation(
    text: str,
    boundary_subset: tuple[tuple[int, int], ...],
    allowed_apps: Any,
    sites: Any,
    state: Any,
    browser_enabled: bool,
) -> Optional[ShortcutPlan]:
    """Test a specific boundary subset. Returns a ShortcutPlan if valid, else None."""
    segments: list[str] = []
    prev_end = 0
    for sep_start, sep_end in boundary_subset:
        segments.append(text[prev_end:sep_start])
        prev_end = sep_end
    segments.append(text[prev_end:])

    # Limits: at most 6 segments, at least 2 segments
    if len(segments) < 2 or len(segments) > 6:
        return None

    sim_state = _clone_state(state)
    all_steps: list[PlanStep] = []

    for seg in segments:
        cleaned_seg = _clean_segment(seg)
        if not cleaned_seg:
            return None

        # Check wait step: "wait <N> second(s)|sec(s)|s" with N from 1 to 30
        wait_match = re.match(
            r"^wait\s+(\d+)\s*(?:seconds?|secs?|s)$",
            cleaned_seg,
            re.IGNORECASE,
        )
        if wait_match:
            sec = int(wait_match.group(1))
            if not (1 <= sec <= 30):
                return None
            step = PlanStep(tool="wait", args={"seconds": sec})
            all_steps.append(step)
            if hasattr(sim_state, "update_from_tool"):
                sim_state.update_from_tool("wait", {"seconds": sec}, status="success", sites=sites)
            continue

        if cleaned_seg.lower() == "wait":
            return None

        # A segment matching a reminder pattern makes the whole chain return None
        if is_reminder_or_timer_pattern(cleaned_seg):
            return None

        # Parse with parse_shortcut using simulated state
        res = parse_shortcut(
            cleaned_seg,
            allowed_apps=allowed_apps,
            sites=sites,
            state=sim_state,
            browser_enabled=browser_enabled,
        )
        if res is None:
            return None

        # Reminder tool calls are never allowed in chains
        if hasattr(res, "name") and res.name in (
            "set_reminder", "set_timer", "list_reminders", "cancel_reminder", "cancel_all_reminders"
        ):
            return None

        if isinstance(res, ShortcutPlan):
            for sub in res.steps:
                tool_name = getattr(sub, "tool", getattr(sub, "name", ""))
                tool_args = getattr(sub, "args", {})
                step = PlanStep(tool=tool_name, args=tool_args)
                all_steps.append(step)
                if hasattr(sim_state, "update_from_tool"):
                    sim_state.update_from_tool(tool_name, tool_args, status="success", sites=sites)
        else:
            step = PlanStep(tool=res.name, args=res.args)
            all_steps.append(step)
            if hasattr(sim_state, "update_from_tool"):
                sim_state.update_from_tool(res.name, res.args, status="success", sites=sites)

    # Limits: at most 10 steps in total
    if len(all_steps) > 10:
        return None

    # Limits: wait steps summing to at most 60 seconds
    total_wait_s = sum(
        s.args.get("seconds", 0) for s in all_steps if s.tool == "wait"
    )
    if total_wait_s > 60:
        return None

    return ShortcutPlan(
        steps=all_steps,
        source="chain",
        labels=[s.label for s in all_steps],
    )


def parse_chain(
    text: str,
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
    state: Optional[Any] = None,
    browser_enabled: bool = False,
) -> Optional[ShortcutPlan]:
    """Parse user text into a multi-step ShortcutPlan with source='chain'.

    Returns None if text does not form a valid chain of at least 2 segments,
    exceeds limits, or chains are disabled.
    """
    if not _is_chains_enabled():
        return None

    if not text or not isinstance(text, str):
        return None

    # Reject text longer than 300 characters
    if len(text) > 300:
        return None

    # Reject newlines
    if "\n" in text or "\r" in text:
        return None

    # Reject URLs
    if "://" in text:
        return None

    # Reject disallowed shell characters
    if any(ch in _DISALLOWED_CHARS for ch in text):
        return None

    raw = text.strip()
    if not raw:
        return None

    # Reminder and timer messages are NEVER split by the chain parser
    if is_reminder_or_timer_pattern(raw):
        return None

    if allowed_apps is None:
        from config.apps import ALLOWED_APPS
        allowed_apps = ALLOWED_APPS

    if sites is None:
        from config.sites import SITES
        sites = SITES

    candidates = _find_candidate_boundaries(raw)
    if not candidates:
        return None

    # Limits: at most 7 candidate boundaries
    if len(candidates) > 7:
        candidates = candidates[:7]

    # Try subsets of candidate boundaries, preferring the most segments (highest m first);
    # ties go to earlier boundaries (preserved by itertools.combinations).
    # Segments count = m + 1. Limits: at most 6 segments, so m <= 5.
    for m in range(min(len(candidates), 5), 0, -1):
        for boundary_subset in itertools.combinations(candidates, m):
            plan = _try_segmentation(
                raw,
                boundary_subset,
                allowed_apps=allowed_apps,
                sites=sites,
                state=state,
                browser_enabled=browser_enabled,
            )
            if plan is not None:
                return plan

    return None


def sanitize_page_title(text: str) -> str:
    """Sanitize page-derived title: single line, at most 100 characters."""
    if not text:
        return ""
    # Single line: replace newlines/tabs/consecutive whitespace with single space
    clean = " ".join(str(text).split())
    # Max 100 characters
    return clean[:100].strip()


def extract_title_from_click_result(result: str, n: int) -> str:
    """Extract and sanitize title from browser_click_result output."""
    if not result:
        return ""
    res = str(result).strip()
    prefix = f"Opened result {n}:"
    if res.lower().startswith(prefix.lower()):
        title = res[len(prefix):].strip()
    elif ":" in res:
        title = res.split(":", 1)[1].strip()
    else:
        title = res
    return sanitize_page_title(title)


def format_step_text(tool: str, args: dict, result: Any = "", redact: bool = False) -> str:
    """Format past-tense description of an executed step from structured parts."""
    t = tool.lower() if tool else ""

    if t == "search_site":
        site = args.get("site", "")
        site_display = _SITE_DISPLAY_NAMES.get(site.lower(), site.title() if site else "Site")
        query = args.get("query", "")
        return f"Searched {site_display} for '{query}'."

    elif t == "browser_click_result":
        n = args.get("n", 1)
        if not redact and result:
            title = extract_title_from_click_result(str(result), n)
            if title:
                return f"Opened result {n}: {title.rstrip('.')}."
        return f"Opened result {n}."

    elif t == "browser_read_page":
        if not redact and result:
            first_line = str(result).strip().splitlines()[0] if str(result).strip() else ""
            if first_line.lower().startswith("page:"):
                first_line = first_line[5:].strip()
            elif first_line.lower().startswith("title:"):
                first_line = first_line[6:].strip()
            clean = sanitize_page_title(first_line)
            if clean:
                return f"Read the page: {clean.rstrip('.')}."
        return "Read the page."

    elif t in ("open_app", "launch_app"):
        app = args.get("app", "")
        return f"Opened {app}."

    elif t == "close_app":
        app = args.get("app", "")
        return f"Closed {app}."

    elif t in ("navigate", "open_url"):
        url = args.get("url", "")
        sk = _extract_site_key(url)
        target = _SITE_DISPLAY_NAMES.get(sk, sk) if sk else "browser"
        return f"Opened {target.lower()}."

    elif t == "browser_media":
        act = str(args.get("action", "")).lower()
        if act == "pause":
            return "Paused."
        elif act in ("play", "resume"):
            return "Playing."
        elif act in ("mute", "unmute", "toggle"):
            return f"Media {act}."
        return "Media updated."

    elif t == "browser_scroll":
        direction = str(args.get("direction", "down")).lower()
        return f"Scrolled {direction}."

    elif t == "browser_history":
        act = str(args.get("action", "back")).lower()
        return f"Went {act}."

    elif t in ("close_browser", "browser_close"):
        return "Closed browser."

    elif t == "browser_restart":
        return "Restarted ORBIT's browser."

    elif t == "wait":
        secs = args.get("seconds", 1)
        try:
            val = float(secs)
            s_val = int(val) if val.is_integer() else val
        except (ValueError, TypeError):
            s_val = secs
        return f"Waited {s_val} second{'s' if s_val != 1 else ''}."

    elif t == "set_reminder":
        txt = args.get("text", "")
        return f"Set reminder for '{txt}'."

    elif t == "set_timer":
        mins = args.get("minutes", "")
        return f"Set timer for {mins} minutes."

    elif t == "list_reminders":
        return "Listed reminders."

    elif t == "cancel_reminder":
        cid = args.get("id", "")
        return f"Cancelled reminder {cid}."

    elif t == "cancel_all_reminders":
        return "Cancelled all reminders."

    desc = describe_step(tool, args)
    if desc.endswith("."):
        return desc
    return f"{desc}."


def redact_page_text(tool: str, args: dict, result: Any = "") -> str:
    """Helper returning the step description with all page-derived text redacted (for agent.history)."""
    return format_step_text(tool, args, result=result, redact=True)


def format_plan_reply(
    executed_steps: list[tuple[str, dict, Any]],
    stopped_at: Optional[int] = None,
    stop_reason: str = "",
    redact: bool = False,
) -> str:
    """Build structured reply for plan execution (one line per step + Done/Stopped)."""
    lines = []
    for idx, (t, a, res) in enumerate(executed_steps, 1):
        step_text = format_step_text(t, a, result=res, redact=redact)
        lines.append(f"{idx}. {step_text}")

    if stopped_at is not None:
        clean_reason = stop_reason.rstrip(".") if stop_reason else "execution failed"
        lines.append(f"Stopped at step {stopped_at}: {clean_reason}.")
    else:
        lines.append("Done")

    return "\n".join(lines)


def format_cancel_reply(
    completed_steps: list[tuple[str, dict, Any]],
    total_steps: int,
    redact: bool = False,
) -> str:
    """Format cancellation reply: 'Stopped after step k of n.' plus completed steps."""
    k = len(completed_steps)
    lines = [f"Stopped after step {k} of {total_steps}."]
    for idx, (t, a, res) in enumerate(completed_steps, 1):
        step_text = format_step_text(t, a, result=res, redact=redact)
        lines.append(f"{idx}. {step_text}")
    return "\n".join(lines)
