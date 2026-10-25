"""Desktop tool registration module for ORBIT assistant."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Mapping, Optional, Union

from security.permissions import PermissionLevel
from tools.apps import make_app_tools
from tools.browser import make_browser_tools
from tools.files import make_file_tools, resolve_safe
from tools.registry import Tool, ToolRegistry
from tools.vscode import make_vscode_tool


def make_open_workspace_folder_tool(workspace_root: Union[str, Path]) -> Tool:
    """Create and return the open_workspace_folder tool scoped to workspace_root."""
    root = Path(workspace_root).resolve()

    def _validate_folder_path(path: Any) -> tuple[Optional[Path], Optional[str]]:
        """Validate that path is within workspace, exists, and is a directory."""
        if path is None:
            path_str = "."
        elif isinstance(path, str):
            path_str = path.strip() or "."
        else:
            return None, f"ERROR: Path must be a string, got {type(path).__name__}"

        try:
            resolved = resolve_safe(root, path_str)
        except Exception as exc:
            return None, f"ERROR: {exc}"

        if not resolved.exists():
            return None, f"ERROR: Folder '{path_str}' does not exist in workspace."
        if not resolved.is_dir():
            return None, f"ERROR: Path '{path_str}' is a file, not a directory."

        return resolved, None

    def precheck_open_workspace_folder(args: dict) -> Optional[str]:
        """Pure precheck validation without launching Windows Explorer."""
        path = args.get("path", ".")
        _, err = _validate_folder_path(path)
        return err

    def open_workspace_folder(path: str = ".") -> str:
        """Open a folder inside the workspace in Windows Explorer."""
        resolved, err = _validate_folder_path(path)
        if err:
            return err

        try:
            # Launch Windows Explorer with resolved directory path as an argument list without shell=True
            subprocess.Popen(["explorer.exe", str(resolved)], shell=False)
            return f"Opened folder '{path}' in Windows Explorer."
        except Exception as exc:
            return f"ERROR: Failed to launch Windows Explorer: {exc}"

    return Tool(
        name="open_workspace_folder",
        description=(
            "Opens a folder inside ORBIT's workspace in Windows Explorer. "
            "Use this when the user asks to open folders or their files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path of the folder within the workspace to open (default is '.').",
                }
            },
            "required": [],
        },
        func=open_workspace_folder,
        permission=PermissionLevel.CONFIRM,
        precheck=precheck_open_workspace_folder,
    )


def register_desktop_tools(
    registry: ToolRegistry,
    workspace_root: Union[str, Path],
    allowed_apps: Mapping[str, Any],
    sites: Optional[Mapping[str, Any]] = None,
) -> None:
    """Create workspace directory if missing, instantiate file, app, browser, VS Code, and workspace folder tools, and register them."""
    root = Path(workspace_root).resolve()
    # Create the workspace folder if missing
    root.mkdir(parents=True, exist_ok=True)

    file_tools = make_file_tools(root)
    app_tools = make_app_tools(allowed_apps)
    browser_tools = make_browser_tools(sites)
    vscode_tool = make_vscode_tool(root)
    folder_tool = make_open_workspace_folder_tool(root)

    for tool in file_tools + app_tools + browser_tools + [vscode_tool, folder_tool]:
        registry.register(tool)

