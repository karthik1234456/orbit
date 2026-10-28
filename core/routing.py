"""Tool routing subsystem for ORBIT v2.7.

Provides group-based tool filtering, keyword-based group selection,
fallback heuristics for general chat, and miss recovery inability detection.

Tool routing is an optimization for token budget, latency, and tool selection
precision. It is NOT a security boundary.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional, Sequence

if TYPE_CHECKING:
    from tools.registry import Tool

# Group mappings defined in one centralized location
GROUPS: dict[str, set[str]] = {
    # System utilities: current time, AST math calculations, host system info
    "system": {"get_current_time", "calculate", "system_info"},
    # Quick scratchpad notes
    "notes": {"write_note"},
    # Workspace file and folder operations
    "files": {
        "list_files",
        "read_file",
        "create_file",
        "create_folder",
        "move_file",
        "open_workspace_folder",
        "open_in_vscode",
    },
    # Allowlisted desktop application launching and closing
    "apps": {"open_application", "close_application", "list_allowed_apps"},
    # General web navigation and search
    "web": {"open_website", "search_web", "search_site"},
    # Controlled browser automation (Playwright backend)
    "browser": {
        "browser_click_result",
        "browser_media",
        "browser_scroll",
        "browser_history",
        "browser_read_page",
        "browser_close",
    },
    # Long-term persistent user facts and preferences
    "memory": {"remember_fact", "recall_memories", "list_memories", "forget_memory"},
    # Workspace document ingestion and retrieval (RAG)
    "docs": {"index_documents", "search_documents", "list_documents", "remove_document"},
    # Scheduled reminders and timers
    "reminders": {
        "set_reminder",
        "set_timer",
        "list_reminders",
        "cancel_reminder",
        "cancel_all_reminders",
    },
}

# Reverse lookup mapping tool name to group
TOOL_TO_GROUP: dict[str, str] = {}
for _group_name, _tools in GROUPS.items():
    for _tool_name in _tools:
        TOOL_TO_GROUP[_tool_name] = _group_name

# Generous lowercase stems and phrases matched on word starts
KEYWORDS: dict[str, list[str]] = {
    "system": [
        "time",
        "clock",
        "date",
        "day",
        "calculate",
        "calc",
        "math",
        "arithmetic",
        "compute",
        "percent",
        "percentage",
        "dollar",
        "system",
        "ram",
        "cpu",
        "disk",
        "memory usage",
        "hardware",
        "spec",
        "specs",
        "battery",
        "os",
        "machine",
        "noon",
        "midnight",
        "plus",
        "minus",
        "multiply",
        "divide",
        "sum",
        "total",
    ],
    "notes": [
        "note",
        "notes",
        "jot",
        "memo",
        "scratchpad",
        "write note",
        "take note",
        "quick note",
    ],
    "files": [
        "file",
        "files",
        "folder",
        "folders",
        "directory",
        "dir",
        "dirs",
        "create",
        "make",
        "save",
        "write",
        "read",
        "move",
        "trow",
        "throw",
        "rename",
        "list",
        "show",
        "workspace",
        "trash",
        "archive",
        "archived",
        "open folder",
        "show files",
        "open in vscode",
        "vscode",
        "my files",
    ],
    "apps": [
        "app",
        "apps",
        "application",
        "applications",
        "launch",
        "start",
        "run",
        "open app",
        "close app",
        "kill",
        "fire up",
        "program",
        "programs",
        "calc",
        "calculator",
        "notepad",
        "mspaint",
        "paint",
        "explorer",
        "whatsapp",
        "chrome",
        "vscode",
        "allowed",
        "allowed apps",
    ],
    "web": [
        "open",
        "go to",
        "visit",
        "site",
        "website",
        "search",
        "look up",
        "find",
        "google",
        "news",
        "weather",
        "price",
        "score",
        "youtube",
        "wikipedia",
        "github",
        "maps",
        "gmail",
        "inbox",
        "browse",
        "web",
        "online",
        "url",
        "http",
        "https",
        "www",
        "internet",
    ],
    "browser": [
        "click",
        "scroll",
        "video",
        "videos",
        "media",
        "play",
        "pause",
        "resume",
        "rewind",
        "fast forward",
        "mute",
        "unmute",
        "volume",
        "tab",
        "tabs",
        "page",
        "read page",
        "history",
        "back",
        "forward",
        "browser",
        "shut down browser",
        "close browser",
        "results page",
        "article",
    ],
    "memory": [
        "remember",
        "reme",
        "remeber",
        "recall",
        "memory",
        "memories",
        "forget",
        "scratch",
        "scratch that",
        "keep in mind",
        "prefer",
        "preference",
        "preferences",
        "favorite",
        "favourite",
        "about myself",
        "told you",
        "remind you",
        "did i tell you",
    ],
    "docs": [
        "document",
        "documents",
        "doc",
        "docs",
        "notes",
        "pdf",
        "pdfs",
        "policy",
        "policies",
        "search my",
        "what did my",
        "index",
        "indexed",
        "indexing",
        "knowledge base",
        "knowledgebase",
        "rag",
        "downloads",
    ],
    "reminders": [
        "remind",
        "reminder",
        "reminders",
        "timer",
        "timers",
        "alarm",
        "alarms",
        "snooze",
        "schedule",
        "ping me",
        "ping",
        "alert",
        "alerts",
        "due",
        "countdown",
    ],
}

# Inability phrasing patterns for miss recovery
INABILITY_PATTERNS = [
    re.compile(r"\bcan't\b", re.IGNORECASE),
    re.compile(r"\bcannot\b", re.IGNORECASE),
    re.compile(r"\bunable\b", re.IGNORECASE),
    re.compile(r"\bdon't have the ability\b", re.IGNORECASE),
    re.compile(r"\bi'm not able\b", re.IGNORECASE),
    re.compile(r"\bi am not able\b", re.IGNORECASE),
    re.compile(r"\bdo not have access\b", re.IGNORECASE),
    re.compile(r"\bno way to\b", re.IGNORECASE),
]

FALLBACK_GROUPS: set[str] = {"system", "web", "memory", "docs"}


def _normalize_text(text: str) -> str:
    """Lowercase text and replace punctuation with spaces."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(cleaned.split())


def match_keywords(text: str) -> set[str]:
    """Identify groups matching keyword stems or phrases in text."""
    norm = _normalize_text(text)
    words = norm.split()
    matched = set()

    for group, kw_list in KEYWORDS.items():
        hit = False
        for kw in kw_list:
            kw_clean = _normalize_text(kw)
            if " " in kw_clean:
                # Multi-word phrase matching
                if kw_clean in norm:
                    hit = True
                    break
            else:
                # Single-word stem matching (matched on word starts)
                if any(w.startswith(kw_clean) for w in words):
                    hit = True
                    break
        if hit:
            matched.add(group)
    return matched


def select_groups(
    text: str,
    prior_tools: Optional[Sequence[str]] = None,
    state: Optional[Any] = None,
) -> set[str]:
    """Select the set of tool groups relevant to the user's text and context.

    Includes:
    - Every group with a keyword hit
    - Groups of prior_tools (continuity)
    - 'browser' when session state indicates a controlled browser page is open
    - 'web' when 'browser' is selected
    - Always 'system'
    - Fallback set {'system', 'web', 'memory', 'docs'} if no group other than 'system' matched
    """
    matched = match_keywords(text)

    # Continuity from prior tools
    if prior_tools:
        for t in prior_tools:
            grp = TOOL_TO_GROUP.get(t)
            if grp:
                matched.add(grp)

    # Browser state check
    if state is not None:
        has_page = bool(getattr(state, "last_url", None) or getattr(state, "last_site", None))
        is_controlled = getattr(state, "browser_controlled", False) is True
        if (has_page and is_controlled) or getattr(state, "browser_open", False) or getattr(state, "is_browser_open", False):
            matched.add("browser")

    # If browser is selected, always include web
    if "browser" in matched:
        matched.add("web")

    # Check if any group OTHER than 'system' matched
    other_than_system = matched - {"system"}
    if not other_than_system:
        return set(FALLBACK_GROUPS)

    matched.add("system")
    return matched


def offered_tools(
    registry_tools: Sequence[Tool],
    groups: set[str],
) -> list[Tool]:
    """Filter registry tools to those in the selected groups, plus unmapped tools.

    - Registered tools only; unknown group names ignored.
    - Tools not in any group in GROUPS are ALWAYS offered so new tools are never silently hidden.
    - Preserves registry order.
    """
    allowed_tool_names: set[str] = set()
    for g in groups:
        if g in GROUPS:
            allowed_tool_names.update(GROUPS[g])

    all_grouped_tools: set[str] = set().union(*GROUPS.values())

    result: list[Tool] = []
    for tool in registry_tools:
        if tool.name in allowed_tool_names:
            result.append(tool)
        elif tool.name not in all_grouped_tools:
            # Ungrouped tools are always offered
            result.append(tool)

    return result


def matches_inability(text: str) -> bool:
    """Return True if the text matches inability phrasing for miss recovery."""
    if not text:
        return False
    lower = text.lower()
    for pattern in INABILITY_PATTERNS:
        if pattern.search(lower):
            return True
    return False
