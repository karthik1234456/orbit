"""Action claim extraction and verification heuristics for ORBIT.

Note: These are conservative heuristics and can miss claims or misfire on
ambiguous language. They are designed to flag clear first-person statements
where the model claims an action was taken or will be taken without tool execution.
"""

from dataclasses import dataclass
import re
from typing import Any, Sequence

# Families mapping claim kind to the corresponding tool names capable of performing it.
FAMILIES: dict[str, set[str]] = {
    "memory": {"remember_fact"},
    "file": {"create_file", "create_folder", "move_file", "write_note"},
    "app": {
        "open_application",
        "close_application",
        "open_website",
        "search_web",
        "open_workspace_folder",
        "open_in_vscode",
    },
    "system": {
        "volume_change",
        "volume_mute_toggle",
        "media_key",
        "lock_screen",
        "open_settings_page",
    },
}

NEGATION_WORDS: tuple[str, ...] = (
    "not",
    "n't",
    "never",
    "no",
    "without",
    "cannot",
    "unable",
    "wasn't",
    "weren't",
    "didn't",
    "won't",
    "can't",
    "don't",
    "couldn't",
    "wouldn't",
)

_NEGATION_RE = re.compile(
    r"\b(can't|cannot|can\s+not|won't|will\s+not|don't|do\s+not|unable|never|not\s+able|not|couldn't|could\s+not|wouldn't|would\s+not)\b",
    re.IGNORECASE,
)

_MEMORY_VERBS = r"(?:remember|save|saved|store|stored|note|noted|keep|kept)"
_FILE_VERBS = r"(?:created|wrote|written|saved|moved|deleted|renamed)"
_APP_VERBS = r"(?:opened|launched|closed)"

_SYSTEM_ACTION_RE = re.compile(
    r"\b(i(?:'ve|\s+have|'ll|\s+will|\s+already)?|\bi)\s+(?:[a-z]+\s+)*?"
    r"("
    r"(?:turned?|set|adjusted|raised?|lowered?)(?:\s+(?:up|down))?\s+(?:the\s+)?volume(?:\s+(?:up|down))?|"
    r"(?:muted?|unmuted?)(?:\s+(?:the\s+)?(?:sound|volume|audio|speakers?|computer|pc|system))?|"
    r"(?:paused?|played?|resumed?|stopped?)\s+(?:the\s+)?(?:music|song|track|audio|playback)|"
    r"(?:skipped?)\s+(?:the\s+|this\s+)?(?:song|track)|"
    r"(?:locked?)\s+(?:the\s+|your\s+|my\s+)?(?:screen|computer|pc|workstation|laptop)|"
    r"(?:opened?)\s+(?:the\s+)?(?:[a-z-]+\s+)?settings\b"
    r")",
    re.IGNORECASE,
)

_MEMORY_RE = re.compile(
    rf"\b(i(?:'ll|\s+will|'ve|\s+have))\s+(?:[a-z]+\s+)*({_MEMORY_VERBS})\b",
    re.IGNORECASE,
)
_FILE_RE = re.compile(
    rf"\b(i(?:'ve|\s+have))\s+(?:[a-z]+\s+)*({_FILE_VERBS})\b",
    re.IGNORECASE,
)
_FILE_TARGET_RE = re.compile(r"\b(file|folder|note)s?\b", re.IGNORECASE)
_APP_RE = re.compile(
    rf"\b(i(?:'ve|\s+have))\s+(?:[a-z]+\s+)*({_APP_VERBS})\b",
    re.IGNORECASE,
)


@dataclass
class Claim:
    """Represents a first-person assertion of an action taken or promised."""

    kind: str  # "memory" | "file" | "app" | "system"
    phrase: str


def find_action_claims(text: str) -> list[Claim]:
    """Extract conservative first-person action claims from model response text.

    Heuristic rules:
    - memory: I'll/I will/I've/I have + remember/save/saved/store/stored/note/noted/keep
    - file: I've/I have + created/wrote/saved/moved/deleted/renamed + file/folder/note
    - system: volume/mute/media/lock/settings action assertions
    - app: I've/I have + opened/launched/closed

    Any sentence with a negation or inability before the verb (e.g. can't, cannot, won't,
    will not, don't, do not, unable, never, not able) does NOT produce a claim.
    """
    if not text or not text.strip():
        return []

    # Split into sentence-like segments
    sentences = re.split(r"(?<=[.!?\n])\s+", text.strip())
    claims: list[Claim] = []

    for sentence in sentences:
        s_clean = sentence.strip()
        if not s_clean:
            continue

        # 1. Check for file claim
        m_file = _FILE_RE.search(s_clean)
        if m_file and _FILE_TARGET_RE.search(s_clean):
            verb_start = m_file.start(2)
            prefix = s_clean[:verb_start]
            if not _NEGATION_RE.search(prefix):
                claims.append(Claim(kind="file", phrase=s_clean))
                continue

        # 2. Check for system claim
        m_sys = _SYSTEM_ACTION_RE.search(s_clean)
        if m_sys:
            verb_start = m_sys.start(2)
            prefix = s_clean[:verb_start]
            if not _NEGATION_RE.search(prefix):
                claims.append(Claim(kind="system", phrase=s_clean))
                continue

        # 3. Check for app claim
        m_app = _APP_RE.search(s_clean)
        if m_app:
            verb_start = m_app.start(2)
            prefix = s_clean[:verb_start]
            if not _NEGATION_RE.search(prefix):
                claims.append(Claim(kind="app", phrase=s_clean))
                continue

        # 3. Check for memory claim
        m_mem = _MEMORY_RE.search(s_clean)
        if m_mem:
            verb_start = m_mem.start(2)
            prefix = s_clean[:verb_start]
            if not _NEGATION_RE.search(prefix):
                claims.append(Claim(kind="memory", phrase=s_clean))
                continue

    return claims


def unverified_claims(claims: Sequence[Claim], events: Sequence[Any]) -> list[Claim]:
    """Identify claims whose family had no successful tool execution in the current turn."""
    successful_tools: set[str] = set()

    for ev in events:
        tool_name = None
        status = None

        if hasattr(ev, "tool") and hasattr(ev, "status"):
            tool_name = ev.tool
            status = ev.status
        elif isinstance(ev, dict):
            tool_name = ev.get("tool") or ev.get("name")
            status = ev.get("status")
        elif isinstance(ev, (tuple, list)):
            tool_name = ev[0]
            if len(ev) >= 4:
                status = ev[2]
            elif len(ev) == 3:
                if ev[2] in ("success", "error", "denied", "blocked"):
                    status = ev[2]
                elif not str(ev[2]).startswith("ERROR"):
                    status = "success"
                else:
                    status = "error"
            elif len(ev) == 2:
                status = ev[1]

        if tool_name and status == "success":
            successful_tools.add(str(tool_name))

    unverified: list[Claim] = []
    for claim in claims:
        family = FAMILIES.get(claim.kind, set())
        if not (family & successful_tools):
            unverified.append(claim)

    return unverified
