"""Application launcher and process management tools for ORBIT."""

from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping, Optional, Sequence

import psutil

from security.permissions import PermissionLevel
from tools.registry import Tool


def make_app_tools(allowed_apps: Mapping[str, Any]) -> list[Tool]:
    """Create and return application management tools scoped to an allowlist.

    Guarantees:
    - Only pre-registered application aliases in allowed_apps can be launched or closed.
    - Arbitrary commands, flags, or shell scripts are rejected.
    - Commands are launched strictly via subprocess.Popen with an argument list, never shell=True.
    - If an application has process=None, ORBIT will refuse to close it.
    - Only processes exactly matching the configured process name are gracefully terminated.
    """
    # Normalize entries for case-insensitive and whitespace-tolerant lookups
    clean_apps: dict[str, dict[str, Any]] = {}
    for k, v in allowed_apps.items():
        key = k.strip().lower()
        if isinstance(v, Mapping):
            clean_apps[key] = {
                "launch": list(v.get("launch", [])),
                "process": v.get("process"),
            }
        elif isinstance(v, Sequence):
            clean_apps[key] = {
                "launch": list(v),
                "process": Path(v[0]).name if v else None,
            }
        else:
            clean_apps[key] = {"launch": [], "process": None}

    sorted_names = sorted(clean_apps.keys())
    allowed_list_str = ", ".join(f"'{name}'" for name in sorted_names)

    def _validate_app_name(name: Any) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
        """Validate app name against clean_apps. Returns (clean_name, entry, error)."""
        if not name or not isinstance(name, str):
            return None, None, f"ERROR: Application name must be a valid string. Allowed applications: {allowed_list_str}"

        clean_name = name.strip().lower()
        if clean_name not in clean_apps:
            return None, None, (
                f"ERROR: Application '{name}' is not in the allowed list. "
                f"Allowed applications: {allowed_list_str}"
            )

        entry = clean_apps[clean_name]
        return clean_name, entry, None

    def precheck_open_application(args: dict) -> Optional[str]:
        """Validate open_application arguments without launching the app."""
        name = args.get("name")
        clean_name, entry, err = _validate_app_name(name)
        if err:
            return err
        cmd_args = entry.get("launch", [])
        if not cmd_args:
            return f"ERROR: No command configured for application '{clean_name}'."
        return None

    def precheck_close_application(args: dict) -> Optional[str]:
        """Validate close_application arguments without terminating any process."""
        name = args.get("name")
        clean_name, entry, err = _validate_app_name(name)
        if err:
            return err
        target_process = entry.get("process")
        if target_process is None:
            return f"ORBIT is not allowed to close {clean_name}."
        return None

    def open_application(name: str) -> str:
        """Launch an allowed application using a predefined command list."""
        clean_name, entry, err = _validate_app_name(name)
        if err:
            return err

        cmd_args = entry.get("launch", [])
        if not cmd_args:
            return f"ERROR: No command configured for application '{clean_name}'."

        executable = cmd_args[0]

        try:
            resolved = shutil.which(executable)
            if not resolved:
                raise FileNotFoundError(f"Executable '{executable}' could not be found on system PATH")

            cmd_list = [resolved] + list(cmd_args[1:])
            # Never run with shell=True to prevent command injection
            subprocess.Popen(cmd_list, shell=False)
            return f"Successfully opened {clean_name}."
        except (FileNotFoundError, OSError) as exc:
            return f"ERROR: Failed to launch '{clean_name}': {exc}"

    def close_application(name: str) -> str:
        """Terminate running instances of an allowlisted application gracefully."""
        clean_name, entry, err = _validate_app_name(name)
        if err:
            return err

        target_process = entry.get("process")
        if target_process is None:
            return f"ORBIT is not allowed to close {clean_name}."

        target_proc_name = str(target_process).strip().lower()

        closed_count = 0
        for proc in psutil.process_iter():
            try:
                p_name = proc.name().lower() if proc.name() else ""
                if p_name == target_proc_name:
                    # Graceful termination (SIGTERM equivalent), never forceful kill
                    proc.terminate()
                    closed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if closed_count > 0:
            return f"Closed {closed_count} running instance(s) of {clean_name}."
        return f"No running instances of {clean_name} were found."

    def list_allowed_apps() -> str:
        """Return the list of allowed application names and whether each can be closed."""
        if not clean_apps:
            return "No applications are currently allowed."
        items = []
        for name in sorted_names:
            can_close = clean_apps[name].get("process") is not None
            close_status = "can close" if can_close else "open only, cannot close"
            items.append(f"- {name} ({close_status})")
        return "Allowed applications:\n" + "\n".join(items)

    return [
        Tool(
            name="open_application",
            description=(
                "Open an approved desktop application from the allowed applications list. "
                "The 'name' parameter must match one of the predefined application names (e.g. 'notepad', 'calculator'). "
                "Arbitrary commands, paths, or unapproved applications are rejected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the allowed application to open (e.g. 'notepad', 'calculator').",
                    }
                },
                "required": ["name"],
            },
            func=open_application,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_open_application,
        ),
        Tool(
            name="close_application",
            description=(
                "Close running instances of an approved application from the allowed applications list. "
                "The 'name' parameter must match one of the predefined application names (e.g. 'notepad', 'calculator'). "
                "Arbitrary process names, commands, or unapproved applications are rejected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the allowed application to close (e.g. 'notepad', 'calculator').",
                    }
                },
                "required": ["name"],
            },
            func=close_application,
            permission=PermissionLevel.CONFIRM,
            precheck=precheck_close_application,
        ),
        Tool(
            name="list_allowed_apps",
            description=(
                "List all desktop applications that ORBIT is permitted to launch or close. "
                "Use this tool to discover which applications can be controlled."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            func=list_allowed_apps,
            permission=PermissionLevel.SAFE,
        ),
    ]

