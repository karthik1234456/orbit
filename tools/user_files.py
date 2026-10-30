"""User file search, open, and reveal tools with strict path and extension guards.

Provides deterministic file discovery in user search roots (Documents, Downloads, Desktop,
Pictures, Videos, Music) and safe opening of verified file types via session-stored indices.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Optional

from config import settings
from config.apps import SEARCH_FOLDER_NAMES
from config.paths import is_demo_mode
from core.session_state import SessionState
from security.permissions import PermissionLevel
from tools.registry import Tool

# Strictly allowed file extensions
ALLOWED_EXTENSIONS: set[str] = {
    "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "txt", "md", "csv", "rtf",
    "png", "jpg", "jpeg", "gif", "bmp", "webp",
    "mp3", "wav", "flac", "m4a",
    "mp4", "mkv", "avi", "mov", "webm",
}

# Categories of extensions
KIND_EXTENSIONS: dict[str, set[str]] = {
    "any": ALLOWED_EXTENSIONS,
    "document": {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "txt", "md", "csv", "rtf"},
    "image": {"png", "jpg", "jpeg", "gif", "bmp", "webp"},
    "video": {"mp4", "mkv", "avi", "mov", "webm"},
    "audio": {"mp3", "wav", "flac", "m4a"},
    "pdf": {"pdf"},
}

# Forbidden extensions explicitly barred
FORBIDDEN_EXTENSIONS: set[str] = {
    "exe", "msi", "bat", "cmd", "ps1", "vbs", "js", "jar", "lnk", "url",
    "scr", "com", "reg", "hta", "dll", "py", "docm", "xlsm", "pptm",
}

# Regex to detect invalid query characters (wildcards, separators, glob chars)
_DISALLOWED_QUERY_CHARS = re.compile(r"[*?/\\[\];:|`<>$]")

# Regex to detect zero-width and bidirectional-override unicode characters
_SUSPICIOUS_NAME_CHARS = re.compile(
    r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069]"
)


def _format_size(size_bytes: int) -> str:
    """Format file size in human-readable units."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _format_date(mtime: float) -> str:
    """Format modification date as YYYY-MM-DD."""
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def default_search_roots() -> list[Path]:
    """Discover existing default user search roots."""
    roots: list[Path] = []
    user_home = Path.home()

    # 1. Standard folders under user home
    for name in SEARCH_FOLDER_NAMES:
        folder = (user_home / name).resolve()
        if folder.is_dir() and folder not in roots:
            roots.append(folder)

    # 2. OneDrive folders if present
    one_drive = os.getenv("OneDrive")
    if one_drive:
        od_path = Path(one_drive).resolve()
        if od_path.is_dir():
            for name in SEARCH_FOLDER_NAMES:
                folder = (od_path / name).resolve()
                if folder.is_dir() and folder not in roots:
                    roots.append(folder)

    return roots


def validate_file_safety(file_path: Path, search_roots: list[Path]) -> Optional[str]:
    """Validate that file exists, is inside search roots, and meets extension and name rules.

    Returns None if valid, or an error string if invalid.
    """
    raw_name = file_path.name
    if raw_name.endswith(" ") or raw_name.endswith("."):
        return "Refused: file names with trailing spaces or dots cannot be opened."
    if ":" in raw_name:
        return "Refused: file names containing colons cannot be opened."

    try:
        resolved = file_path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return f"File '{file_path.name}' no longer exists."

    # 1. Must be a regular file, not a directory or symlink / reparse point
    if not resolved.is_file():
        return f"'{resolved.name}' is not a regular file."
    if resolved.is_symlink():
        return "Reparse points and symbolic links cannot be opened."

    # Check reparse point on Windows
    if sys.platform == "win32":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(resolved))
            FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
            if attrs != 0xFFFFFFFF and (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
                return "Reparse points and junctions cannot be opened."
        except Exception:
            pass

    # 2. Must be strictly within one of the search roots
    inside_root = any(
        resolved.is_relative_to(root.resolve())
        for root in search_roots
        if root.is_dir()
    )
    if not inside_root:
        return "File is outside allowed search roots."

    filename = resolved.name

    # 3. Trailing space or dot refusal
    if filename.endswith(" ") or filename.endswith("."):
        return "File names with trailing spaces or dots cannot be opened."

    # 4. Colon refusal
    if ":" in filename:
        return "File names containing colons cannot be opened."

    # 5. Zero-width or bidi-override characters refusal
    if _SUSPICIOUS_NAME_CHARS.search(filename):
        return "Refused: file names containing special characters or directional formatting cannot be opened."

    # 6. Double extensions refusal (more than one dot)
    if filename.count(".") > 1:
        return "Refused: files with multiple or double extensions are not allowed."

    # 7. Check extension allowlist
    parts = filename.rsplit(".", 1)
    if len(parts) < 2:
        return "Files without an extension cannot be opened."
    ext = parts[1].lower()

    if ext in FORBIDDEN_EXTENSIONS or ext not in ALLOWED_EXTENSIONS:
        return f"Files with extension '.{ext}' cannot be opened for security reasons."

    return None


def make_user_file_tools(
    session_state_getter: Callable[[], Optional[SessionState]],
    search_roots_getter: Optional[Callable[[], list[Path]]] = None,
    opener: Optional[Callable[[str], bool]] = None,
    revealer: Optional[Callable[[list[str]], bool]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> list[Tool]:
    """Create and return the user file search, open, and reveal tools."""
    get_roots = search_roots_getter or default_search_roots
    get_time = clock or time.monotonic

    def _default_opener(path_str: str) -> bool:
        if sys.platform == "win32":
            os.startfile(path_str)
            return True
        return False

    def _default_revealer(cmd_args: list[str]) -> bool:
        import subprocess
        subprocess.Popen(cmd_args, shell=False)
        return True

    actual_opener = opener or _default_opener
    actual_revealer = revealer or _default_revealer

    def _is_enabled() -> bool:
        if is_demo_mode():
            return False
        return bool(getattr(settings, "FILES_ENABLED", True))

    def _extract_args(pos_args: tuple, kwargs: dict) -> dict[str, Any]:
        if pos_args and isinstance(pos_args[0], dict):
            return pos_args[0]
        return dict(kwargs)

    # --- find_files ---

    def _find_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "File search is off. Turn on FILES_ENABLED in Settings."
        return None

    def _find_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "File search is off. Turn on FILES_ENABLED in Settings."

        query = str(args.get("query", "")).strip()
        kind = str(args.get("kind", "any")).strip().lower()
        recent = bool(args.get("recent", False))

        if kind not in KIND_EXTENSIONS:
            kind = "any"
        target_exts = KIND_EXTENSIONS[kind]

        if query:
            if _DISALLOWED_QUERY_CHARS.search(query):
                return "Query contains invalid characters. Use letters and numbers only."
            words = query.split()
            if len(words) > 5:
                return "Query too long. Use 1 to 5 words."

        roots = get_roots()
        if not roots:
            return "No search folders found."

        # If searching specifically for downloads
        is_download_search = "download" in query.lower() or (recent and not query and kind == "any")
        if is_download_search:
            dl_roots = [r for r in roots if "download" in r.name.lower()]
            if dl_roots:
                roots = dl_roots

        matched_entries: list[tuple[Path, os.stat_result]] = []
        entries_scanned = 0
        max_entries = 20000
        start_time = get_time()
        max_duration = 2.0  # 2 second search budget

        clean_query_lower = query.lower()

        for root in roots:
            if not root.is_dir():
                continue

            # Walk up to depth 4 using os.scandir
            stack = [(str(root), 0)]
            while stack and entries_scanned < max_entries:
                if (get_time() - start_time) > max_duration:
                    break

                curr_dir, depth = stack.pop()
                try:
                    with os.scandir(curr_dir) as it:
                        for entry in it:
                            entries_scanned += 1
                            if entries_scanned >= max_entries:
                                break

                            # Skip hidden and system files/dirs
                            name = entry.name
                            if name.startswith("."):
                                continue

                            # Skip symlinks and junctions without following
                            try:
                                if entry.is_symlink():
                                    continue
                            except OSError:
                                continue

                            if sys.platform == "win32":
                                try:
                                    st = entry.stat(follow_symlinks=False)
                                    FILE_ATTRIBUTE_HIDDEN = 0x02
                                    FILE_ATTRIBUTE_SYSTEM = 0x04
                                    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
                                    attrs = getattr(st, "st_file_attributes", 0)
                                    if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_REPARSE_POINT):
                                        continue
                                except Exception:
                                    pass

                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    if depth < 4:
                                        stack.append((entry.path, depth + 1))
                                elif entry.is_file(follow_symlinks=False):
                                    # Check extension
                                    parts = name.rsplit(".", 1)
                                    if len(parts) == 2:
                                        ext = parts[1].lower()
                                        if ext in target_exts and name.count(".") == 1:
                                            # Match query
                                            if not clean_query_lower or clean_query_lower in name.lower():
                                                st = entry.stat(follow_symlinks=False)
                                                matched_entries.append((Path(entry.path), st))
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError):
                    continue

        if not matched_entries:
            return "No matching files found."

        # Sort results: if recent sort by mtime descending, else sort by mtime descending by default
        matched_entries.sort(key=lambda item: item[1].st_mtime, reverse=True)
        top_entries = matched_entries[:5]

        # Store in session state (absolute path strings, memory only)
        stored_paths = [str(p.resolve()) for p, _ in top_entries]
        state = session_state_getter()
        if state is not None:
            state.set_found_files(stored_paths)

        lines: list[str] = []
        for idx, (path, st) in enumerate(top_entries, 1):
            folder_name = path.parent.name
            size_str = _format_size(st.st_size)
            date_str = _format_date(st.st_mtime)
            lines.append(f"{idx}. {path.name} ({folder_name}, {size_str}, {date_str})")

        return "\n".join(lines)

    find_files_tool = Tool(
        name="find_files",
        description="Search for documents, downloads, and media files across user search folders.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to match in file name (1-5 words)."},
                "kind": {
                    "type": "string",
                    "enum": ["any", "document", "image", "video", "audio", "pdf"],
                    "description": "File category filter.",
                },
                "recent": {"type": "boolean", "description": "Sort newest files first."},
                "_free_text": {"type": "boolean", "description": "Internal flag."},
            },
        },
        func=_find_exec,
        permission=PermissionLevel.SAFE,
        precheck=_find_precheck,
        model_visible=False,
    )

    # --- open_found_file ---

    def _open_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "File search is off. Turn on FILES_ENABLED in Settings."
        args = _extract_args(pos_args, kwargs)
        try:
            idx = int(args.get("index", 1))
        except (ValueError, TypeError):
            return "File index must be a number between 1 and 5."
        if idx < 1 or idx > 5:
            return "File index must be between 1 and 5."

        state = session_state_getter()
        candidates = state.get_found_files() if state else []
        if not candidates or idx > len(candidates):
            return f"No search result #{idx} available. Run a file search first."

        target_path = Path(candidates[idx - 1])
        roots = get_roots()
        safety_err = validate_file_safety(target_path, roots)
        if safety_err:
            return f"ERROR: {safety_err}"
        return None

    def _open_describe(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        idx = int(args.get("index", 1))
        state = session_state_getter()
        candidates = state.get_found_files() if state else []
        if candidates and 1 <= idx <= len(candidates):
            p = Path(candidates[idx - 1])
            return f"Open '{p.name}' from '{p.parent.name}'?"
        return f"Open file #{idx}?"

    def _open_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "File search is off. Turn on FILES_ENABLED in Settings."
        idx = int(args.get("index", 1))
        state = session_state_getter()
        candidates = state.get_found_files() if state else []
        if not candidates or idx < 1 or idx > len(candidates):
            return f"No search result #{idx} available. Run a file search first."

        target_path = Path(candidates[idx - 1])
        roots = get_roots()
        safety_err = validate_file_safety(target_path, roots)
        if safety_err:
            return f"ERROR: {safety_err}"

        ok = actual_opener(str(target_path.resolve()))
        if ok:
            return f"Opened {target_path.name}."
        return f"Could not open {target_path.name}."

    open_found_file_tool = Tool(
        name="open_found_file",
        description="Open a file from the recent search results by its 1-based index (1 to 5).",
        parameters={
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Result number (1 to 5)."},
            },
            "required": ["index"],
        },
        func=_open_exec,
        permission=PermissionLevel.CONFIRM,
        precheck=_open_precheck,
        describe=_open_describe,
        model_visible=False,
    )

    # --- reveal_found_file ---

    def _reveal_precheck(*pos_args: Any, **kwargs: Any) -> Optional[str]:
        if not _is_enabled():
            return "File search is off. Turn on FILES_ENABLED in Settings."
        args = _extract_args(pos_args, kwargs)
        try:
            idx = int(args.get("index", 1))
        except (ValueError, TypeError):
            return "File index must be a number between 1 and 5."
        if idx < 1 or idx > 5:
            return "File index must be between 1 and 5."

        state = session_state_getter()
        candidates = state.get_found_files() if state else []
        if not candidates or idx > len(candidates):
            return f"No search result #{idx} available. Run a file search first."

        target_path = Path(candidates[idx - 1])
        roots = get_roots()
        safety_err = validate_file_safety(target_path, roots)
        if safety_err:
            return f"ERROR: {safety_err}"
        return None

    def _reveal_describe(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        idx = int(args.get("index", 1))
        state = session_state_getter()
        candidates = state.get_found_files() if state else []
        if candidates and 1 <= idx <= len(candidates):
            p = Path(candidates[idx - 1])
            return f"Reveal '{p.name}' in File Explorer?"
        return f"Reveal file #{idx} in File Explorer?"

    def _reveal_exec(*pos_args: Any, **kwargs: Any) -> str:
        args = _extract_args(pos_args, kwargs)
        if not _is_enabled():
            return "File search is off. Turn on FILES_ENABLED in Settings."
        idx = int(args.get("index", 1))
        state = session_state_getter()
        candidates = state.get_found_files() if state else []
        if not candidates or idx < 1 or idx > len(candidates):
            return f"No search result #{idx} available. Run a file search first."

        target_path = Path(candidates[idx - 1])
        roots = get_roots()
        safety_err = validate_file_safety(target_path, roots)
        if safety_err:
            return f"ERROR: {safety_err}"

        cmd = ["explorer.exe", f"/select,{target_path.resolve()}"]
        actual_revealer(cmd)
        return f"Revealed {target_path.name} in folder."

    reveal_found_file_tool = Tool(
        name="reveal_found_file",
        description="Reveal a file from the recent search results in File Explorer.",
        parameters={
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Result number (1 to 5)."},
            },
            "required": ["index"],
        },
        func=_reveal_exec,
        permission=PermissionLevel.CONFIRM,
        precheck=_reveal_precheck,
        describe=_reveal_describe,
        model_visible=False,
    )

    return [find_files_tool, open_found_file_tool, reveal_found_file_tool]

