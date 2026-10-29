"""Centralized filesystem path resolution and isolation management for ORBIT.

Provides resolution logic for storage directories, SQLite database files, and
logs, supporting normal execution and isolated demo environments via ORBIT_HOME.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def is_demo_mode() -> bool:
    """Return True if DEMO_MODE environment variable is enabled."""
    val = os.getenv("DEMO_MODE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def get_orbit_home() -> Optional[Path]:
    """Return resolved ORBIT_HOME directory, or None if unset or empty.

    Raises:
        ValueError: If ORBIT_HOME is set to a drive root, the user's home
                    directory, or the project root.
    """
    raw = os.getenv("ORBIT_HOME")
    if not raw or not raw.strip():
        return None

    home = Path(raw.strip()).resolve()

    # Reject drive root (e.g. C:\ or /)
    if home.parent == home or home == Path(home.anchor):
        raise ValueError(f"ORBIT_HOME cannot be a drive root: '{home}'")

    # Reject user's home directory
    if home == Path.home().resolve():
        raise ValueError(f"ORBIT_HOME cannot be the user's home directory: '{home}'")

    # Reject project root
    if home == REPO_ROOT.resolve():
        raise ValueError(f"ORBIT_HOME cannot be the project root: '{home}'")

    return home


def get_workspace_dir() -> Path:
    """Return resolved workspace directory.

    Precedence: WORKSPACE_DIR env var > ORBIT_HOME / workspace > workspace.
    """
    explicit = os.getenv("WORKSPACE_DIR")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    home = get_orbit_home()
    if home is not None:
        return (home / "workspace").resolve()
    return (REPO_ROOT / "workspace").resolve()


def get_docs_dir() -> Path:
    """Return resolved documents directory.

    Precedence: DOCS_DIR env var > workspace_dir / docs.
    """
    explicit = os.getenv("DOCS_DIR")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_workspace_dir() / "docs").resolve()


def get_data_dir() -> Path:
    """Return resolved data directory.

    Precedence: DATA_DIR env var > ORBIT_HOME / data > data.
    """
    explicit = os.getenv("DATA_DIR")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    home = get_orbit_home()
    if home is not None:
        return (home / "data").resolve()
    return (REPO_ROOT / "data").resolve()


def get_memory_db_path() -> Path:
    """Return resolved memory SQLite database path.

    Precedence: MEMORY_DB_PATH env var > data_dir / memory.db.
    """
    explicit = os.getenv("MEMORY_DB_PATH")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_data_dir() / "memory.db").resolve()


def get_rag_db_path() -> Path:
    """Return resolved document FTS5 SQLite database path.

    Precedence: RAG_DB_PATH env var > data_dir / rag.db.
    """
    explicit = os.getenv("RAG_DB_PATH")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_data_dir() / "rag.db").resolve()


def get_reminders_db_path() -> Path:
    """Return resolved reminders SQLite database path.

    Precedence: REMINDERS_DB_PATH env var > data_dir / reminders.db.
    """
    explicit = os.getenv("REMINDERS_DB_PATH")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_data_dir() / "reminders.db").resolve()


def get_routines_db_path() -> Path:
    """Return resolved routines SQLite database path.

    Precedence: ROUTINES_DB_PATH env var > data_dir / routines.db.
    """
    explicit = os.getenv("ROUTINES_DB_PATH")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_data_dir() / "routines.db").resolve()


def get_browser_profile_dir() -> Path:
    """Return resolved browser profile directory.

    Precedence: BROWSER_PROFILE_DIR env var > data_dir / browser_profile.
    """
    explicit = os.getenv("BROWSER_PROFILE_DIR")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_data_dir() / "browser_profile").resolve()


def get_logs_dir() -> Path:
    """Return resolved logs directory.

    Precedence: LOGS_DIR env var > ORBIT_HOME / logs > logs.
    """
    explicit = os.getenv("LOGS_DIR")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    home = get_orbit_home()
    if home is not None:
        return (home / "logs").resolve()
    return (REPO_ROOT / "logs").resolve()


def get_audit_log_path() -> Path:
    """Return resolved audit log JSONL file path.

    Precedence: AUDIT_LOG_PATH env var > logs_dir / audit.jsonl.
    """
    explicit = os.getenv("AUDIT_LOG_PATH")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_logs_dir() / "audit.jsonl").resolve()


def get_runs_log_path() -> Path:
    """Return resolved task runs log JSONL file path.

    Precedence: RUNS_LOG_PATH env var > logs_dir / runs.jsonl.
    """
    explicit = os.getenv("RUNS_LOG_PATH")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return (get_logs_dir() / "runs.jsonl").resolve()


def get_diag_dir() -> Path:
    """Return resolved diagnostics report output directory.

    Precedence: DIAG_DIR env var > logs_dir.
    """
    explicit = os.getenv("DIAG_DIR")
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    return get_logs_dir()


RECORDED_OLD_DEFAULTS: dict[str, Path] = {
    "workspace": (REPO_ROOT / "workspace").resolve(),
    "docs": (REPO_ROOT / "workspace" / "docs").resolve(),
    "memory_db": (REPO_ROOT / "data" / "memory.db").resolve(),
    "rag_db": (REPO_ROOT / "data" / "rag.db").resolve(),
    "reminders_db": (REPO_ROOT / "data" / "reminders.db").resolve(),
    "routines_db": (REPO_ROOT / "data" / "routines.db").resolve(),
    "browser_profile": (REPO_ROOT / "data" / "browser_profile").resolve(),
    "audit_log": (REPO_ROOT / "logs" / "audit.jsonl").resolve(),
    "runs_log": (REPO_ROOT / "logs" / "runs.jsonl").resolve(),
    "diag_dir": (REPO_ROOT / "logs").resolve(),
    "data_dir": (REPO_ROOT / "data").resolve(),
    "logs_dir": (REPO_ROOT / "logs").resolve(),
}

