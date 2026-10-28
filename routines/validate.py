"""Validation for named routines, names, and allowable steps.

Routines can only consist of pre-approved safe and confirmation tools:
- open_application, close_application
- open_website, search_site
- open_workspace_folder
- browser_click_result, browser_media, browser_scroll, browser_history, browser_read_page, browser_close
- wait

All file system modifications, arbitrary code, memories, reminders,
and shell commands are strictly refused.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from config.apps import ALLOWED_APPS
from config.sites import SITES, resolve_site
from core.session_state import SessionState

ALLOWED_ROUTINE_TOOLS: frozenset[str] = frozenset({
    "open_application",
    "close_application",
    "open_website",
    "search_site",
    "open_workspace_folder",
    "browser_click_result",
    "browser_media",
    "browser_scroll",
    "browser_history",
    "browser_read_page",
    "browser_close",
    "wait",
})

ALLOWED_TOOLS = ALLOWED_ROUTINE_TOOLS

MAX_STEPS: int = 10
MAX_ROUTINE_STEPS: int = MAX_STEPS
MAX_WAIT_SECONDS: int = 60
MAX_TOTAL_WAIT_SECONDS: int = MAX_WAIT_SECONDS

FORBIDDEN_NAME_STARTS: frozenset[str] = frozenset({
    "all", "list", "show", "delete", "remove", "create", "make", "save", "run", "start",
    "open", "launch", "play", "search", "click", "scroll", "pause", "resume",
    "mute", "unmute", "go", "back", "forward", "reload", "refresh", "close",
    "come", "read", "wait", "next", "previous", "home", "shorts", "look",
    "restart", "fire", "bring", "pull", "find",
})

_DISALLOWED_NAME_CHARS = set(";&|`$><\\")
_NAME_VALID_PATTERN = re.compile(r"^[A-Za-z0-9 _-]+$")


class RoutineError(Exception):
    """Base exception for routine operations."""
    pass


class RoutineLimit(RoutineError):
    """Raised when routine storage capacity limit is reached."""
    pass


class RoutineValidationError(RoutineError, ValueError):
    """Raised when routine name or steps fail validation."""
    pass


class RoutineExistsError(RoutineError):
    """Raised when attempting to save an existing routine without overwrite=True."""
    pass


def normalize_routine_name(name: str) -> str:
    """Normalize routine name: lowercase, whitespace collapsed."""
    if not name or not isinstance(name, str):
        return ""
    return " ".join(name.strip().split()).lower()


def validate_routine_name(name: str) -> str:
    """Validate a proposed routine name.

    Rules:
    - 1 to 30 characters after trimming.
    - Letters, digits, spaces, hyphen, and underscore only.
    - Must not start with a command verb or reserved words (all, list, show, delete, create, make, save, run, start).
    - No disallowed shell characters or URLs.

    Returns the normalized routine name.
    """
    if not isinstance(name, str):
        raise RoutineValidationError("Routine name must be a non-empty string.")

    trimmed = name.strip()
    if not (1 <= len(trimmed) <= 30):
        raise RoutineValidationError(
            f"Routine name must be between 1 and 30 characters (got {len(trimmed)})."
        )

    if any(ch in _DISALLOWED_NAME_CHARS for ch in trimmed) or "://" in trimmed or not _NAME_VALID_PATTERN.match(trimmed):
        raise RoutineValidationError(
            "Routine name may only contain letters, digits, spaces, hyphens, and underscores."
        )

    first_word = trimmed.split()[0].lower()
    if first_word in FORBIDDEN_NAME_STARTS:
        raise RoutineValidationError(
            f"Routine name must not start with command word '{first_word}'."
        )

    # Check if the name contains candidate chaining conjunctions like 'and delete'
    words = trimmed.lower().split()
    for idx, w in enumerate(words[:-1]):
        if w in ("and", "then"):
            next_w = words[idx + 1]
            if next_w in FORBIDDEN_NAME_STARTS:
                raise RoutineValidationError(
                    f"Routine name cannot contain chained command '{w} {next_w}'."
                )

    return normalize_routine_name(trimmed)


def validate_routine_steps(
    steps: list[dict[str, Any]],
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
    state: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Validate and sanitize a sequence of routine steps.

    Guarantees:
    - At least 1 and at most 10 steps.
    - Only tools in ALLOWED_ROUTINE_TOOLS are accepted; all others refused.
    - Sum of wait steps is at most 60 seconds.
    - Apps must be keys in ALLOWED_APPS (or passed allowed_apps).
    - Sites must be valid keys in SITES (or passed sites).
    - No URLs permitted in stored steps; converted to explicit site references.
    - State-dependent "close it" is resolved to the explicit app or refused.
    - Unknown fields are dropped; returns structured list of {tool, args}.
    """
    if not steps or not isinstance(steps, list):
        raise RoutineValidationError("Routine must contain at least 1 step.")

    if len(steps) > 10:
        raise RoutineValidationError(
            f"Routines can have at most 10 steps (got {len(steps)})."
        )

    # Setup allowlists
    apps_map = allowed_apps if allowed_apps is not None else ALLOWED_APPS
    clean_apps: dict[str, dict[str, Any]] = {}
    for k, v in apps_map.items():
        k_clean = k.strip().lower()
        if isinstance(v, Mapping):
            clean_apps[k_clean] = {
                "launch": list(v.get("launch", [])),
                "process": v.get("process"),
            }
        elif isinstance(v, (list, tuple)):
            clean_apps[k_clean] = {
                "launch": list(v),
                "process": v[0] if v else None,
            }
        else:
            clean_apps[k_clean] = {"launch": [], "process": None}

    sites_map = sites if sites is not None else SITES

    # Clone state for step simulation
    sim_state = (
        SessionState(
            _last_site=state._last_site,
            _last_query=state._last_query,
            _last_app=state._last_app,
            _last_folder=state._last_folder,
            _last_url=state._last_url,
            _last_tool=state._last_tool,
            browser_controlled=getattr(state, "browser_controlled", False),
        )
        if isinstance(state, SessionState)
        else SessionState()
    )

    validated_steps: list[dict[str, Any]] = []
    total_wait_s = 0

    for idx, raw_step in enumerate(steps, 1):
        if not isinstance(raw_step, dict):
            raise RoutineValidationError(f"Step {idx} must be a dictionary.")

        tool = str(raw_step.get("tool") or raw_step.get("name") or "").strip()
        args = raw_step.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        for k, v in args.items():
            if k == "url" or (isinstance(v, str) and "://" in v):
                raise RoutineValidationError("URLs are not allowed in routine arguments.")

        if tool not in ALLOWED_ROUTINE_TOOLS:
            raise RoutineValidationError(
                f"Tool '{tool}' is not allowed in routines. Allowed actions are: "
                "open/close apps, open websites, search sites, workspace folders, browser navigation, and wait."
            )

        clean_args: dict[str, Any] = {}

        if tool == "open_application":
            raw_app = str(args.get("name") or args.get("app_name") or "").strip().lower()
            if not raw_app or raw_app not in clean_apps:
                raise RoutineValidationError(
                    f"The app '{args.get('name') or args.get('app_name', '')}' is not in the allowed list (no longer in the allowed list)."
                )
            clean_args = {"name": raw_app}
            sim_state._last_app = raw_app
            sim_state._last_tool = "open_application"

        elif tool == "close_application":
            raw_app = str(args.get("name") or args.get("app_name") or "").strip().lower()
            if raw_app in ("it", "the app", "this app", ""):
                if sim_state._last_app:
                    raw_app = sim_state._last_app
                else:
                    raise RoutineValidationError("name the app explicitly")

            if not raw_app or raw_app not in clean_apps:
                raise RoutineValidationError(
                    f"The app '{args.get('name') or args.get('app_name', raw_app)}' is not in the allowed list (no longer in the allowed list)."
                )

            entry = clean_apps[raw_app]
            if entry.get("process") is None:
                raise RoutineValidationError(f"ORBIT is not allowed to close {raw_app}.")

            clean_args = {"name": raw_app}
            sim_state._last_app = raw_app
            sim_state._last_tool = "close_application"

        elif tool == "open_website":
            raw_site = str(args.get("site") or args.get("site_name") or "").strip()
            resolved = resolve_site(raw_site)
            if not resolved or resolved not in sites_map:
                raise RoutineValidationError(
                    f"The site '{raw_site}' is not recognized / no longer in the allowed list."
                )
            clean_site = resolved.lower()
            clean_args = {"site": clean_site}
            sim_state._last_site = clean_site
            sim_state._last_tool = "open_website"

        elif tool == "search_site":
            raw_site = str(args.get("site") or args.get("site_name") or "").strip()
            resolved = resolve_site(raw_site)
            if not resolved or resolved not in sites_map:
                raise RoutineValidationError(
                    f"The site '{raw_site}' is not recognized / no longer in the allowed list."
                )
            clean_site = resolved.lower()

            query = str(args.get("query", "")).strip()
            if not query:
                raise RoutineValidationError("Search query cannot be empty (1-8 words required).")
            if "://" in query:
                raise RoutineValidationError("Search query cannot contain URLs.")

            words = query.split()
            if not (1 <= len(words) <= 8):
                raise RoutineValidationError(
                    f"Search query must be 1 to 8 words (got {len(words)})."
                )

            clean_args = {"site": clean_site, "query": query}
            sim_state._last_site = clean_site
            sim_state._last_query = query
            sim_state._last_tool = "search_site"

        elif tool == "open_workspace_folder":
            raw_path = str(args.get("path", ".")).strip() or "."
            if any(ch in "; & | ` $ < >" for ch in raw_path):
                raise RoutineValidationError("Folder path contains forbidden characters.")
            if ".." in raw_path or raw_path.startswith("/") or (len(raw_path) > 1 and raw_path[1] == ":"):
                raise RoutineValidationError("Folder path must be relative to workspace.")
            clean_args = {"path": raw_path}
            sim_state._last_folder = raw_path
            sim_state._last_tool = "open_workspace_folder"

        elif tool == "browser_click_result":
            next_vid = bool(args.get("next_video", False))
            if next_vid:
                clean_args = {"next_video": True}
            else:
                try:
                    n = int(args.get("n", 1))
                except (ValueError, TypeError):
                    n = 1
                if not (1 <= n <= 10):
                    raise RoutineValidationError(f"Result click index must be 1 to 10 (got {n}).")
                clean_args = {"n": n}
            sim_state._last_tool = "browser_click_result"

        elif tool == "browser_media":
            action = str(args.get("action", "")).strip().lower()
            if action not in ("pause", "play", "mute", "unmute"):
                raise RoutineValidationError(
                    f"Invalid browser media action: '{action}'. Allowed: pause, play, mute, unmute."
                )
            clean_args = {"action": action}
            sim_state._last_tool = "browser_media"

        elif tool == "browser_scroll":
            direction = str(args.get("direction", "down")).strip().lower()
            if direction not in ("up", "down"):
                direction = "down"
            try:
                screens = int(args.get("screens", 1))
            except (ValueError, TypeError):
                screens = 1
            if not (1 <= screens <= 10):
                screens = 1
            clean_args = {"direction": direction, "screens": screens}
            sim_state._last_tool = "browser_scroll"

        elif tool == "browser_history":
            action = str(args.get("action", "back")).strip().lower()
            if action not in ("back", "forward", "reload"):
                raise RoutineValidationError(
                    f"Invalid browser history action: '{action}'. Allowed: back, forward, reload."
                )
            clean_args = {"action": action}
            sim_state._last_tool = "browser_history"

        elif tool == "browser_read_page":
            clean_args = {}
            sim_state._last_tool = "browser_read_page"

        elif tool == "browser_close":
            clean_args = {}
            sim_state._last_tool = "browser_close"

        elif tool == "wait":
            try:
                sec = int(args.get("seconds", 1))
            except (ValueError, TypeError):
                raise RoutineValidationError("Wait seconds must be an integer.")
            if not (1 <= sec <= 60):
                raise RoutineValidationError(
                    f"Wait step must be between 1 and 60 seconds (got {sec})."
                )
            total_wait_s += sec
            clean_args = {"seconds": sec}
            sim_state._last_tool = "wait"

        validated_steps.append({"tool": tool, "args": clean_args})

    if total_wait_s > MAX_WAIT_SECONDS:
        raise RoutineValidationError(
            f"Total wait time in a routine cannot exceed 60 seconds (at most 60 seconds, got {total_wait_s} seconds)."
        )

    return validated_steps
