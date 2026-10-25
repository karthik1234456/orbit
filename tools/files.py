"""File system tools for ORBIT with strict path traversal and workspace jail defenses."""

from pathlib import Path
import re
import shutil
from typing import Optional, Union

from security.permissions import PermissionLevel
from tools.registry import Tool

_MAX_FILE_SIZE = 100 * 1024  # 100 KB limit for reads and writes
_MAX_LIST_ENTRIES = 100       # Maximum directory items to display


def resolve_safe(workspace_root: Union[str, Path], relative_path: Union[str, Path]) -> Path:
    """Resolve a relative path against workspace_root ensuring it stays within boundaries.

    Enforces workspace jail guarantees:
    - Rejects empty paths or paths containing null bytes.
    - Rejects absolute paths and paths with Windows drive letters (e.g. 'C:', '/etc/passwd').
    - Rejects path traversal segments ('..' or '../' or '..\\\\').
    - Resolves symlinks and verifies target is inside workspace_root using is_relative_to().

    Raises:
        ValueError: If any traversal attempt, escape, or invalid path syntax is detected.
    """
    if not isinstance(relative_path, (str, Path)):
        raise ValueError(f"Path must be a string or Path, got {type(relative_path).__name__}")

    path_str = str(relative_path).strip()
    if not path_str:
        raise ValueError("Path cannot be empty.")

    if "\x00" in path_str:
        raise ValueError("Null bytes are not allowed in paths.")

    # Reject drive letters (e.g. 'C:', 'd:/...')
    if re.match(r"^[a-zA-Z]:", path_str):
        raise ValueError(f"Drive letters are not permitted in relative paths: '{path_str}'")

    # Reject absolute paths (starting with '/' or '\\' or Path.is_absolute)
    if path_str.startswith(("/", "\\")) or Path(path_str).is_absolute():
        raise ValueError(f"Absolute paths are not permitted: '{path_str}'")

    # Reject any '..' traversal segments regardless of slash direction
    normalized = path_str.replace("\\", "/")
    segments = normalized.split("/")
    if any(segment == ".." for segment in segments):
        raise ValueError(f"Path traversal using '..' segments is not permitted: '{path_str}'")

    root = Path(workspace_root).resolve()
    target = (root / normalized).resolve()

    # Verify that the target is strictly inside workspace_root (also blocks symlink escapes)
    if not target.is_relative_to(root):
        raise ValueError(f"Path '{path_str}' escapes workspace root boundary '{root}'")

    return target


def _format_precheck_path_error(exc: Exception) -> str:
    """Format path resolution error for prechecks, teaching relative workspace paths."""
    msg = str(exc).lower()
    if "drive letters" in msg or "absolute" in msg or "escapes" in msg:
        return "ERROR: use a path relative to the workspace, or '.' for the workspace root (for example 'notes')."
    return f"ERROR: {exc}"


def make_file_tools(workspace_root: Union[str, Path]) -> list[Tool]:
    """Create and return file management tools scoped strictly to workspace_root."""
    root = Path(workspace_root).resolve()

    def precheck_list_files(args: dict) -> Optional[str]:
        raw_path = args.get("path")
        path = raw_path.strip() if isinstance(raw_path, str) and raw_path.strip() else "."
        try:
            target = resolve_safe(root, path)
        except Exception as exc:
            return _format_precheck_path_error(exc)
        if not target.exists():
            return f"ERROR: Path '{path}' does not exist."
        if not target.is_dir():
            return f"ERROR: Path '{path}' is a file, not a directory."
        return None

    def precheck_read_file(args: dict) -> Optional[str]:
        path = args.get("path")
        try:
            target = resolve_safe(root, path)
        except Exception as exc:
            return _format_precheck_path_error(exc)
        if not target.exists():
            return f"ERROR: File '{path}' does not exist."
        if target.is_dir():
            return f"ERROR: Path '{path}' is a directory, not a file."
        if target.is_file() and target.stat().st_size > _MAX_FILE_SIZE:
            return f"ERROR: File '{path}' exceeds maximum allowed size of 100 KB ({target.stat().st_size} bytes)."
        return None

    def precheck_create_folder(args: dict) -> Optional[str]:
        path = args.get("path")
        try:
            target = resolve_safe(root, path)
        except Exception as exc:
            return _format_precheck_path_error(exc)
        if target.exists() and not target.is_dir():
            return f"ERROR: Cannot create folder '{path}': a file already exists at this path."
        return None

    def precheck_create_file(args: dict) -> Optional[str]:
        path = args.get("path")
        content = args.get("content", "")
        try:
            target = resolve_safe(root, path)
        except Exception as exc:
            return _format_precheck_path_error(exc)
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > _MAX_FILE_SIZE:
                return f"ERROR: Content exceeds maximum allowed size of 100 KB ({len(content_bytes)} bytes)."
        if target.exists():
            return f"ERROR: File '{path}' already exists (refusing to overwrite)."
        return None

    def precheck_move_file(args: dict) -> Optional[str]:
        source = args.get("source")
        destination = args.get("destination")
        try:
            src = resolve_safe(root, source)
        except Exception as exc:
            msg = str(exc).lower()
            if "drive letters" in msg or "absolute" in msg or "escapes" in msg:
                return "ERROR: use a path relative to the workspace, or '.' for the workspace root (for example 'notes')."
            return f"ERROR: Invalid source path: {exc}"
        try:
            dst = resolve_safe(root, destination)
        except Exception as exc:
            msg = str(exc).lower()
            if "drive letters" in msg or "absolute" in msg or "escapes" in msg:
                return "ERROR: use a path relative to the workspace, or '.' for the workspace root (for example 'notes')."
            return f"ERROR: Invalid destination path: {exc}"
        if not src.exists():
            return f"ERROR: Source file '{source}' does not exist."
        if dst.exists():
            return f"ERROR: Destination '{destination}' already exists (refusing to overwrite)."
        return None

    def list_files(path: str = ".") -> str:
        """List contents of a directory within the workspace, marking folders."""
        path_clean = path.strip() if isinstance(path, str) and path.strip() else "."
        target = resolve_safe(root, path_clean)
        if not target.exists():
            return f"ERROR: Path '{path_clean}' does not exist."
        if not target.is_dir():
            return f"ERROR: Path '{path_clean}' is a file, not a directory."

        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        if not entries:
            return "(empty directory)"

        capped_entries = entries[:_MAX_LIST_ENTRIES]
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in capped_entries]
        if len(entries) > _MAX_LIST_ENTRIES:
            lines.append(f"... and {len(entries) - _MAX_LIST_ENTRIES} more entries (output capped at {_MAX_LIST_ENTRIES})")

        return "\n".join(lines)

    def read_file(path: str) -> str:
        """Read UTF-8 text from a workspace file (max 100 KB)."""
        target = resolve_safe(root, path)
        if not target.exists():
            return f"ERROR: File '{path}' does not exist."
        if target.is_dir():
            return f"ERROR: Path '{path}' is a directory, not a file."

        size = target.stat().st_size
        if size > _MAX_FILE_SIZE:
            return f"ERROR: File '{path}' exceeds maximum allowed size of 100 KB ({size} bytes)."

        try:
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            return f"ERROR: File '{path}' appears to be a binary file and cannot be read as UTF-8 text."

        return content

    def create_folder(path: str) -> str:
        """Create a directory and any intermediate parents in the workspace."""
        target = resolve_safe(root, path)
        if target.exists():
            if target.is_dir():
                return f"Folder '{path}' already exists."
            return f"ERROR: Cannot create folder '{path}': a file already exists at this path."

        target.mkdir(parents=True, exist_ok=True)
        return f"Folder '{path}' created successfully."

    def create_file(path: str, content: str) -> str:
        """Create a new file in the workspace. Refuses to overwrite existing files."""
        target = resolve_safe(root, path)
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _MAX_FILE_SIZE:
            return f"ERROR: Content exceeds maximum allowed size of 100 KB ({len(content_bytes)} bytes)."

        if target.exists():
            return f"ERROR: File '{path}' already exists (refusing to overwrite)."

        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

        return f"File '{path}' created successfully."

    def move_file(source: str, destination: str) -> str:
        """Move or rename a file within the workspace. Refuses to overwrite existing destination."""
        src = resolve_safe(root, source)
        dst = resolve_safe(root, destination)

        if not src.exists():
            return f"ERROR: Source file '{source}' does not exist."
        if dst.exists():
            return f"ERROR: Destination '{destination}' already exists (refusing to overwrite)."

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"File moved from '{source}' to '{destination}' successfully."

    return [
        Tool(
            name="list_files",
            description=(
                "List files and folders within a directory in the workspace. "
                "All paths must be relative to the workspace root (defaults to '.'). "
                "Folders are indicated with a trailing slash ('/'). Output is capped at 100 entries. "
                "If the user does not name a folder, pass '.'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to list, relative to the workspace root (default is '.').",
                    }
                },
                "required": [],
            },
            func=list_files,
            permission=PermissionLevel.SAFE,
            precheck=precheck_list_files,
        ),
        Tool(
            name="read_file",
            description=(
                "Call this to check whether a file exists and to read it. Never claim a file is missing without calling this or list_files. "
                "Read UTF-8 text from a file in the workspace. "
                "All paths must be relative to the workspace root. "
                "Maximum file size is 100 KB. Returns an error for binary or oversized files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file to read within the workspace.",
                    }
                },
                "required": ["path"],
            },
            func=read_file,
            permission=PermissionLevel.SAFE,
            untrusted_output=True,
            precheck=precheck_read_file,
        ),
        Tool(
            name="create_folder",
            description=(
                "Create a new folder and any missing parent directories in the workspace. "
                "All paths must be relative to the workspace root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the folder to create in the workspace.",
                    }
                },
                "required": ["path"],
            },
            func=create_folder,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_create_folder,
        ),
        Tool(
            name="create_file",
            description=(
                "Creates a file with the exact name the user gave. "
                "Create a new text file with UTF-8 content in the workspace. "
                "All paths must be relative to the workspace root. "
                "Refuses to overwrite existing files. Content must not exceed 100 KB."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file to create within the workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "UTF-8 text content to write into the file (max 100 KB).",
                    },
                },
                "required": ["path", "content"],
            },
            func=create_file,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_create_file,
        ),
        Tool(
            name="move_file",
            description=(
                "Move or rename a file within the workspace. "
                "Both source and destination paths must be relative to the workspace root. "
                "Refuses to overwrite existing destination files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Relative path of the source file to move.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Relative path of the destination file.",
                    },
                },
                "required": ["source", "destination"],
            },
            func=move_file,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_move_file,
        ),
    ]

