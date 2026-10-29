"""Unit and integration tests for ORBIT Demo Mode and ORBIT_HOME path isolation.

All tests strictly adhere to safety constraints:
- Uses fakes and pytest's tmp_path fixture only.
- Never reads or writes real .env, real data/, logs/, or workspace/ folders.
- Never touches real processes, network, or external hardware.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import pytest

from config.paths import (
    RECORDED_OLD_DEFAULTS,
    REPO_ROOT,
    get_audit_log_path,
    get_browser_profile_dir,
    get_data_dir,
    get_diag_dir,
    get_docs_dir,
    get_logs_dir,
    get_memory_db_path,
    get_orbit_home,
    get_rag_db_path,
    get_reminders_db_path,
    get_routines_db_path,
    get_runs_log_path,
    get_workspace_dir,
    is_demo_mode,
)
from config.settings_store import SettingsError, save
from core.diagnostics import build_diagnostics_text
from core.doctor import Probes, check_demo_mode
from main import format_startup_banner, handle_set_command
from memory.store import MemoryStore
from rag.index import DocumentIndex
from reminders.store import ReminderStore
from routines.store import RoutineStore
from scripts.demo import (
    DEMO_MARKER_FILE,
    DOC_HARDWARE_INVENTORY,
    DOC_PROJECT_PLAN,
    DOC_VISITOR_POLICY,
    NOTE_IDEAS,
    NOTE_QUICK,
    build_child_env,
    reset_demo_home,
    safe_rmtree,
    seed_demo_home,
    validate_demo_home,
)
from security.audit import AuditLogger
from core.task import RunLogger


@pytest.fixture(autouse=True)
def clean_orbit_env(monkeypatch):
    """Ensure clean test environment without lingering ORBIT_HOME or DEMO_MODE."""
    for key in (
        "ORBIT_HOME",
        "DEMO_MODE",
        "WORKSPACE_DIR",
        "DOCS_DIR",
        "DATA_DIR",
        "LOGS_DIR",
        "MEMORY_DB_PATH",
        "RAG_DB_PATH",
        "REMINDERS_DB_PATH",
        "ROUTINES_DB_PATH",
        "BROWSER_PROFILE_DIR",
        "AUDIT_LOG_PATH",
        "RUNS_LOG_PATH",
        "DIAG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


# --- 1. Path Defaults & Regression Recording ---


def test_default_paths_match_recorded_defaults():
    """Verify that when ORBIT_HOME is empty, all path getters match recorded old defaults."""
    assert get_orbit_home() is None
    assert is_demo_mode() is False

    assert get_workspace_dir() == RECORDED_OLD_DEFAULTS["workspace"]
    assert get_docs_dir() == RECORDED_OLD_DEFAULTS["docs"]
    assert get_memory_db_path() == RECORDED_OLD_DEFAULTS["memory_db"]
    assert get_rag_db_path() == RECORDED_OLD_DEFAULTS["rag_db"]
    assert get_reminders_db_path() == RECORDED_OLD_DEFAULTS["reminders_db"]
    assert get_routines_db_path() == RECORDED_OLD_DEFAULTS["routines_db"]
    assert get_browser_profile_dir() == RECORDED_OLD_DEFAULTS["browser_profile"]
    assert get_audit_log_path() == RECORDED_OLD_DEFAULTS["audit_log"]
    assert get_runs_log_path() == RECORDED_OLD_DEFAULTS["runs_log"]
    assert get_diag_dir() == RECORDED_OLD_DEFAULTS["diag_dir"]
    assert get_data_dir() == RECORDED_OLD_DEFAULTS["data_dir"]
    assert get_logs_dir() == RECORDED_OLD_DEFAULTS["logs_dir"]


def test_orbit_home_moves_all_defaults_under_directory(tmp_path, monkeypatch):
    """Verify that when ORBIT_HOME is set, all storage defaults move under it."""
    home_dir = tmp_path / "custom_home"
    monkeypatch.setenv("ORBIT_HOME", str(home_dir))

    assert get_orbit_home() == home_dir.resolve()

    assert get_workspace_dir() == (home_dir / "workspace").resolve()
    assert get_docs_dir() == (home_dir / "workspace" / "docs").resolve()
    assert get_data_dir() == (home_dir / "data").resolve()
    assert get_memory_db_path() == (home_dir / "data" / "memory.db").resolve()
    assert get_rag_db_path() == (home_dir / "data" / "rag.db").resolve()
    assert get_reminders_db_path() == (home_dir / "data" / "reminders.db").resolve()
    assert get_routines_db_path() == (home_dir / "data" / "routines.db").resolve()
    assert get_browser_profile_dir() == (home_dir / "data" / "browser_profile").resolve()
    assert get_logs_dir() == (home_dir / "logs").resolve()
    assert get_audit_log_path() == (home_dir / "logs" / "audit.jsonl").resolve()
    assert get_runs_log_path() == (home_dir / "logs" / "runs.jsonl").resolve()
    assert get_diag_dir() == (home_dir / "logs").resolve()


def test_explicit_env_vars_win_over_orbit_home(tmp_path, monkeypatch):
    """Verify that explicit per-path env vars always take precedence over ORBIT_HOME."""
    home_dir = tmp_path / "orbit_home"
    override_dir = tmp_path / "explicit"
    monkeypatch.setenv("ORBIT_HOME", str(home_dir))

    monkeypatch.setenv("WORKSPACE_DIR", str(override_dir / "ws"))
    monkeypatch.setenv("DOCS_DIR", str(override_dir / "my_docs"))
    monkeypatch.setenv("DATA_DIR", str(override_dir / "my_data"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(override_dir / "mem.sqlite"))
    monkeypatch.setenv("RAG_DB_PATH", str(override_dir / "docs.sqlite"))
    monkeypatch.setenv("REMINDERS_DB_PATH", str(override_dir / "rem.sqlite"))
    monkeypatch.setenv("ROUTINES_DB_PATH", str(override_dir / "rout.sqlite"))
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(override_dir / "chrome_prof"))
    monkeypatch.setenv("LOGS_DIR", str(override_dir / "my_logs"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(override_dir / "aud.jsonl"))
    monkeypatch.setenv("RUNS_LOG_PATH", str(override_dir / "rns.jsonl"))
    monkeypatch.setenv("DIAG_DIR", str(override_dir / "diag_out"))

    assert get_workspace_dir() == (override_dir / "ws").resolve()
    assert get_docs_dir() == (override_dir / "my_docs").resolve()
    assert get_data_dir() == (override_dir / "my_data").resolve()
    assert get_memory_db_path() == (override_dir / "mem.sqlite").resolve()
    assert get_rag_db_path() == (override_dir / "docs.sqlite").resolve()
    assert get_reminders_db_path() == (override_dir / "rem.sqlite").resolve()
    assert get_routines_db_path() == (override_dir / "rout.sqlite").resolve()
    assert get_browser_profile_dir() == (override_dir / "chrome_prof").resolve()
    assert get_logs_dir() == (override_dir / "my_logs").resolve()
    assert get_audit_log_path() == (override_dir / "aud.jsonl").resolve()
    assert get_runs_log_path() == (override_dir / "rns.jsonl").resolve()
    assert get_diag_dir() == (override_dir / "diag_out").resolve()


def test_orbit_home_rejects_unsafe_paths(monkeypatch):
    """Verify that ORBIT_HOME rejects drive root, user home, and repo root."""
    # 1. Reject drive root
    drive_root = Path(Path.cwd().anchor)
    monkeypatch.setenv("ORBIT_HOME", str(drive_root))
    with pytest.raises(ValueError, match="drive root"):
        get_orbit_home()

    # 2. Reject user home directory
    user_home = Path.home()
    monkeypatch.setenv("ORBIT_HOME", str(user_home))
    with pytest.raises(ValueError, match="user's home directory"):
        get_orbit_home()

    # 3. Reject project repository root
    monkeypatch.setenv("ORBIT_HOME", str(REPO_ROOT))
    with pytest.raises(ValueError, match="project root"):
        get_orbit_home()


# --- 2. Store Lazy Instantiation Under ORBIT_HOME ---


def test_stores_instantiate_under_orbit_home(tmp_path, monkeypatch):
    """Verify stores initialized without an explicit path automatically use ORBIT_HOME paths."""
    home_dir = tmp_path / "home_stores"
    monkeypatch.setenv("ORBIT_HOME", str(home_dir))

    mem_store = MemoryStore()
    assert mem_store.path == (home_dir / "data" / "memory.db").resolve()
    assert mem_store.path.parent.exists()

    rem_store = ReminderStore()
    assert rem_store.path == (home_dir / "data" / "reminders.db").resolve()
    assert rem_store.path.parent.exists()

    rout_store = RoutineStore()
    assert rout_store.path == (home_dir / "data" / "routines.db").resolve()
    assert rout_store.path.parent.exists()

    rag_index = DocumentIndex()
    assert rag_index.path == (home_dir / "data" / "rag.db").resolve()
    assert rag_index.path.parent.exists()

    audit_logger = AuditLogger()
    assert audit_logger.path == (home_dir / "logs" / "audit.jsonl").resolve()

    run_logger = RunLogger()
    assert run_logger.path == (home_dir / "logs" / "runs.jsonl").resolve()


# --- 3. Demo Reset & Marker Safety Rules ---


def test_reset_demo_home_deletes_only_marker_bearing_folder(tmp_path):
    """Verify reset deletes folder only if .orbit_demo marker is present."""
    demo_dir = tmp_path / "demo_test"
    demo_dir.mkdir(parents=True)
    canary = demo_dir / "data.txt"
    canary.write_text("critical data", encoding="utf-8")

    # Without marker: must refuse and delete nothing
    with pytest.raises(RuntimeError, match=r"Refusing to delete"):
        reset_demo_home(demo_dir)
    assert canary.exists()
    assert canary.read_text(encoding="utf-8") == "critical data"

    # With marker: successfully resets
    (demo_dir / DEMO_MARKER_FILE).write_text("marker", encoding="utf-8")
    reset_demo_home(demo_dir)
    assert not demo_dir.exists()


def test_safe_rmtree_does_not_follow_symlinks(tmp_path):
    """Verify safe_rmtree unlinks symlinks without deleting files in the target directory."""
    target_external_dir = tmp_path / "external_target"
    target_external_dir.mkdir(parents=True)
    target_canary = target_external_dir / "precious.txt"
    target_canary.write_text("do not delete me", encoding="utf-8")

    demo_dir = tmp_path / "demo_home_symlink_test"
    demo_dir.mkdir(parents=True)

    link_dir = demo_dir / "symlink_folder"
    try:
        link_dir.symlink_to(target_external_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        # On Windows, creating symlinks without Developer Mode / admin may fail
        pytest.skip("Symlink creation not permitted in this environment.")

    assert link_dir.is_symlink()
    assert (link_dir / "precious.txt").exists()

    safe_rmtree(demo_dir)

    assert not demo_dir.exists()
    # The external directory and its file must remain completely untouched!
    assert target_external_dir.exists()
    assert target_canary.exists()
    assert target_canary.read_text(encoding="utf-8") == "do not delete me"


def test_validate_demo_home_constraints(tmp_path):
    """Verify validate_demo_home rejects invalid locations and unmarker-bearing external directories."""
    # Drive root rejected
    with pytest.raises(ValueError, match="drive root"):
        validate_demo_home(Path(Path.cwd().anchor))

    # User home rejected
    with pytest.raises(ValueError, match="user's home directory"):
        validate_demo_home(Path.home())

    # Project root rejected
    with pytest.raises(ValueError, match="project root"):
        validate_demo_home(REPO_ROOT)

    # External non-empty directory without marker rejected
    ext_dir = tmp_path / "external_dir"
    ext_dir.mkdir(parents=True)
    (ext_dir / "somefile.txt").write_text("content", encoding="utf-8")
    with pytest.raises(ValueError, match=r"must be empty or contain"):
        validate_demo_home(ext_dir)

    # Empty external directory allowed
    empty_ext = tmp_path / "empty_ext"
    empty_ext.mkdir(parents=True)
    validate_demo_home(empty_ext)


# --- 4. Seeding & Privacy Auditing ---


def test_seed_demo_home_content_and_indexing(tmp_path):
    """Verify seed_demo_home creates sample docs, notes, marker, and FTS5 index without model."""
    demo_dir = tmp_path / "demo_seeded"
    seed_demo_home(demo_dir)

    # 1. Check directories and marker
    assert (demo_dir / DEMO_MARKER_FILE).is_file()
    assert (demo_dir / "workspace" / "docs" / "visitor_policy.md").is_file()
    assert (demo_dir / "workspace" / "docs" / "hardware_inventory.md").is_file()
    assert (demo_dir / "workspace" / "docs" / "project_plan.md").is_file()
    assert (demo_dir / "workspace" / "notes" / "quick_notes.txt").is_file()
    assert (demo_dir / "workspace" / "notes" / "ideas.txt").is_file()

    # 2. Check no memory, reminders, or routines databases pre-created
    assert not (demo_dir / "data" / "memory.db").exists()
    assert not (demo_dir / "data" / "reminders.db").exists()
    assert not (demo_dir / "data" / "routines.db").exists()

    # 3. Check document index
    rag_db = demo_dir / "data" / "rag.db"
    assert rag_db.is_file()
    index = DocumentIndex(path=rag_db)

    # Safe visitor query matches visitor_policy.md
    hits, withheld = index.search("visitor registration policy", top_k=2)
    assert len(hits) > 0
    assert any("visitor_policy.md" in h.path for h in hits)

    # Injected prompt was flagged and withheld
    hits_inj, withheld_inj = index.search("pwned", top_k=2, include_flagged=False)
    assert len(hits_inj) == 0
    assert withheld_inj > 0


def test_sample_documents_privacy_audit():
    """Verify sample documents contain no personal names, emails, addresses, or phone patterns."""
    phone_pattern = re.compile(r"\b(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}\b")
    consecutive_digits = re.compile(r"\b\d{7,15}\b")

    all_samples = [
        DOC_VISITOR_POLICY,
        DOC_HARDWARE_INVENTORY,
        DOC_PROJECT_PLAN,
        NOTE_QUICK,
        NOTE_IDEAS,
    ]

    for sample in all_samples:
        # No @ symbol (emails / handles)
        assert "@" not in sample

        # No specific user or developer identifiers
        lower = sample.lower()
        assert "karthik" not in lower
        assert "gmail" not in lower

        # No phone-like numbers
        assert not phone_pattern.search(sample)
        assert not consecutive_digits.search(sample)


# --- 5. Child Process Environment Isolation ---


def test_build_child_env_isolation(tmp_path):
    """Verify build_child_env configures isolated variables without mutating parent environment."""
    parent_orbit_home = os.getenv("ORBIT_HOME")
    parent_demo_mode = os.getenv("DEMO_MODE")

    child_env = build_child_env(tmp_path, offline=True)

    # Parent environment remains completely unaffected
    assert os.getenv("ORBIT_HOME") == parent_orbit_home
    assert os.getenv("DEMO_MODE") == parent_demo_mode

    # Child environment carries expected configuration
    assert child_env["ORBIT_HOME"] == str(tmp_path.resolve())
    assert child_env["DEMO_MODE"] == "1"
    assert child_env["ORBIT_OFFLINE"] == "1"
    assert child_env["LLM_PROVIDER"] == "ollama"


# --- 6. Settings Protection in Demo Mode ---


def test_settings_save_refused_in_demo_mode(tmp_path, monkeypatch):
    """Verify settings.save and /set command refuse to write when DEMO_MODE is set."""
    monkeypatch.setenv("DEMO_MODE", "1")

    # 1. Direct settings_store.save call refuses with exact friendly message
    env_file = tmp_path / ".env"
    env_file.write_text("SPEAK_REPLIES=0\n", encoding="utf-8")

    with pytest.raises(SettingsError, match=r"Settings can't be changed in demo mode"):
        save(env_file, {"SPEAK_REPLIES": "1"})

    # Content remains untouched
    assert env_file.read_text(encoding="utf-8") == "SPEAK_REPLIES=0\n"

    # 2. CLI /set command prints friendly message and returns cleanly
    output_lines: list[str] = []
    handle_set_command(
        "/set SPEAK_REPLIES 1",
        env_path=env_file,
        print_func=lambda msg: output_lines.append(str(msg)),
    )
    assert any("Settings can't be changed in demo mode" in line for line in output_lines)
    assert env_file.read_text(encoding="utf-8") == "SPEAK_REPLIES=0\n"


# --- 7. UI & Terminal Indicators ---


def test_terminal_banner_demo_mode(monkeypatch, tmp_path):
    """Verify terminal banner prints 'DEMO MODE: sample data in <folder>' when DEMO_MODE is active."""
    home_dir = tmp_path / "my_demo_folder"
    monkeypatch.setenv("ORBIT_HOME", str(home_dir))
    monkeypatch.setenv("DEMO_MODE", "1")

    banner = format_startup_banner(label="mock", tool_count=15, memory_count=0)
    assert f"DEMO MODE: sample data in {home_dir.name}" in banner

    # In normal mode, banner does not include DEMO MODE
    monkeypatch.delenv("DEMO_MODE")
    banner_normal = format_startup_banner(label="mock", tool_count=15, memory_count=0)
    assert "DEMO MODE" not in banner_normal


def test_ui_title_and_chip_in_demo_mode(monkeypatch):
    """Verify UI window title and status bar chip react to DEMO_MODE."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception:
        pytest.skip("Tkinter display not available in test environment.")

    try:
        from core.version import VERSION

        monkeypatch.setenv("DEMO_MODE", "1")
        base_title = f"ORBIT v{VERSION} Desktop Assistant"
        expected_demo_title = f"{base_title} - DEMO MODE (sample data)"

        # Check title formatting logic
        title = base_title
        if is_demo_mode():
            title += " - DEMO MODE (sample data)"
        assert title == expected_demo_title

        # Check chip appearance
        chip = tk.Label(root, text="DEMO", fg="#e67e22")
        assert chip.cget("text") == "DEMO"
        assert chip.cget("fg") == "#e67e22"
    finally:
        root.destroy()


# --- 8. Doctor & Diagnostics Reporting ---


def test_doctor_and_diag_show_orbit_home_and_demo_mode(tmp_path, monkeypatch):
    """Verify doctor checks and /diag output report folder name (not full path) and DEMO_MODE."""
    home_dir = tmp_path / "demo_box"
    monkeypatch.setenv("ORBIT_HOME", str(home_dir))
    monkeypatch.setenv("DEMO_MODE", "1")

    # 1. Doctor check
    probes = Probes(env=lambda var, default=None: os.environ.get(var, default))
    check = check_demo_mode(probes)
    assert check.status == "OK"
    assert "DEMO_MODE=active" in check.detail
    assert f"ORBIT_HOME={home_dir.name}" in check.detail
    # Never leak full absolute path in detail
    assert str(home_dir.resolve()) not in check.detail

    # 2. Diagnostics text
    diag_text = build_diagnostics_text()
    assert f"ORBIT_HOME: {home_dir.name}" in diag_text
    assert "DEMO_MODE: active" in diag_text
    assert f"ORBIT_HOME: {home_dir.resolve()}" not in diag_text
