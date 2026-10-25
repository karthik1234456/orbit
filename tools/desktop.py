"""Desktop tool registration module for ORBIT assistant."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Optional, Union

from config import settings
from security.permissions import PermissionLevel
from tools.apps import make_app_tools
from tools.browser import make_browser_tools as make_classic_browser_tools
from tools.browser_tools import make_browser_tools as make_controller_browser_tools
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
            msg = str(exc).lower()
            if "drive letters" in msg or "absolute" in msg or "escapes" in msg:
                return None, "ERROR: use a path relative to the workspace, or '.' for the workspace root (for example 'notes')."
            return None, f"ERROR: {exc}"

        if not resolved.exists():
            return None, f"ERROR: Folder '{path_str}' does not exist in workspace."
        if not resolved.is_dir():
            return None, f"ERROR: Path '{path_str}' is a file, not a directory."

        return resolved, None

    def precheck_open_workspace_folder(args: dict) -> Optional[str]:
        """Pure precheck validation without launching Windows Explorer."""
        path = args.get("path")
        _, err = _validate_folder_path(path)
        return err

    def open_workspace_folder(path: str = ".") -> str:
        """Open a folder inside the workspace in Windows Explorer."""
        path_str = path.strip() if isinstance(path, str) and path.strip() else "."
        resolved, err = _validate_folder_path(path_str)
        if err:
            return err

        try:
            # Launch Windows Explorer with resolved directory path as an argument list without shell=True
            subprocess.Popen(["explorer.exe", str(resolved)], shell=False)
            return f"Opened folder '{path_str}' in Windows Explorer."
        except Exception as exc:
            return f"ERROR: Failed to launch Windows Explorer: {exc}"

    return Tool(
        name="open_workspace_folder",
        description=(
            "Opens a folder inside ORBIT's workspace in Windows Explorer. "
            "Use this when the user asks to open folders or their files. "
            "If the user does not name a folder, pass '.'."
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


_OMITTED = object()


def is_browser_enabled() -> bool:
    """Return True if managed browser automation is enabled via environment or settings."""
    return settings.browser_enabled()


def register_desktop_tools(
    registry: ToolRegistry,
    workspace_root: Union[str, Path],
    allowed_apps: Mapping[str, Any],
    sites: Optional[Mapping[str, Any]] = None,
    browser_controller: Any = _OMITTED,
    browser_backend_factory: Optional[Callable[[], Any]] = None,
    session_state: Optional[Any] = None,
    state: Optional[Any] = None,
) -> Optional[Any]:
    """Create workspace directory if missing, instantiate file, app, browser, VS Code, and workspace folder tools, and register them."""
    root = Path(workspace_root).resolve()
    # Create the workspace folder if missing
    root.mkdir(parents=True, exist_ok=True)

    file_tools = make_file_tools(root)
    app_tools = make_app_tools(allowed_apps)

    st = state if state is not None else session_state

    if browser_controller is _OMITTED:
        if browser_backend_factory is not None or is_browser_enabled():
            from browser.controller import BrowserController
            controller = BrowserController(backend_factory=browser_backend_factory)
            if st is not None:
                controller.session_state = st
            browser_tools = make_controller_browser_tools(controller, sites=sites, state=st)
        else:
            controller = None
            browser_tools = make_classic_browser_tools(sites=sites)
    elif browser_controller is not None:
        controller = browser_controller
        if st is not None:
            controller.session_state = st
        browser_tools = make_controller_browser_tools(controller, sites=sites, state=st)
    else:
        # Caller passed browser_controller=None explicitly -> "no browser"
        controller = None
        browser_tools = make_classic_browser_tools(sites=sites)

    vscode_tool = make_vscode_tool(root)
    folder_tool = make_open_workspace_folder_tool(root)

    for tool in file_tools + app_tools + browser_tools + [vscode_tool, folder_tool]:
        registry.register(tool)

    return controller

