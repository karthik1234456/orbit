"""Unit tests for config.settings_store.

Covers:
- Spec whitelist guard: no security keys in SPECS, no tool imports settings_store or opens .env.
- Validation tables for every spec in SPECS.
- Control character and newline rejection (including 'x\\nGEMINI_API_KEY=abc').
- Quote and leading symbol rejection.
- Line ending (CRLF vs LF) and UTF-8 BOM preservation.
- Duplicate key and merged line refusal with line numbers and key name only.
- Automated .env.bak creation byte-identical to original.
- Atomic write with failure rollback and temp file cleanup.
- Source detection (default, .env, environment, overridden).
- ORBIT_MOCK checked writes line, unchecked removes line with warning.
- Secret leak prevention across outputs and diagnostics.
"""

from pathlib import Path
import re
from typing import Any
import pytest

from config.settings_store import (
    SPECS,
    SettingsError,
    is_secret_like,
    read_effective,
    save,
    validate,
)
from core.diagnostics import build_diagnostics_text


def test_specs_whitelist_guard():
    """Verify that no security or governance boundaries are exposed in SPECS."""
    forbidden = ("PERMISSION", "CONFIRM", "SANDBOX", "AUDIT", "GUARD")
    for key in SPECS:
        key_upper = key.upper()
        for term in forbidden:
            assert term not in key_upper, f"Forbidden term '{term}' found in editable spec key '{key}'"


def test_tools_never_import_settings_store_or_touch_env():
    """Verify that no registered tools in tools/ import settings_store or open .env."""
    repo_root = Path(__file__).resolve().parent.parent
    tools_dir = repo_root / "tools"
    assert tools_dir.is_dir(), "tools/ directory not found"

    violations: list[str] = []
    for py_file in tools_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        if "settings_store" in content:
            violations.append(f"{py_file.name}: imports or references settings_store")
        # Check for open(".env") or Path(".env")
        if re.search(r'open\s*\(\s*[\'"][^\'"]*\.env[\'"]', content) or re.search(
            r'Path\s*\(\s*[\'"][^\'"]*\.env[\'"]', content
        ):
            violations.append(f"{py_file.name}: directly accesses .env file")

    assert not violations, "Tool security isolation violated:\n" + "\n".join(violations)


def test_validation_tables_all_specs():
    """Test valid and invalid inputs across all specs in SPECS."""
    valid_test_cases: dict[str, list[tuple[Any, str]]] = {
        "LLM_PROVIDER": [("auto", "auto"), ("OLLAMA", "ollama"), ("Gemini", "gemini"), ("mock", "mock")],
        "OLLAMA_MODEL": [("qwen3:8b", "qwen3:8b"), ("llama3.2:1b-instruct", "llama3.2:1b-instruct"), ("my_model.v1", "my_model.v1")],
        "OLLAMA_NUM_CTX": [(2048, "2048"), ("8192", "8192"), (32768, "32768")],
        "BROWSER_ENABLED": [("auto", "auto"), ("0", "0"), ("1", "1"), ("AUTO", "auto")],
        "SPEAK_REPLIES": [("1", "1"), ("true", "1"), ("yes", "1"), ("on", "1"), ("0", "0"), ("false", "0"), ("no", "0"), ("off", "0")],
        "VOICE_IN_UI": [("true", "1"), ("false", "0"), ("1", "1"), ("0", "0")],
        "COMMAND_SHORTCUTS": [("true", "1"), ("false", "0")],
        "CHAINS_ENABLED": [("1", "1"), ("0", "0")],
        "PLAN_PREVIEW": [("auto", "auto"), ("ALWAYS", "always"), ("never", "never")],
        "TOOL_ROUTING": [("1", "1"), ("0", "0")],
        "REMINDERS_ENABLED": [("true", "1"), ("false", "0")],
        "SESSION_CONTEXT": [("1", "1"), ("0", "0")],
        "RAG_AUTO": [("1", "1"), ("0", "0")],
        "DOCTOR_ON_START": [("1", "1"), ("0", "0")],
        "STT_MODEL": [("tiny.en", "tiny.en"), ("base.en", "base.en"), ("small.en", "small.en"), ("tiny", "tiny"), ("base", "base"), ("small", "small")],
        "STT_LANGUAGE": [("en", "en"), ("hi", "hi"), ("empty", ""), ("", "")],
        "ORBIT_MOCK": [("1", "1"), ("0", "0"), ("true", "1"), ("false", "0")],
    }

    invalid_test_cases: dict[str, list[Any]] = {
        "LLM_PROVIDER": ["openai", "claude", "invalid"],
        "OLLAMA_MODEL": ["", "model with spaces", "model$bad", "a" * 65],
        "OLLAMA_NUM_CTX": [1024, 65536, "not_int", -1],
        "BROWSER_ENABLED": ["2", "enabled", "chrome"],
        "SPEAK_REPLIES": ["maybe", "2", "none"],
        "VOICE_IN_UI": ["invalid", "2"],
        "COMMAND_SHORTCUTS": ["invalid"],
        "CHAINS_ENABLED": ["invalid"],
        "PLAN_PREVIEW": ["sometimes", "invalid"],
        "TOOL_ROUTING": ["invalid"],
        "REMINDERS_ENABLED": ["invalid"],
        "SESSION_CONTEXT": ["invalid"],
        "RAG_AUTO": ["invalid"],
        "DOCTOR_ON_START": ["invalid"],
        "STT_MODEL": ["large", "medium", "whisper-1"],
        "STT_LANGUAGE": ["fr", "de", "es"],
        "ORBIT_MOCK": ["invalid", "2"],
    }

    for key, pairs in valid_test_cases.items():
        assert key in SPECS, f"Key '{key}' missing from SPECS"
        for raw_in, expected_out in pairs:
            assert validate(key, raw_in) == expected_out, f"Validation failed for {key}={raw_in}"

    for key, bad_inputs in invalid_test_cases.items():
        for bad in bad_inputs:
            with pytest.raises(SettingsError):
                validate(key, bad)


def test_validation_rejects_control_characters():
    """Verify rejection of newlines, carriage returns, NUL, and control chars."""
    bad_values = [
        "val\nnewline",
        "val\rreturn",
        "val\0nul",
        "val\x1fcontrol",
        "x\nGEMINI_API_KEY=abc",
        "x\r\nGEMINI_API_KEY=abc",
    ]
    for bad in bad_values:
        with pytest.raises(SettingsError) as exc_info:
            validate("LLM_PROVIDER", bad)
        assert "control characters" in str(exc_info.value).lower() or "newline" in str(exc_info.value).lower()


def test_validation_rejects_quotes_and_leading_symbols():
    """Verify rejection of surrounding quotes, leading '=' or '#', and overlong strings."""
    with pytest.raises(SettingsError):
        validate("LLM_PROVIDER", '"auto"')
    with pytest.raises(SettingsError):
        validate("LLM_PROVIDER", "'auto'")
    with pytest.raises(SettingsError):
        validate("LLM_PROVIDER", "=auto")
    with pytest.raises(SettingsError):
        validate("LLM_PROVIDER", "#auto")
    with pytest.raises(SettingsError):
        validate("OLLAMA_MODEL", "a" * 105)


def test_validation_rejects_secrets_and_unknown_keys():
    """Verify that secret-like keys and unknown keys cannot be validated or edited."""
    secret_keys = ["GEMINI_API_KEY", "API_KEY", "SECRET_TOKEN", "PASSWORD", "MY_AUTH_KEY"]
    for k in secret_keys:
        assert is_secret_like(k)
        with pytest.raises(SettingsError) as exc_info:
            validate(k, "val")
        assert "That setting can't be changed here." in str(exc_info.value)

    with pytest.raises(SettingsError):
        validate("UNKNOWN_KEY", "val")


def test_save_preserves_bom_and_crlf(tmp_path: Path):
    """Verify that save preserves UTF-8 BOM, CRLF, comments, unknown keys, and secrets."""
    env_file = tmp_path / ".env"
    initial_content = (
        "\ufeff# Project Environment File\r\n"
        "\r\n"
        "GEMINI_API_KEY=my_super_secret_plant_12345\r\n"
        "CUSTOM_APP_PORT=8080\r\n"
        "LLM_PROVIDER=auto\r\n"
        "SPEAK_REPLIES=0\r\n"
        "# End of file\r\n"
    )
    env_file.write_bytes(initial_content.encode("utf-8"))

    res = save(env_file, {"SPEAK_REPLIES": "1", "LLM_PROVIDER": "ollama"})

    assert "SPEAK_REPLIES" in res.changed
    assert res.changed["SPEAK_REPLIES"] == ("0", "1")
    assert res.changed["LLM_PROVIDER"] == ("auto", "ollama")

    saved_bytes = env_file.read_bytes()
    assert saved_bytes.startswith(b"\xef\xbb\xbf"), "BOM was lost"
    assert b"\r\n" in saved_bytes, "CRLF was lost"
    assert b"\r\n\n" not in saved_bytes

    text = saved_bytes.decode("utf-8")
    assert "# Project Environment File" in text
    assert "GEMINI_API_KEY=my_super_secret_plant_12345" in text, "Secret line was corrupted"
    assert "CUSTOM_APP_PORT=8080" in text, "Unknown key was corrupted"
    assert "LLM_PROVIDER=ollama" in text
    assert "SPEAK_REPLIES=1" in text
    assert "# End of file" in text

    # Verify .env.bak was created and is byte-identical to original
    bak_file = tmp_path / ".env.bak"
    assert bak_file.is_file(), ".env.bak was not created"
    assert bak_file.read_bytes() == initial_content.encode("utf-8")


def test_save_preserves_lf_line_endings(tmp_path: Path):
    """Verify that save preserves Unix LF line endings when present."""
    env_file = tmp_path / ".env"
    initial_content = (
        "# Unix Style LF\n"
        "LLM_PROVIDER=auto\n"
        "RAG_AUTO=1\n"
    )
    env_file.write_bytes(initial_content.encode("utf-8"))

    save(env_file, {"RAG_AUTO": "0"})

    saved_bytes = env_file.read_bytes()
    assert b"\r\n" not in saved_bytes, "LF was converted to CRLF"
    assert b"\n" in saved_bytes
    text = saved_bytes.decode("utf-8")
    assert "RAG_AUTO=0" in text
    assert "# Unix Style LF" in text


def test_save_refuses_duplicate_keys_with_line_numbers_and_key_name_only(tmp_path: Path):
    """Verify refusal to write when key to change appears multiple times, reporting line numbers only."""
    env_file = tmp_path / ".env"
    content = (
        "# Header\n"
        "LLM_PROVIDER=auto\n"
        "CUSTOM_VAR=123\n"
        "GEMINI_API_KEY=secret_key_plant_abc\n"
        "LLM_PROVIDER=mock\n"
    )
    env_file.write_bytes(content.encode("utf-8"))

    with pytest.raises(SettingsError) as exc_info:
        save(env_file, {"LLM_PROVIDER": "ollama"})

    msg = str(exc_info.value)
    assert "LLM_PROVIDER" in msg
    assert "2" in msg and "5" in msg
    assert "secret_key_plant_abc" not in msg, "Secret leaked in error message"
    # Ensure original was untouched
    assert env_file.read_bytes() == content.encode("utf-8")


def test_save_refuses_merged_lines_with_line_numbers_and_key_name_only(tmp_path: Path):
    """Verify refusal to write when key to change has a merged line value, reporting line number only."""
    env_file = tmp_path / ".env"
    content = (
        "# Header\n"
        "CUSTOM_VAR=123\n"
        "LLM_PROVIDER=OLLAMA_MODEL=qwen3:8b\n"
        "GEMINI_API_KEY=secret_key_plant_xyz\n"
    )
    env_file.write_bytes(content.encode("utf-8"))

    with pytest.raises(SettingsError) as exc_info:
        save(env_file, {"LLM_PROVIDER": "ollama"})

    msg = str(exc_info.value)
    assert "LLM_PROVIDER" in msg
    assert "3" in msg
    assert "secret_key_plant_xyz" not in msg, "Secret leaked in error message"
    assert env_file.read_bytes() == content.encode("utf-8")


def test_save_failure_leaves_original_untouched_and_cleans_temp_file(tmp_path: Path, monkeypatch):
    """Simulate os.replace failure and verify original is untouched and temp files cleaned."""
    env_file = tmp_path / ".env"
    initial_bytes = b"LLM_PROVIDER=auto\nSPEAK_REPLIES=0\n"
    env_file.write_bytes(initial_bytes)

    def _failing_replace(src, dst):
        raise OSError("Simulated filesystem error during atomic replace")

    monkeypatch.setattr("os.replace", _failing_replace)

    with pytest.raises(OSError):
        save(env_file, {"SPEAK_REPLIES": "1"})

    assert env_file.read_bytes() == initial_bytes, "Original file was modified on failure"
    # Check that no temporary files remain in tmp_path
    remaining_tmps = list(tmp_path.glob(".*.tmp_*")) + list(tmp_path.glob("*.tmp_*"))
    assert not remaining_tmps, f"Dangling temp file(s) left behind: {remaining_tmps}"


def test_save_creates_missing_env(tmp_path: Path):
    """Saving when .env does not exist creates a clean file with changed keys only."""
    env_file = tmp_path / ".env"
    assert not env_file.exists()

    res = save(env_file, {"LLM_PROVIDER": "ollama", "TOOL_ROUTING": "1"})
    assert res.changed["LLM_PROVIDER"] == (SPECS["LLM_PROVIDER"].default, "ollama")

    assert env_file.is_file()
    text = env_file.read_text(encoding="utf-8")
    assert "# set by ORBIT settings" in text
    assert "LLM_PROVIDER=ollama" in text
    assert "TOOL_ROUTING=1" in text
    assert "GEMINI_API_KEY" not in text, ".env.example keys must not be populated"


def test_read_effective_source_detection(tmp_path: Path):
    """Verify resolution hierarchy: environment > .env > default, and override detection."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=gemini\n"
        "OLLAMA_NUM_CTX=4096\n"
        "SPEAK_REPLIES=1\n",
        encoding="utf-8",
    )

    environ = {
        "OLLAMA_NUM_CTX": "16384",  # Overrides .env (differs)
        "SPEAK_REPLIES": "1",       # Same as .env
        "TOOL_ROUTING": "1",        # Not in .env
    }

    effective = read_effective(env_path=env_file, environ=environ)

    # 1. Default: CHAINS_ENABLED (neither in .env nor in environ)
    assert effective["CHAINS_ENABLED"].source == "default"
    assert effective["CHAINS_ENABLED"].overridden_by_env is False
    assert effective["CHAINS_ENABLED"].value == SPECS["CHAINS_ENABLED"].default

    # 2. .env: LLM_PROVIDER (in .env, not in environ)
    assert effective["LLM_PROVIDER"].source == ".env"
    assert effective["LLM_PROVIDER"].overridden_by_env is False
    assert effective["LLM_PROVIDER"].value == "gemini"

    # 3. Environment: TOOL_ROUTING (in environ, not in .env)
    assert effective["TOOL_ROUTING"].source == "environment"
    assert effective["TOOL_ROUTING"].overridden_by_env is False
    assert effective["TOOL_ROUTING"].value == "1"

    # 4. Same value in both: SPEAK_REPLIES
    assert effective["SPEAK_REPLIES"].source == ".env"
    assert effective["SPEAK_REPLIES"].overridden_by_env is False
    assert effective["SPEAK_REPLIES"].value == "1"

    # 5. Overridden by environment: OLLAMA_NUM_CTX
    assert effective["OLLAMA_NUM_CTX"].source == "environment"
    assert effective["OLLAMA_NUM_CTX"].overridden_by_env is True
    assert effective["OLLAMA_NUM_CTX"].value == "16384"


def test_orbit_mock_toggle(tmp_path: Path):
    """Verify ORBIT_MOCK checked writes line, unchecked removes line with warning."""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=auto\n", encoding="utf-8")

    # Checked: writes ORBIT_MOCK=1
    res1 = save(env_file, {"ORBIT_MOCK": "1"})
    assert res1.changed["ORBIT_MOCK"] == ("0", "1")
    text1 = env_file.read_text(encoding="utf-8")
    assert "ORBIT_MOCK=1" in text1

    # Unchecked: removes ORBIT_MOCK line and reports warning
    res2 = save(env_file, {"ORBIT_MOCK": "0"})
    assert res2.changed["ORBIT_MOCK"] == ("1", "0")
    text2 = env_file.read_text(encoding="utf-8")
    assert "ORBIT_MOCK" not in text2
    assert any("ORBIT_MOCK was removed from .env" in w for w in res2.warnings)


def test_no_secret_leak_anywhere(tmp_path: Path, monkeypatch):
    """Ensure planted fake secrets never appear in settings effective values, diag text, or save results."""
    secret_plant = "planted_super_secret_canary_value_98765"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"GEMINI_API_KEY={secret_plant}\n"
        f"CUSTOM_SECRET_PASSWORD={secret_plant}\n"
        "LLM_PROVIDER=auto\n",
        encoding="utf-8",
    )

    environ = {
        "GEMINI_API_KEY": secret_plant,
        "SECRET_TOKEN": secret_plant,
    }

    # 1. read_effective
    effective = read_effective(env_path=env_file, environ=environ)
    for k, eff in effective.items():
        assert secret_plant not in k
        assert secret_plant not in eff.value

    # 2. save
    res = save(env_file, {"SPEAK_REPLIES": "1"})
    res_str = str(res)
    assert secret_plant not in res_str

    # 3. diagnostics text
    report = build_diagnostics_text()
    assert secret_plant not in report
