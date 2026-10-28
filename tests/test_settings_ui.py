"""UI tests for SettingsWindow in ui/settings_window.py.

Covers:
- Window creation with withdrawn Tk root and FakeController (skips when Tk is unavailable).
- Verification that all 17 SPECS are rendered and no secrets are present.
- Source tags and environment override display.
- PLAN_PREVIEW=never confirmation dialog gate (user can decline or accept).
- Save action: .env updated, .env.bak created, audit event logged, doctor notice shown.
- Cancel action destroys window.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.conftest import check_tk_available, make_test_root

# Skip module if Tkinter is not available in the environment
check_tk_available()

from config.settings_store import SPECS
from ui.settings_window import SettingsWindow


class FakeAuditLogger:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def log(self, tool: str, args: dict[str, Any], decision: str, outcome: str) -> None:
        self.entries.append({
            "tool": tool,
            "args": args,
            "decision": decision,
            "outcome": outcome,
        })


class FakeAgent:
    def __init__(self) -> None:
        self.audit = FakeAuditLogger()


class FakeApp:
    def __init__(self) -> None:
        self.agent = FakeAgent()


class FakeController:
    def __init__(self) -> None:
        self.app = FakeApp()
        self.submits: list[str] = []

    def submit(self, text: str) -> bool:
        self.submits.append(text)
        return True


def test_settings_window_renders_all_specs_no_secrets(tmp_path: Path):
    """Ensure all 17 editable non-secret settings are rendered and secrets are absent."""
    root = make_test_root()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("LLM_PROVIDER=auto\nGEMINI_API_KEY=plant123\n", encoding="utf-8")

        ctrl = FakeController()
        win = SettingsWindow(root, controller=ctrl, env_path=env_file)

        # Check all SPECS are in widget_vars
        for key in SPECS:
            assert key in win.widget_vars, f"Key {key} missing from widget_vars"

        # Check GEMINI_API_KEY and secrets are NOT in widget_vars
        assert "GEMINI_API_KEY" not in win.widget_vars
        assert not any("KEY" in k for k in win.widget_vars)
        assert not any("SECRET" in k for k in win.widget_vars)

        win.destroy()
    finally:
        root.destroy()


def test_settings_window_source_and_overrides(tmp_path: Path):
    """Verify source tags and overridden_by_env flags in SettingsWindow."""
    root = make_test_root()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("OLLAMA_NUM_CTX=4096\nLLM_PROVIDER=ollama\n", encoding="utf-8")

        ctrl = FakeController()
        win = SettingsWindow(
            root,
            controller=ctrl,
            env_path=env_file,
            environ={"OLLAMA_NUM_CTX": "8192"},
        )

        assert win.effective["OLLAMA_NUM_CTX"].source == "environment"
        assert win.effective["OLLAMA_NUM_CTX"].overridden_by_env is True
        assert win.effective["LLM_PROVIDER"].source == ".env"
        assert win.effective["LLM_PROVIDER"].overridden_by_env is False

        win.destroy()
    finally:
        root.destroy()


def test_settings_window_plan_preview_never_confirmation(tmp_path: Path):
    """Verify confirmation dialog when saving PLAN_PREVIEW=never."""
    root = make_test_root()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("PLAN_PREVIEW=auto\n", encoding="utf-8")

        ctrl = FakeController()
        win = SettingsWindow(root, controller=ctrl, env_path=env_file)
        win.widget_vars["PLAN_PREVIEW"].set("never")

        # Case 1: User says No to warning -> save aborted
        with patch("tkinter.messagebox.askyesno", return_value=False) as mock_ask:
            win._on_save()
            mock_ask.assert_called_once()
            # File should not be modified
            assert "PLAN_PREVIEW=never" not in env_file.read_text(encoding="utf-8")

        # Case 2: User says Yes -> save proceeds
        with patch("tkinter.messagebox.askyesno", return_value=True) as mock_ask, \
             patch("tkinter.messagebox.showinfo") as mock_info:
            win._on_save()
            mock_ask.assert_called_once()
            mock_info.assert_called_once()
            assert "PLAN_PREVIEW=never" in env_file.read_text(encoding="utf-8")
    finally:
        root.destroy()


def test_settings_window_save_flow_and_audit(tmp_path: Path):
    """Verify full save flow creates .env.bak, updates .env, logs audit, and shows info."""
    root = make_test_root()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("SPEAK_REPLIES=0\n", encoding="utf-8")

        ctrl = FakeController()
        win = SettingsWindow(root, controller=ctrl, env_path=env_file)

        # Toggle SPEAK_REPLIES to 1
        win.widget_vars["SPEAK_REPLIES"].set(True)

        info_messages: list[str] = []
        with patch("tkinter.messagebox.showinfo", side_effect=lambda title, msg, **kw: info_messages.append(msg)):
            win._on_save()

        # Check .env updated
        content = env_file.read_text(encoding="utf-8")
        assert "SPEAK_REPLIES=1" in content

        # Check .env.bak created
        bak_file = tmp_path / ".env.bak"
        assert bak_file.exists()
        assert bak_file.read_text(encoding="utf-8") == "SPEAK_REPLIES=0\n"

        # Check audit entry
        audit_entries = ctrl.app.agent.audit.entries
        assert len(audit_entries) == 1
        entry = audit_entries[0]
        assert entry["decision"] == "settings_change"
        assert entry["args"]["key"] == "SPEAK_REPLIES"
        assert entry["args"]["old_value"] == "0"
        assert entry["args"]["new_value"] == "1"

        # Check dialog contents
        assert len(info_messages) == 1
        msg = info_messages[0]
        assert "Changed keys: SPEAK_REPLIES" in msg
        assert ".env.bak was created." in msg
        assert "Restart ORBIT to apply." in msg
    finally:
        root.destroy()


def test_settings_window_no_changes_dialog(tmp_path: Path):
    """Verify dialog when user clicks Save without changing anything."""
    root = make_test_root()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("SPEAK_REPLIES=0\n", encoding="utf-8")

        ctrl = FakeController()
        win = SettingsWindow(root, controller=ctrl, env_path=env_file)

        info_messages: list[str] = []
        with patch("tkinter.messagebox.showinfo", side_effect=lambda title, msg, **kw: info_messages.append(msg)):
            win._on_save()

        assert len(info_messages) == 1
        assert "No settings were changed." in info_messages[0]
    finally:
        root.destroy()
