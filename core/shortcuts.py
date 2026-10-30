"""Command shortcut parser for deterministic desktop actions without LLM calls.

Matches strict whitelisted phrase patterns for opening applications, opening websites,
opening workspace folders, and performing site-specific searches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
import re
from typing import Any, Callable, Mapping, Optional, Sequence, Union

_DISALLOWED_CHARS = set(";&|`$><")

BARE_BROWSER_VERBS = ("back", "forward", "pause", "play", "mute", "unmute", "scroll", "home")

CONJUNCTIONS_SEQUENCING: set[str] = {
    "and", "then", "also", "after", "afterwards", "now", "or", "but", "plus", "instead",
}

COMMAND_VERBS_SET: set[str] = {
    "open", "launch", "start", "play", "search", "click", "scroll",
    "pause", "resume", "mute", "unmute", "go", "back", "forward",
    "reload", "refresh", "close", "come", "read", "wait", "next",
    "previous", "home", "shorts", "run", "look", "restart",
}

ORDINAL_WORDS_SET: set[str] = {
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th", "top",
}

FORBIDDEN_QUERY_SUBSTRINGS: tuple[str, ...] = (
    "browser", "tab", "window", "link", "links", "website", "websites", "result", "results", "the same", "default",
)

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
    "book", "cook", "hook", "took", "lock",
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


def is_valid_ordinal_query(query: str) -> bool:
    """Validate query after an ordinal expression per Requirement 7.

    Query rejected (None) when:
    - starting with conjunction/sequencing word (and, then, also, after, afterwards, now, or, but, plus, instead),
    - containing any of: ordinal words, "browser", "tab", "window", "link", "links", "website", "websites", "result", "results", "the same", "default",
    - or ending with a command verb.
    """
    if not query or not isinstance(query, str):
        return False
    words = query.split()
    if not (1 <= len(words) <= 8):
        return False

    first_word = words[0].lower()
    if (
        first_word in CONJUNCTIONS_SEQUENCING
        or first_word in COMMAND_VERBS_SET
        or any(_match_typo_verb(first_word, v) for v in TYPO_VERBS)
    ):
        return False

    last_word = words[-1].lower()
    if (
        last_word in COMMAND_VERBS_SET
        or any(_match_typo_verb(last_word, v) for v in TYPO_VERBS)
    ):
        return False

    lower_q = query.lower()
    for forbidden in FORBIDDEN_QUERY_SUBSTRINGS:
        if re.search(rf"\b{re.escape(forbidden)}\b", lower_q):
            return False

    for w in words:
        w_clean = re.sub(r"[^\w]", "", w.lower())
        if w_clean in ORDINAL_WORDS_SET:
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
class ShortcutReply:
    """Fixed plain-text reply for a shortcut that executes no tool."""

    reply: str

    @property
    def text(self) -> str:
        return self.reply

    def __str__(self) -> str:
        return self.reply

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.reply == other
        if isinstance(other, ShortcutReply):
            return self.reply == other.reply
        return False


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


@dataclass
class RoutineAction:
    """Action requested via routine shortcut."""

    action: str  # "create", "save_last_plan", "run", "list", "show", "delete"
    name: str = ""
    display_name: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)


_FORBIDDEN_REMINDER_CHARS = set(";&|`$><")


def is_reminder_or_timer_pattern(text: str) -> bool:
    """Return True if text begins with a reminder or timer pattern."""
    if not text or not isinstance(text, str):
        return False
    cleaned = text.strip()
    while True:
        prev = cleaned
        cleaned = re.sub(
            r"^(?:hey|hi|ok|okay|now|just|please|can\s+you|could\s+you|can\s+u|would\s+you)\b[,:\s]*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned == prev:
            break

    pat = re.compile(
        r"^(?:"
        r"remind\s+me\b|"
        r"set\s+a\s+reminder\b|"
        r"set\s+a\s+timer\b|"
        r"set\s+a\s+.+?\s+timer\b|"
        r"start\s+a\s+.+?\s+timer\b|"
        r"timer\b|"
        r"(?:list|show)\s+(?:my\s+)?reminders\b|"
        r"what\s+(?:reminders|timers)\s+do\s+i\s+have\b|"
        r"cancel\s+(?:(?:all\s+)?(?:reminders|timers)|reminder|timer)\b"
        r")",
        re.IGNORECASE,
    )
    return bool(pat.match(cleaned))


def parse_reminder_shortcut(
    cleaned: str,
    clock: Optional[Callable[[], datetime]] = None,
) -> Optional[ToolCallSpec]:
    """Parse cleaned user text into reminder ToolCallSpec if matching strict reminder/timer patterns."""
    from reminders.store import sanitize_reminder_text
    from reminders.timeparse import parse_clock, parse_relative

    ref_now = clock() if clock else datetime.now().astimezone()

    # 1. "remind me in <n> <unit> to <text>"
    m1 = re.match(r"^remind\s+me\s+in\s+(.+?)\s+to\s+(.+)$", cleaned, re.IGNORECASE)
    if m1:
        dur_str = m1.group(1).strip()
        raw_text = m1.group(2).strip()
        if any(ch in _FORBIDDEN_REMINDER_CHARS for ch in raw_text):
            return None
        if re.search(r"://|www\.|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?", raw_text, re.IGNORECASE):
            return None
        td = parse_relative(dur_str)
        if td is not None:
            total_sec = td.total_seconds()
            if 5.0 <= total_sec <= 7 * 86400:
                clean_text = sanitize_reminder_text(raw_text)
                if 1 <= len(clean_text) <= 100:
                    minutes = total_sec / 60.0
                    return ToolCallSpec(
                        name="set_reminder",
                        args={"text": clean_text, "minutes": round(minutes, 4)},
                        target_name=clean_text,
                    )
        return None

    # 2. "remind me to <text> in <n> <unit>"
    m2 = re.match(r"^remind\s+me\s+to\s+(.+)$", cleaned, re.IGNORECASE)
    if m2:
        tail = m2.group(1).strip()
        if any(ch in _FORBIDDEN_REMINDER_CHARS for ch in tail):
            return None
        if re.search(r"://|www\.|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?", tail, re.IGNORECASE):
            return None
        parts = re.split(r"\s+in\s+", tail, flags=re.IGNORECASE)
        if len(parts) >= 2:
            for idx in range(len(parts) - 1, 0, -1):
                cand_dur = " in ".join(parts[idx:])
                td = parse_relative(cand_dur)
                if td is not None:
                    total_sec = td.total_seconds()
                    if 5.0 <= total_sec <= 7 * 86400:
                        cand_text = " in ".join(parts[:idx]).strip()
                        clean_text = sanitize_reminder_text(cand_text)
                        if 1 <= len(clean_text) <= 100:
                            minutes = total_sec / 60.0
                            return ToolCallSpec(
                                name="set_reminder",
                                args={"text": clean_text, "minutes": round(minutes, 4)},
                                target_name=clean_text,
                            )
        return None

    # 3. "remind me at <clock> to <text>"
    m3 = re.match(r"^remind\s+me\s+at\s+(.+?)\s+to\s+(.+)$", cleaned, re.IGNORECASE)
    if m3:
        clock_str = m3.group(1).strip()
        raw_text = m3.group(2).strip()
        if any(ch in _FORBIDDEN_REMINDER_CHARS for ch in raw_text):
            return None
        if re.search(r"://|www\.|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?", raw_text, re.IGNORECASE):
            return None
        target_dt = parse_clock(clock_str, now=ref_now)
        if target_dt is not None:
            delta_sec = (target_dt - ref_now).total_seconds()
            if 5.0 <= delta_sec <= 7 * 86400:
                clean_text = sanitize_reminder_text(raw_text)
                if 1 <= len(clean_text) <= 100:
                    at_str = target_dt.strftime("%H:%M")
                    return ToolCallSpec(
                        name="set_reminder",
                        args={"text": clean_text, "at": at_str},
                        target_name=clean_text,
                    )
        return None

    # 4. "set a reminder for <clock> to <text>"
    m4 = re.match(r"^set\s+a\s+reminder\s+for\s+(.+?)\s+to\s+(.+)$", cleaned, re.IGNORECASE)
    if m4:
        clock_str = m4.group(1).strip()
        raw_text = m4.group(2).strip()
        if any(ch in _FORBIDDEN_REMINDER_CHARS for ch in raw_text):
            return None
        if re.search(r"://|www\.|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?", raw_text, re.IGNORECASE):
            return None
        target_dt = parse_clock(clock_str, now=ref_now)
        if target_dt is not None:
            delta_sec = (target_dt - ref_now).total_seconds()
            if 5.0 <= delta_sec <= 7 * 86400:
                clean_text = sanitize_reminder_text(raw_text)
                if 1 <= len(clean_text) <= 100:
                    at_str = target_dt.strftime("%H:%M")
                    return ToolCallSpec(
                        name="set_reminder",
                        args={"text": clean_text, "at": at_str},
                        target_name=clean_text,
                    )
        return None

    # 5. Timers
    timer_dur: Optional[str] = None
    t1 = re.match(r"^set\s+a\s+timer\s+for\s+(.+)$", cleaned, re.IGNORECASE)
    if t1:
        timer_dur = t1.group(1).strip()
    else:
        t2 = re.match(r"^set\s+a\s+(.+?)\s+timer$", cleaned, re.IGNORECASE)
        if t2:
            timer_dur = t2.group(1).strip()
        else:
            t3 = re.match(r"^start\s+a\s+(.+?)\s+timer$", cleaned, re.IGNORECASE)
            if t3:
                timer_dur = t3.group(1).strip()
            else:
                t4 = re.match(r"^timer\s+(?:for\s+)?(.+)$", cleaned, re.IGNORECASE)
                if t4:
                    timer_dur = t4.group(1).strip()

    if timer_dur is not None:
        td = parse_relative(timer_dur)
        if td is not None:
            total_sec = td.total_seconds()
            if 5.0 <= total_sec <= 7 * 86400:
                minutes = total_sec / 60.0
                return ToolCallSpec(
                    name="set_timer",
                    args={"minutes": round(minutes, 4), "label": "Timer"},
                    target_name="timer",
                )
        return None

    # 6. List / show reminders
    if (
        re.match(r"^(?:list|show)\s+(?:my\s+)?reminders$", cleaned, re.IGNORECASE)
        or re.match(r"^what\s+reminders\s+do\s+i\s+have$", cleaned, re.IGNORECASE)
        or re.match(r"^what\s+timers\s+do\s+i\s+have$", cleaned, re.IGNORECASE)
    ):
        return ToolCallSpec(
            name="list_reminders",
            args={},
            target_name="reminders",
        )

    # 7. Cancel
    c_rem = re.match(r"^cancel\s+reminder\s+(\d+)$", cleaned, re.IGNORECASE)
    if c_rem:
        return ToolCallSpec(
            name="cancel_reminder",
            args={"id": int(c_rem.group(1))},
            target_name=f"reminder {c_rem.group(1)}",
        )

    c_tim = re.match(r"^cancel\s+timer\s+(\d+)$", cleaned, re.IGNORECASE)
    if c_tim:
        return ToolCallSpec(
            name="cancel_reminder",
            args={"id": int(c_tim.group(1))},
            target_name=f"timer {c_tim.group(1)}",
        )

    c_all = re.match(r"^cancel\s+(?:all\s+)?(reminders|timers)$", cleaned, re.IGNORECASE)
    if c_all:
        return ToolCallSpec(
            name="cancel_all_reminders",
            args={},
            target_name="all reminders",
        )

    return None



_WORD_TO_NUM: dict[str, int] = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def parse_system_control_shortcut(cleaned: str) -> Optional[ToolCallSpec]:
    """Parse cleaned user text into a system control ToolCallSpec if matching strict patterns."""
    if not cleaned:
        return None

    # 1. Volume adjustment:
    # "volume up|down", "turn (the )?volume up|down", optionally followed by
    # "a bit" (2), "a lot" (5), "by N" (1-10, also words one to ten); default 3.
    m_vol = re.match(
        r"^(?:turn\s+(?:the\s+)?)?volume\s+(up|down)(?:\s+(.+))?$",
        cleaned,
        re.IGNORECASE,
    )
    if m_vol:
        direction = m_vol.group(1).lower()
        suffix = (m_vol.group(2) or "").strip().lower()
        steps = 3
        if suffix:
            if suffix == "a bit":
                steps = 2
            elif suffix == "a lot":
                steps = 5
            elif suffix.startswith("by "):
                by_val = suffix[3:].strip()
                if by_val in _WORD_TO_NUM:
                    steps = _WORD_TO_NUM[by_val]
                else:
                    return None
            else:
                return None
        return ToolCallSpec(
            name="volume_change",
            args={"direction": direction, "steps": steps},
            target_name=f"volume {direction}",
        )

    # "louder" (up 3), "quieter" / "softer" (down 3)
    if cleaned.lower() == "louder":
        return ToolCallSpec(
            name="volume_change",
            args={"direction": "up", "steps": 3},
            target_name="volume up",
        )
    if cleaned.lower() in ("quieter", "softer"):
        return ToolCallSpec(
            name="volume_change",
            args={"direction": "down", "steps": 3},
            target_name="volume down",
        )

    # 2. Volume mute toggle:
    # "mute|unmute (the )?(volume|sound|computer|system|speakers)" and "toggle mute"
    if (
        re.match(
            r"^(?:mute|unmute)\s+(?:the\s+)?(?:volume|sound|computer|system|speakers)$",
            cleaned,
            re.IGNORECASE,
        )
        or cleaned.lower() == "toggle mute"
    ):
        return ToolCallSpec(
            name="volume_mute_toggle",
            args={},
            target_name="mute",
        )

    # 3. Media keys:
    # "(pause|play|resume) (the )?(music|song|track|audio)" -> media_key play_pause
    if re.match(
        r"^(?:pause|play|resume)\s+(?:the\s+)?(?:music|song|track|audio)$",
        cleaned,
        re.IGNORECASE,
    ):
        return ToolCallSpec(
            name="media_key",
            args={"action": "play_pause"},
            target_name="play_pause",
        )

    # "stop (the )?(music|song|track|audio)" -> media_key stop
    if re.match(
        r"^stop\s+(?:the\s+)?(?:music|song|track|audio)$",
        cleaned,
        re.IGNORECASE,
    ):
        return ToolCallSpec(
            name="media_key",
            args={"action": "stop"},
            target_name="stop",
        )

    # "next (song|track)" and "skip (this |the )?(song|track)" -> media_key next
    if (
        re.match(r"^next\s+(?:song|track)$", cleaned, re.IGNORECASE)
        or re.match(r"^skip\s+(?:(?:this|the)\s+)?(?:song|track)$", cleaned, re.IGNORECASE)
    ):
        return ToolCallSpec(
            name="media_key",
            args={"action": "next"},
            target_name="next",
        )

    # "(previous|last) (song|track)" and "go back a song" -> media_key previous
    if (
        re.match(r"^(?:previous|last)\s+(?:song|track)$", cleaned, re.IGNORECASE)
        or cleaned.lower() == "go back a song"
    ):
        return ToolCallSpec(
            name="media_key",
            args={"action": "previous"},
            target_name="previous",
        )

    # 4. Lock screen:
    # "lock (the |my )?(screen|computer|pc|laptop)" -> lock_screen
    if re.match(
        r"^lock\s+(?:(?:the|my)\s+)?(?:screen|computer|pc|laptop)$",
        cleaned,
        re.IGNORECASE,
    ):
        return ToolCallSpec(
            name="lock_screen",
            args={},
            target_name="lock",
        )

    # 5. Open settings page:
    # "open (the )?(bluetooth|wifi|wi-fi|network|display|sound|apps|update|updates|privacy|battery|storage|accounts?) settings"
    m_set = re.match(
        r"^open\s+(?:the\s+)?(bluetooth|wifi|wi-fi|network|display|sound|apps|update|updates|privacy|battery|storage|accounts?)\s+settings$",
        cleaned,
        re.IGNORECASE,
    )
    if m_set:
        page_raw = m_set.group(1).lower()
        page_map = {
            "wifi": "wifi",
            "wi-fi": "wifi",
            "update": "updates",
            "updates": "updates",
            "account": "accounts",
            "accounts": "accounts",
        }
        canon_page = page_map.get(page_raw, page_raw)
        return ToolCallSpec(
            name="open_settings_page",
            args={"page": canon_page},
            target_name=f"{canon_page} settings",
        )

    return None


def is_system_control_pattern(text: str) -> bool:
    """Return True if text matches any system control pattern."""
    if not text or not isinstance(text, str):
        return False
    cleaned = text.strip()
    while True:
        prev = cleaned
        cleaned = re.sub(
            r"^(?:hey|hi|ok|okay|now|just|please|can\s+you|could\s+you|can\s+u|would\s+you)[,:\s]*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned == prev:
            break
    cleaned = re.sub(r"[.!?,:\s]+$", "", cleaned).strip()
    return parse_system_control_shortcut(cleaned) is not None



RESERVED_CONTACT_WORDS: set[str] = {
    "me", "him", "her", "them", "us", "a", "an", "the", "this", "that", "it",
    "later", "soon", "now", "back", "again", "when", "if", "message", "text", "someone"
}

_NAME_CHAR_RE = re.compile(r"^[A-Za-z0-9 '-]+$")


def _is_valid_contact_name_syntax(name: str) -> bool:
    """Validate candidate contact name syntax (1-3 words, allowed chars, no reserved words)."""
    if not name or not isinstance(name, str):
        return False
    trimmed = name.strip()
    words = trimmed.split()
    if not (1 <= len(words) <= 3):
        return False
    if not _NAME_CHAR_RE.match(trimmed):
        return False
    first = words[0].lower().strip("'-")
    last = words[-1].lower().strip("'-")
    if first in RESERVED_CONTACT_WORDS or last in RESERVED_CONTACT_WORDS:
        return False
    return True


def _is_valid_body(body: str) -> bool:
    """Validate message body: 1-300 chars, 1-60 words, not starting with conjunction/sequencing word."""
    if not body or not isinstance(body, str):
        return False
    trimmed = body.strip()
    if not (1 <= len(trimmed) <= 300):
        return False
    words = trimmed.split()
    if not (1 <= len(words) <= 60):
        return False
    first = words[0].lower().strip(".,!?:;\"'()[]{}")
    if first in CONJUNCTIONS_SEQUENCING:
        return False
    return True


def _match_contact_name(raw_name: str, contacts: Any = None, contact_names: Optional[Sequence[str]] = None) -> Optional[str]:
    """Match candidate contact name strictly against known contacts by longest exact match (never fuzzy)."""
    if not raw_name or not isinstance(raw_name, str):
        return None
    query_norm = " ".join(raw_name.strip().split()).lower()
    if not query_norm:
        return None

    # If contacts has resolve() method (e.g. ContactStore)
    if contacts is not None and hasattr(contacts, "resolve"):
        try:
            ref = contacts.resolve(raw_name)
            return ref.display_name
        except Exception:
            pass

    # Build candidates list
    candidates: list[tuple[str, str]] = []  # (normalized_alias, canonical_display_name)
    if contacts is not None and hasattr(contacts, "list_contacts"):
        try:
            for c in contacts.list_contacts():
                candidates.append(( " ".join(c.display_name.strip().split()).lower(), c.display_name ))
                for a in c.aliases:
                    candidates.append(( " ".join(a.strip().split()).lower(), c.display_name ))
        except Exception:
            pass
    elif contact_names is not None:
        for n in contact_names:
            candidates.append(( " ".join(n.strip().split()).lower(), n.strip() ))

    # Sort candidates by number of words descending, then character length descending
    candidates.sort(key=lambda item: (len(item[0].split()), len(item[0])), reverse=True)

    for norm_name, disp_name in candidates:
        if norm_name == query_norm:
            return disp_name

    return None


def parse_messaging_shortcut(
    cleaned: str,
    contacts: Any = None,
    contact_names: Optional[Sequence[str]] = None,
) -> Optional[Union[ToolCallSpec, ShortcutReply]]:
    """Parse cleaned user text into messaging ToolCallSpec or ShortcutReply."""
    if not cleaned or not isinstance(cleaned, str):
        return None

    if any(ch in _DISALLOWED_CHARS for ch in cleaned):
        return None

    # Load contacts if not passed
    if contacts is None and contact_names is None:
        try:
            from contacts.store import ContactStore
            contacts = ContactStore()
        except Exception:
            contacts = None

    # Pattern a1: text|message|whatsapp <name> (on whatsapp)? (saying|that|to say|:) <body>
    m_a1 = re.match(
        r"^(?:text|message|whatsapp)\s+(.+?)(?:\s+on\s+whatsapp)?\s+(?:saying|that|to\s+say|:)\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_a1:
        raw_name = m_a1.group(1).strip()
        body = m_a1.group(2).strip()
        if _is_valid_body(body):
            matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
            if matched:
                return ToolCallSpec(
                    name="send_whatsapp",
                    args={"contact": matched, "text": body, "attach_link": False},
                    target_name="whatsapp",
                )
            if _is_valid_contact_name_syntax(raw_name):
                return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Pattern a2: send (a )?(whatsapp )?(message|text) to <name> (on whatsapp)? (saying|that|:) <body>
    m_a2 = re.match(
        r"^send\s+(?:a\s+)?(?:whatsapp\s+)?(?:message|text)\s+to\s+(.+?)(?:\s+on\s+whatsapp)?\s+(?:saying|that|:)\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_a2:
        raw_name = m_a2.group(1).strip()
        body = m_a2.group(2).strip()
        if _is_valid_body(body):
            matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
            if matched:
                return ToolCallSpec(
                    name="send_whatsapp",
                    args={"contact": matched, "text": body, "attach_link": False},
                    target_name="whatsapp",
                )
            if _is_valid_contact_name_syntax(raw_name):
                return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Pattern b: (open|start) (a )?(whatsapp )?chat with <name>
    m_b = re.match(
        r"^(?:open|start)\s+(?:a\s+)?(?:whatsapp\s+)?chat\s+with\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_b:
        raw_name = m_b.group(1).strip()
        matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
        if matched:
            return ToolCallSpec(
                name="send_whatsapp",
                args={"contact": matched, "text": "", "attach_link": False},
                target_name="whatsapp",
            )
        if _is_valid_contact_name_syntax(raw_name):
            return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Pattern c1: send (this|the|that) (link|page|video|article) to <name> (on whatsapp)?( (saying|with|:) <body>)?
    m_c1 = re.match(
        r"^send\s+(?:this|the|that)\s+(?:link|page|video|article)\s+to\s+(.+?)(?:\s+on\s+whatsapp)?(?:\s+(?:saying|with|:)\s+(.+))?$",
        cleaned,
        re.IGNORECASE,
    )
    if m_c1:
        raw_name = m_c1.group(1).strip()
        body = (m_c1.group(2) or "").strip()
        if body and not _is_valid_body(body):
            return None
        matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
        if matched:
            return ToolCallSpec(
                name="send_whatsapp",
                args={"contact": matched, "text": body, "attach_link": True},
                target_name="whatsapp",
            )
        if _is_valid_contact_name_syntax(raw_name):
            return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Pattern c2: share (this|the) (link|page|video|article) with <name> (on whatsapp)?
    m_c2 = re.match(
        r"^share\s+(?:this|the)\s+(?:link|page|video|article)\s+with\s+(.+?)(?:\s+on\s+whatsapp)?$",
        cleaned,
        re.IGNORECASE,
    )
    if m_c2:
        raw_name = m_c2.group(1).strip()
        matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
        if matched:
            return ToolCallSpec(
                name="send_whatsapp",
                args={"contact": matched, "text": "", "attach_link": True},
                target_name="whatsapp",
            )
        if _is_valid_contact_name_syntax(raw_name):
            return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Pattern c3: message <name> (this|the) link
    m_c3 = re.match(
        r"^message\s+(.+?)\s+(?:this|the)\s+link$",
        cleaned,
        re.IGNORECASE,
    )
    if m_c3:
        raw_name = m_c3.group(1).strip()
        matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
        if matched:
            return ToolCallSpec(
                name="send_whatsapp",
                args={"contact": matched, "text": "", "attach_link": True},
                target_name="whatsapp",
            )
        if _is_valid_contact_name_syntax(raw_name):
            return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Pattern d1: (email|mail) <name> (saying|that|about|:) <body>
    m_d1 = re.match(
        r"^(?:email|mail)\s+(.+?)\s+(?:saying|that|about|:)\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_d1:
        raw_name = m_d1.group(1).strip()
        body = m_d1.group(2).strip()
        if _is_valid_body(body):
            matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
            if matched:
                return ToolCallSpec(
                    name="open_email_draft",
                    args={"contact": matched, "subject": body[:60], "body": body, "attach_link": False},
                    target_name="email",
                )
            if _is_valid_contact_name_syntax(raw_name):
                return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Pattern d2: email (this|the) (link|page) to <name>
    m_d2 = re.match(
        r"^email\s+(?:this|the)\s+(?:link|page)\s+to\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_d2:
        raw_name = m_d2.group(1).strip()
        matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
        if matched:
            return ToolCallSpec(
                name="open_email_draft",
                args={"contact": matched, "subject": "", "body": "", "attach_link": True},
                target_name="email",
            )
        if _is_valid_contact_name_syntax(raw_name):
            return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    # Bare pattern: text|message <name>
    m_bare = re.match(
        r"^(?:text|message)\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_bare:
        raw_name = m_bare.group(1).strip()
        matched = _match_contact_name(raw_name, contacts=contacts, contact_names=contact_names)
        if matched:
            return ShortcutReply(f"What should I say to {matched}? Try: text {matched} saying ...")
        if _is_valid_contact_name_syntax(raw_name):
            return ShortcutReply(f"I don't have a contact called {raw_name}. Add it in Contacts.")
        return None

    return None


def parse_laptop_shortcut(
    cleaned: str,
    allowed_apps: Optional[Mapping[str, Any]] = None,
    state: Optional[Any] = None,
    window_control_enabled: Optional[bool] = None,
    files_enabled: Optional[bool] = None,
    screenshots_enabled: Optional[bool] = None,
    typing_enabled: Optional[bool] = None,
    sleep_enabled: Optional[bool] = None,
) -> Optional[Union[ToolCallSpec, ShortcutReply]]:
    """Parse window controls, user file operations, screenshots, typing, and sleep shortcuts."""
    from config import settings
    from config.apps import ALLOWED_APPS, TYPING_ALLOWLIST

    apps_map = allowed_apps if allowed_apps is not None else ALLOWED_APPS

    if window_control_enabled is None:
        window_control_enabled = getattr(settings, "WINDOW_CONTROL_ENABLED", True)
    if files_enabled is None:
        files_enabled = getattr(settings, "FILES_ENABLED", True)
    if screenshots_enabled is None:
        screenshots_enabled = getattr(settings, "SCREENSHOTS_ENABLED", True)
    if typing_enabled is None:
        typing_enabled = getattr(settings, "TYPING_ENABLED", False)
    if sleep_enabled is None:
        sleep_enabled = getattr(settings, "SLEEP_ENABLED", False)

    def _resolve_win_app(name: str) -> Optional[str]:
        c = name.strip().lower()
        if c in apps_map:
            info = apps_map[c]
            if isinstance(info, dict) and info.get("windows"):
                return c
        return None

    # show desktop / minimize everything
    if re.match(r"^(?:show\s+(?:the\s+)?desktop|minimize\s+(?:everything|all\s+windows))$", cleaned, re.IGNORECASE):
        if not window_control_enabled:
            return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
        return ToolCallSpec(name="show_desktop", args={}, target_name="desktop")

    # switch to / focus <app>
    m_focus = re.match(r"^(?:switch\s+to|focus)\s+([a-zA-Z0-9_-]+)$", cleaned, re.IGNORECASE)
    if m_focus:
        app_key = _resolve_win_app(m_focus.group(1))
        if app_key:
            if not window_control_enabled:
                return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
            return ToolCallSpec(name="focus_app", args={"app": app_key}, target_name=app_key)
        return None

    # bring <app> to the front
    m_front = re.match(r"^bring\s+([a-zA-Z0-9_-]+)\s+to\s+the\s+front$", cleaned, re.IGNORECASE)
    if m_front:
        app_key = _resolve_win_app(m_front.group(1))
        if app_key:
            if not window_control_enabled:
                return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
            return ToolCallSpec(name="focus_app", args={"app": app_key}, target_name=app_key)
        return None

    # minimize <app>
    m_min = re.match(r"^minimize\s+([a-zA-Z0-9_-]+)$", cleaned, re.IGNORECASE)
    if m_min:
        app_key = _resolve_win_app(m_min.group(1))
        if app_key:
            if not window_control_enabled:
                return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
            return ToolCallSpec(name="minimize_app", args={"app": app_key}, target_name=app_key)
        return None

    # maximize <app>
    m_max = re.match(r"^maximize\s+([a-zA-Z0-9_-]+)$", cleaned, re.IGNORECASE)
    if m_max:
        app_key = _resolve_win_app(m_max.group(1))
        if app_key:
            if not window_control_enabled:
                return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
            return ToolCallSpec(name="maximize_app", args={"app": app_key}, target_name=app_key)
        return None

    # restore <app>
    m_res = re.match(r"^restore\s+([a-zA-Z0-9_-]+)$", cleaned, re.IGNORECASE)
    if m_res:
        app_key = _resolve_win_app(m_res.group(1))
        if app_key:
            if not window_control_enabled:
                return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
            return ToolCallSpec(name="restore_app", args={"app": app_key}, target_name=app_key)
        return None

    # snap <app> (to the )?(left|right)
    m_snap = re.match(r"^snap\s+([a-zA-Z0-9_-]+)(?:\s+to\s+the)?\s+(left|right)$", cleaned, re.IGNORECASE)
    if m_snap:
        app_key = _resolve_win_app(m_snap.group(1))
        side = m_snap.group(2).lower()
        if app_key and side in ("left", "right"):
            if not window_control_enabled:
                return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
            return ToolCallSpec(name="snap_app", args={"app": app_key, "side": side}, target_name=f"{app_key} {side}")
        return None

    # put <app> on the (left|right)( half)?
    m_put = re.match(r"^put\s+([a-zA-Z0-9_-]+)\s+on\s+the\s+(left|right)(?:\s+half)?$", cleaned, re.IGNORECASE)
    if m_put:
        app_key = _resolve_win_app(m_put.group(1))
        side = m_put.group(2).lower()
        if app_key and side in ("left", "right"):
            if not window_control_enabled:
                return ShortcutReply("Window control is off. Turn on WINDOW_CONTROL_ENABLED in Settings.")
            return ToolCallSpec(name="snap_app", args={"app": app_key, "side": side}, target_name=f"{app_key} {side}")
        return None

    # Screenshots
    if re.match(r"^(?:take\s+(?:a\s+)?(?:screenshot|screen\s+shot)|screenshot|capture\s+(?:the\s+)?screen)$", cleaned, re.IGNORECASE):
        if not screenshots_enabled:
            return ShortcutReply("Screenshots are off. Turn on SCREENSHOTS_ENABLED in Settings.")
        return ToolCallSpec(name="take_screenshot", args={}, target_name="screenshot")

    # Sleep
    if re.match(r"^put\s+(?:the\s+|this\s+|my\s+)?(?:computer|laptop|pc)\s+to\s+sleep$", cleaned, re.IGNORECASE):
        if not sleep_enabled:
            return ShortcutReply("Sleep is off. Turn on SLEEP_ENABLED in Settings.")
        return ToolCallSpec(name="sleep_computer", args={}, target_name="sleep")

    # Typing
    m_type1 = re.match(r"^type\s+(.+?)\s+(?:in|into)\s+([a-zA-Z0-9_-]+)$", cleaned, re.IGNORECASE)
    m_type2 = re.match(r"^in\s+([a-zA-Z0-9_-]+)\s+type\s+(.+)$", cleaned, re.IGNORECASE)
    m_type3 = re.match(r"^write\s+(.+?)\s+in\s+([a-zA-Z0-9_-]+)$", cleaned, re.IGNORECASE)

    raw_text: Optional[str] = None
    target_app: Optional[str] = None

    if m_type1:
        raw_text = m_type1.group(1).strip()
        target_app = m_type1.group(2).strip().lower()
    elif m_type2:
        target_app = m_type2.group(1).strip().lower()
        raw_text = m_type2.group(2).strip()
    elif m_type3:
        raw_text = m_type3.group(1).strip()
        target_app = m_type3.group(2).strip().lower()

    if raw_text is not None and target_app is not None:
        words = raw_text.split()
        if (
            1 <= len(raw_text) <= 300
            and 1 <= len(words) <= 60
            and not re.match(r"^(?:and|then|after|so|but|or|because)", raw_text, re.IGNORECASE)
        ):
            allowed_set = {a.lower() for a in TYPING_ALLOWLIST}
            if target_app not in allowed_set:
                allow_str = ", ".join(sorted(list(allowed_set)))
                return ShortcutReply(f"I only type into {allow_str}.")
            if not typing_enabled:
                return ShortcutReply("Typing is off. Turn on TYPING_ENABLED in Settings.")
            return ToolCallSpec(
                name="type_into_app",
                args={"app": target_app, "text": raw_text},
                target_name=target_app,
            )
        return None

    # File open / reveal
    _ORDS = {
        "first": 1, "1st": 1, "top": 1, "1": 1,
        "second": 2, "2nd": 2, "2": 2,
        "third": 3, "3rd": 3, "3": 3,
        "fourth": 4, "4th": 4, "4": 4,
        "fifth": 5, "5th": 5, "5": 5,
    }

    m_rev = re.match(
        r"^reveal\s+(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top)\s+file\s+in\s+(?:the\s+)?folder$",
        cleaned,
        re.IGNORECASE,
    )
    if m_rev:
        idx = _ORDS[m_rev.group(1).lower()]
        if not files_enabled:
            return ShortcutReply("File operations are off. Turn on FILES_ENABLED in Settings.")
        return ToolCallSpec(name="reveal_found_file", args={"index": idx}, target_name=f"file #{idx}")

    m_open_file = re.match(
        r"^open\s+(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top)\s+file$",
        cleaned,
        re.IGNORECASE,
    )
    if m_open_file:
        idx = _ORDS[m_open_file.group(1).lower()]
        if not files_enabled:
            return ShortcutReply("File operations are off. Turn on FILES_ENABLED in Settings.")
        return ToolCallSpec(name="open_found_file", args={"index": idx}, target_name=f"file #{idx}")

    m_open_num = re.match(
        r"^open\s+file\s+(?:number\s+)?([1-5])$",
        cleaned,
        re.IGNORECASE,
    )
    if m_open_num:
        idx = int(m_open_num.group(1))
        if not files_enabled:
            return ShortcutReply("File operations are off. Turn on FILES_ENABLED in Settings.")
        return ToolCallSpec(name="open_found_file", args={"index": idx}, target_name=f"file #{idx}")

    m_bare_ord = re.match(
        r"^open\s+(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top)\s+(?:one|result)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_bare_ord:
        idx = _ORDS[m_bare_ord.group(1).lower()]
        is_files = (
            state is not None
            and getattr(state, "last_result_kind", None) == "files"
            and bool(state.get_found_files())
        )
        if is_files:
            if not files_enabled:
                return ShortcutReply("File operations are off. Turn on FILES_ENABLED in Settings.")
            return ToolCallSpec(name="open_found_file", args={"index": idx}, target_name=f"file #{idx}")

    # File search
    _KIND_MAP = {
        "pdf": "pdf",
        "document": "document",
        "documents": "document",
        "image": "image",
        "images": "image",
        "photo": "image",
        "photos": "image",
        "picture": "image",
        "pictures": "image",
        "video": "video",
        "videos": "video",
        "audio": "audio",
        "file": "any",
        "files": "any",
    }

    m_latest = re.match(
        r"^find\s+(?:my\s+)?(?:latest|newest|most\s+recent)\s+(download|pdf|document|image|photo|picture|video|file)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_latest:
        target_kind_word = m_latest.group(1).lower()
        if target_kind_word == "download":
            kind = "any"
            query = "download"
        else:
            kind = _KIND_MAP.get(target_kind_word, "any")
            query = ""
        if not files_enabled:
            return ShortcutReply("File operations are off. Turn on FILES_ENABLED in Settings.")
        return ToolCallSpec(
            name="find_files",
            args={"query": query, "kind": kind, "recent": True, "_free_text": False},
            target_name=f"recent {target_kind_word}",
        )

    m_find = re.match(
        r"^find\s+(?:my\s+)?(recent\s+)?(?:(pdf|document|documents|image|images|photo|photos|picture|pictures|video|videos|audio|file|files)\s+)?(?:(?:named|called|about)\s+)?([a-zA-Z0-9\s]+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_find:
        is_recent_word = bool(m_find.group(1))
        kind_word = (m_find.group(2) or "").lower()
        query_str = m_find.group(3).strip()
        has_named_clause = bool(re.search(r"(?:named|called|about)", cleaned, re.IGNORECASE))

        if kind_word or is_recent_word or has_named_clause:
            words = query_str.split()
            if 1 <= len(words) <= 5 and not any(ch in r"*?/[\];:|`<>$" for ch in query_str):
                kind = _KIND_MAP.get(kind_word, "any")
                recent = is_recent_word
                if not files_enabled:
                    return ShortcutReply("File operations are off. Turn on FILES_ENABLED in Settings.")
                return ToolCallSpec(
                    name="find_files",
                    args={"query": query_str, "kind": kind, "recent": recent, "_free_text": True},
                    target_name=f"{kind} files",
                )

    return None


def parse_shortcut(
    text: str,
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
    state: Optional[Any] = None,
    browser_enabled: bool = False,
    reminders_enabled: Optional[bool] = None,
    system_controls_enabled: Optional[bool] = None,
    clock: Optional[Callable[[], datetime]] = None,
    contacts: Optional[Any] = None,
    contact_names: Optional[Sequence[str]] = None,
    messaging_enabled: Optional[bool] = None,
    window_control_enabled: Optional[bool] = None,
    files_enabled: Optional[bool] = None,
    screenshots_enabled: Optional[bool] = None,
    typing_enabled: Optional[bool] = None,
    sleep_enabled: Optional[bool] = None,
) -> Optional[Union[ToolCallSpec, ShortcutPlan, ShortcutReply]]:
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
        if not re.match(r"^what(?:'?s|\s+is)\b", cleaned, re.IGNORECASE):
            cleaned = re.sub(
                r"[,:\s]+(?:in\s+(?:the\s+same|this)\s+(?:browser|tab|window)|on\s+this\s+page)[.!?,:\s]*$",
                "",
                cleaned,
                flags=re.IGNORECASE,
            ).strip()
        else:
            cleaned = re.sub(
                r"[,:\s]+in\s+(?:the\s+same|this)\s+(?:browser|tab|window)[.!?,:\s]*$",
                "",
                cleaned,
                flags=re.IGNORECASE,
            ).strip()
        cleaned = re.sub(r"[.!?,:\s]+$", "", cleaned).strip()
        if cleaned == prev:
            break

    if not cleaned:
        return None

    # Early rejections per Requirement 7:
    # "in my default browser", "in chrome", and "in a new tab" return None (unless typing into an app)
    if not re.match(r"^(?:type|write)\b|^in\s+[a-zA-Z0-9_-]+\s+type\b", cleaned, re.IGNORECASE):
        if re.search(r"\b(?:in\s+(?:my\s+)?default\s+browser|in\s+chrome|in\s+a\s+new\s+tab)\b", cleaned, re.IGNORECASE):
            return None

    # "open|click the first link|website|page" containing "or" returns None
    if (
        re.search(r"\b(?:open|click|play)\b.*\b(?:link|website|site|page)\b.*\bor\b", cleaned, re.IGNORECASE)
        or re.search(r"\bor\b.*\b(?:open|click|play)\b.*\b(?:link|website|site|page)\b", cleaned, re.IGNORECASE)
    ):
        return None

    if is_reminder_or_timer_pattern(cleaned):
        if reminders_enabled is None:
            env_r = os.getenv("REMINDERS_ENABLED")
            if env_r is not None:
                reminders_enabled = env_r.strip().lower() not in ("0", "false", "no", "off")
            else:
                from config import settings
                reminders_enabled = getattr(settings, "REMINDERS_ENABLED", True)
        if reminders_enabled:
            return parse_reminder_shortcut(cleaned, clock=clock)
        return None

    # System controls checked BEFORE browser patterns
    if system_controls_enabled is None:
        env_s = os.getenv("SYSTEM_CONTROLS_ENABLED")
        if env_s is not None:
            system_controls_enabled = env_s.strip().lower() not in ("0", "false", "no", "off")
        else:
            import sys as _sys
            from config import settings
            system_controls_enabled = getattr(settings, "SYSTEM_CONTROLS_ENABLED", _sys.platform == "win32")

    if system_controls_enabled:
        sys_spec = parse_system_control_shortcut(cleaned)
        if sys_spec is not None:
            return sys_spec

    # Messaging shortcuts (WhatsApp and Email)
    if messaging_enabled is None:
        env_m = os.getenv("MESSAGING_ENABLED")
        if env_m is not None:
            messaging_enabled = env_m.strip().lower() not in ("0", "false", "no", "off")
        else:
            from config import settings
            messaging_enabled = getattr(settings, "MESSAGING_ENABLED", True)

    if messaging_enabled:
        msg_spec = parse_messaging_shortcut(
            cleaned,
            contacts=contacts,
            contact_names=contact_names,
        )
        if msg_spec is not None:
            return msg_spec

    # Laptop shortcuts (window, files, screenshots, typing, sleep)
    lap_spec = parse_laptop_shortcut(
        cleaned,
        allowed_apps=allowed_apps,
        state=state,
        window_control_enabled=window_control_enabled,
        files_enabled=files_enabled,
        screenshots_enabled=screenshots_enabled,
        typing_enabled=typing_enabled,
        sleep_enabled=sleep_enabled,
    )
    if lap_spec is not None:
        return lap_spec

    if browser_enabled:
        if sites is None:
            from config.sites import SITES
            sites = SITES
        from config.sites import resolve_site
        clean_sites_map = {k.lower(): k for k in sites}

        # Restart shortcut
        if re.match(r"^restart\s+(?:the\s+)?browser$", cleaned, re.IGNORECASE):
            return ToolCallSpec(
                name="browser_restart",
                args={},
                target_name="browser",
            )

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
        # "play|open|click (the)? <ordinal> (noun)? [of|for|from|about]? <query> [on|in <site>]"
        ord_token_pattern = r"(10th|[1-9](?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|top)"
        num_token_pattern = r"(10th|10|[1-9](?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|top|one|two|three|four|five|six|seven|eight|nine|ten)"
        noun_pattern = r"(video|result|item|one|link|website|site|page|article|short|story)"
        post_noun_pattern = r"(video|result|item|link|website|site|page|article|short|story)"

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

        click_ord_match = re.match(
            rf"^(?:play|open|click)(?:\s+the)?\s+{ord_token_pattern}(?:\s+{noun_pattern})?(?:\s+(.*))?$",
            cleaned,
            re.IGNORECASE,
        )
        matched_noun = None
        if click_ord_match:
            n = ord_map[click_ord_match.group(1).lower()]
            matched_noun = click_ord_match.group(2).lower() if click_ord_match.group(2) else None
            rem = (click_ord_match.group(3) or "").strip()
        else:
            click_num_match = re.match(
                rf"^(?:play|open|click)\s+number\s+{num_token_pattern}(?:\s+(.*))?$",
                cleaned,
                re.IGNORECASE,
            )
            if click_num_match:
                n = ord_map[click_num_match.group(1).lower()]
                matched_noun = None
                rem = (click_num_match.group(2) or "").strip()
            else:
                click_noun_match = re.match(
                    rf"^(?:play|open|click)(?:\s+the)?\s+{post_noun_pattern}\s+(?:number\s+)?{num_token_pattern}(?:\s+(.*))?$",
                    cleaned,
                    re.IGNORECASE,
                )
                if click_noun_match:
                    matched_noun = click_noun_match.group(1).lower()
                    n = ord_map[click_noun_match.group(2).lower()]
                    rem = (click_noun_match.group(3) or "").strip()
                else:
                    n = None
                    rem = None

        if n is not None:
            if not rem:
                return ToolCallSpec(
                    name="browser_click_result",
                    args={"n": n},
                    target_name=f"result #{n}",
                )

            if rem.lower() in ("of", "for", "from", "about"):
                return None

            explicit_prep = False
            prep_match = re.match(r"^(?:of|for|from|about)\s+(.+)$", rem, re.IGNORECASE)
            if prep_match:
                rem = prep_match.group(1).strip()
                explicit_prep = True

            if not rem:
                return None

            # Search query after ordinal allowed ONLY after nouns video/result/one/short, or after explicit of|for|from|about
            allowed_query_nouns = {"video", "result", "one", "short"}
            if not explicit_prep and (matched_noun not in allowed_query_nouns):
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

            if not is_valid_ordinal_query(query_candidate):
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

    # Pattern 4(a): Web search via "look up <query>"
    # Idiom guard: "look up to <anything>" must return None, as must "look up" alone.
    look_up_match = re.match(r"^look\s+up(?:\s+(.*))?$", cleaned, re.IGNORECASE)
    if look_up_match:
        cand_query = (look_up_match.group(1) or "").strip()
        if not cand_query:
            return None
        # Reject "look up to <anything>" idiom
        if cand_query.lower() == "to" or cand_query.lower().startswith("to "):
            return None
        if is_valid_search_query(cand_query, require_clean_tail=True):
            return ToolCallSpec(
                name="search_web",
                args={"query": cand_query},
                target_name="google",
            )
        return None

    # Pattern 4(b): Weather queries
    # "what's the weather in <place>", "what is the weather in <place>",
    # "weather in <place>", "weather <place>"
    # All produce search_web(query=f"weather in {place}").
    weather_match = re.match(
        r"^(?:what(?:'?s|\s+is)\s+the\s+weather\s+in\s+|weather\s+in\s+|weather\s+)(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if weather_match:
        place = weather_match.group(1).strip()
        if place.lower() == "in" or place.lower().startswith("in "):
            return None
        if place and is_valid_search_query(place, require_clean_tail=True):
            return ToolCallSpec(
                name="search_web",
                args={"query": f"weather in {place}"},
                target_name="google",
            )
        return None

    return None


def _strip_routine_fillers(text: str) -> str:
    """Strip allowed filler words from the front of routine shortcut text."""
    cleaned = text.strip()
    while True:
        prev = cleaned
        cleaned = re.sub(
            r"^(?:please|hey|can\s+you|can\s+u|could\s+you)\b[,:\s]*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned == prev:
            break
    return cleaned.rstrip(".?!").strip()


def _has_command_chain(name: str) -> bool:
    """Return True if name contains sequencing words or conjunctions with command verbs."""
    if not name:
        return False
    if re.search(r"\b(?:and\s+then|after\s+that|afterwards|then)\b|,", name, re.IGNORECASE):
        return True
    m_and = re.search(r"\band\s+([a-zA-Z]+)\b", name, re.IGNORECASE)
    if m_and:
        verb = m_and.group(1).lower()
        from routines.validate import FORBIDDEN_NAME_STARTS
        from core.chains import COMMAND_VERBS
        if verb in COMMAND_VERBS or verb in FORBIDDEN_NAME_STARTS:
            return True
    return False


def parse_routine_shortcut(
    text: str,
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
    state: Optional[Any] = None,
    browser_enabled: bool = False,
) -> Optional[RoutineAction]:
    """Parse a user command for routine management actions.

    Supported patterns:
    - create|make|save a routine (called|named)? <name> (:|that|to|as) <chain sentence>
    - save (that|this|the last plan) as (a )?routine (called|named)? <name>
    - run|start|do|launch (my )?routine <name>
    - run <name> routine
    - routine <name>
    - list routines / show routines / what routines do i have
    - show routine <name>
    - delete|remove routine <name>

    Returns RoutineAction or None.
    """
    if not text or not isinstance(text, str):
        return None
    if "\n" in text or "\r" in text:
        return None
    if "://" in text:
        return None
    if any(ch in _DISALLOWED_CHARS for ch in text):
        return None
    if len(text) > 300:
        return None

    cleaned = _strip_routine_fillers(text)
    if not cleaned:
        return None

    from routines.validate import (
        RoutineValidationError,
        validate_routine_name,
        validate_routine_steps,
    )

    # 1. List routines
    if re.match(
        r"^(?:list\s+routines|show\s+routines|what\s+routines\s+do\s+i\s+have)$",
        cleaned,
        re.IGNORECASE,
    ):
        return RoutineAction(action="list")

    # 2. Show routine <name>
    m_show = re.match(r"^show\s+routine\s+(.+)$", cleaned, re.IGNORECASE)
    if m_show:
        raw_name = m_show.group(1).strip()
        if _has_command_chain(raw_name) or "://" in raw_name:
            return None
        try:
            norm_name = validate_routine_name(raw_name)
            return RoutineAction(action="show", name=norm_name, display_name=raw_name)
        except RoutineValidationError:
            return None

    # 3. Delete routine <name>
    m_del = re.match(r"^(?:delete|remove)\s+routine\s+(.+)$", cleaned, re.IGNORECASE)
    if m_del:
        raw_name = m_del.group(1).strip()
        if _has_command_chain(raw_name) or "://" in raw_name:
            return None
        try:
            norm_name = validate_routine_name(raw_name)
            return RoutineAction(action="delete", name=norm_name, display_name=raw_name)
        except RoutineValidationError:
            return None

    # 4. Save last plan as routine
    m_last = re.match(
        r"^save\s+(?:that|this|the\s+last\s+plan)\s+as\s+(?:a\s+)?routine(?:\s+(?:called|named))?\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_last:
        raw_name = m_last.group(1).strip()
        if _has_command_chain(raw_name) or "://" in raw_name:
            return None
        try:
            norm_name = validate_routine_name(raw_name)
            return RoutineAction(action="save_last_plan", name=norm_name, display_name=raw_name)
        except RoutineValidationError:
            return None

    # 5. Create routine from sentence
    m_create = re.match(
        r"^(?:create|make|save)\s+(?:a\s+)?routine(?:\s+(?:called|named))?\s+(.+?)\s*(?::|that|to|as)\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_create:
        raw_name = m_create.group(1).strip()
        chain_sentence = m_create.group(2).strip()
        if _has_command_chain(raw_name) or "://" in raw_name:
            return None
        try:
            norm_name = validate_routine_name(raw_name)
        except RoutineValidationError:
            return None

        if is_reminder_or_timer_pattern(chain_sentence):
            return None
        if any(ch in _DISALLOWED_CHARS for ch in chain_sentence) or "://" in chain_sentence:
            return None

        from core.chains import parse_chain
        chain_plan = parse_chain(
            chain_sentence,
            allowed_apps=allowed_apps,
            sites=sites,
            state=state,
            browser_enabled=browser_enabled,
        )
        if chain_plan is not None:
            raw_steps = [
                {"tool": getattr(s, "tool", getattr(s, "name", "")), "args": dict(getattr(s, "args", {}))}
                for s in chain_plan.steps
            ]
        else:
            single_res = parse_shortcut(
                chain_sentence,
                allowed_apps=allowed_apps,
                sites=sites,
                state=state,
                browser_enabled=browser_enabled,
            )
            if isinstance(single_res, ToolCallSpec):
                raw_steps = [{"tool": single_res.name, "args": dict(single_res.args)}]
            elif isinstance(single_res, ShortcutPlan):
                raw_steps = [
                    {"tool": getattr(s, "tool", getattr(s, "name", "")), "args": dict(getattr(s, "args", {}))}
                    for s in single_res.steps
                ]
            else:
                return None

        try:
            validated_steps = validate_routine_steps(
                raw_steps,
                allowed_apps=allowed_apps,
                sites=sites,
                state=state,
            )
            return RoutineAction(
                action="create",
                name=norm_name,
                display_name=raw_name,
                steps=validated_steps,
            )
        except RoutineValidationError:
            return None

    # 6. Run routine
    m_run1 = re.match(r"^(?:run|start|do|launch)\s+(?:my\s+)?routine\s+(.+)$", cleaned, re.IGNORECASE)
    m_run2 = re.match(r"^run\s+(.+?)\s+routine$", cleaned, re.IGNORECASE)
    m_run3 = re.match(r"^routine\s+(.+)$", cleaned, re.IGNORECASE)

    target_name = None
    if m_run1:
        target_name = m_run1.group(1).strip()
    elif m_run2:
        target_name = m_run2.group(1).strip()
    elif m_run3:
        target_name = m_run3.group(1).strip()

    if target_name:
        if target_name.lower() in ("routine", "routines", "my routine"):
            return None
        if _has_command_chain(target_name) or "://" in target_name:
            return None
        try:
            norm_name = validate_routine_name(target_name)
            return RoutineAction(action="run", name=norm_name, display_name=target_name)
        except RoutineValidationError:
            return None

    return None

