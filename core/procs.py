"""Safe process inspection and termination utilities for ORBIT."""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional, Sequence, Union

import psutil

logger = logging.getLogger(__name__)

SAFE_PROCESS_ERRORS = (
    psutil.AccessDenied,
    psutil.NoSuchProcess,
    psutil.ZombieProcess,
    PermissionError,
    OSError,
)


def safe_name(proc: Any) -> str:
    """Return process name safely, or empty string on error."""
    try:
        if hasattr(proc, "info") and isinstance(proc.info, dict) and proc.info.get("name"):
            return str(proc.info["name"])
        if hasattr(proc, "name") and callable(proc.name):
            return str(proc.name() or "")
        if hasattr(proc, "name") and isinstance(proc.name, str):
            return str(proc.name)
        return ""
    except SAFE_PROCESS_ERRORS:
        return ""


def safe_cmdline(proc: Any) -> list[str]:
    """Return process command line list safely, or empty list on error."""
    try:
        if hasattr(proc, "cmdline") and callable(proc.cmdline):
            res = proc.cmdline()
            if isinstance(res, (list, tuple)):
                return [str(x) for x in res]
        if hasattr(proc, "info") and isinstance(proc.info, dict):
            info_cmd = proc.info.get("cmdline")
            if isinstance(info_cmd, (list, tuple)):
                return [str(x) for x in info_cmd]
        if hasattr(proc, "cmdline") and isinstance(proc.cmdline, (list, tuple)):
            return [str(x) for x in proc.cmdline]
        return []
    except SAFE_PROCESS_ERRORS:
        return []


def safe_exe(proc: Any) -> str:
    """Return process executable path safely, or empty string on error."""
    try:
        if hasattr(proc, "exe") and callable(proc.exe):
            return str(proc.exe() or "")
        if hasattr(proc, "info") and isinstance(proc.info, dict) and proc.info.get("exe"):
            return str(proc.info["exe"])
        if hasattr(proc, "exe") and isinstance(proc.exe, str):
            return str(proc.exe)
        return ""
    except SAFE_PROCESS_ERRORS:
        return ""


def iter_processes(
    name_contains: Optional[Union[str, Sequence[str]]] = None,
    cmdline_contains: Optional[str] = None,
) -> Iterator[Any]:
    """Iterate running processes safely without raising permissions or OS errors.

    - Uses psutil.process_iter(["pid", "name"]).
    - Checks process name first against name_contains (case-insensitive).
    - Reads cmdline or exe lazily only for processes whose name matched.
    - Skips any process that raises psutil.AccessDenied, NoSuchProcess, ZombieProcess,
      PermissionError or OSError.
    - Never raises.
    """
    targets: list[str] = []
    if name_contains:
        if isinstance(name_contains, str):
            targets = [name_contains.lower()]
        else:
            targets = [str(t).lower() for t in name_contains]

    cmdline_target = cmdline_contains.lower() if cmdline_contains else None

    try:
        proc_iter = psutil.process_iter(["pid", "name"])
    except SAFE_PROCESS_ERRORS:
        return

    for proc in proc_iter:
        try:
            pname = safe_name(proc).lower()
            if targets:
                matched = any(t in pname or pname in t for t in targets)
                if not matched:
                    continue

            if cmdline_target is not None:
                raw_cmd = safe_cmdline(proc)
                cmdline_str = " ".join(str(x) for x in raw_cmd).lower()
                if cmdline_target not in cmdline_str:
                    continue

            yield proc
        except SAFE_PROCESS_ERRORS:
            continue


def terminate_gently(proc: Any) -> bool:
    """Terminate a process gracefully and wait up to 3 seconds. Never kills."""
    try:
        if hasattr(proc, "terminate") and callable(proc.terminate):
            proc.terminate()
        elif hasattr(proc, "send_signal"):
            proc.send_signal(15)
        else:
            return False

        if hasattr(proc, "wait") and callable(proc.wait):
            try:
                proc.wait(timeout=3.0)
            except (psutil.TimeoutExpired, TimeoutError):
                pass
        return True
    except SAFE_PROCESS_ERRORS:
        return False
