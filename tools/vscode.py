"""Visual Studio Code integration tool for ORBIT assistant."""

from pathlib import Path
import shutil
import subprocess
from typing import Union

from security.permissions import PermissionLevel
from tools.files import resolve_safe
from tools.registry import Tool


def make_vscode_tool(workspace_root: Union[str, Path]) -> Tool:
    """Create and return the open_in_vscode tool scoped to workspace_root."""
    root = Path(workspace_root).resolve()

    def open_in_vscode(path: str) -> str:
        """Open a file or directory within the workspace in Visual Studio Code."""
        try:
            resolved = resolve_safe(root, path)
        except ValueError as exc:
            return f"ERROR: Invalid path '{path}': {exc}"

        if not resolved.exists():
            return f"ERROR: Path '{path}' does not exist."

        code_path = shutil.which("code")
        if not code_path:
            return "ERROR: VS Code command ('code') was not found in PATH."

        try:
            # Launch strictly using argument list without shell=True
            subprocess.Popen([code_path, str(resolved)], shell=False)
            return f"Opened '{path}' in VS Code."
        except Exception as exc:
            return f"ERROR: Failed to launch VS Code: {exc}"

    return Tool(
        name="open_in_vscode",
        description=(
            "Open a file or directory in Visual Studio Code. "
            "The path must be relative to the workspace root and must already exist."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path of the existing file or directory to open.",
                }
            },
            "required": ["path"],
        },
        func=open_in_vscode,
        permission=PermissionLevel.CONFIRM,
    )
