"""CLI tests for /settings and /set commands in main.py.

Covers:
- /settings table printing (headers: Key, Effective Value, Source, Restart Needed).
- /settings hides secrets and flags overridden environment variables.
- /set KEY VALUE validation and confirmation prompt: "Change KEY from A to B? (y/n)".
- /set rejection of unknown keys and secret-like keys ("That setting can't be changed here.").
- /set invalid value validation error message.
- /set confirmation accepts 'y' -> persists to .env, creates .env.bak, logs audit event.
- /set rejection on 'n' -> prints "Setting unchanged.", leaves file intact.
- Planted fake canary secret never leaked in any output or audit entries.
"""

from pathlib import Path
from typing import Any

from config.settings_store import SPECS
from main import handle_set_command, print_settings_table


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


def test_print_settings_table(tmp_path: Path):
    """Verify /settings outputs tabular format with all specs, sources, and restart flags."""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=ollama\nOLLAMA_NUM_CTX=4096\n", encoding="utf-8")

    environ = {
        "OLLAMA_NUM_CTX": "8192",  # overridden
    }

    output_lines: list[str] = []
    print_settings_table(
        env_path=env_file,
        environ=environ,
        print_func=output_lines.append,
    )

    combined = "\n".join(output_lines)
    assert "Key" in combined
    assert "Effective Value" in combined
    assert "Source" in combined
    assert "Restart Needed" in combined

    # Check that all specs are present
    for key in SPECS:
        assert key in combined, f"Spec key {key} missing from /settings output"

    # Check override flag
    assert "(overridden)" in combined

    # Check that secret-like names are absent
    assert "GEMINI_API_KEY" not in combined
    assert not any("SECRET" in line for line in output_lines)


def test_set_command_usage():
    """Verify /set with missing arguments prints usage."""
    output_lines: list[str] = []
    handle_set_command("/set", print_func=output_lines.append)
    assert any("Usage: /set KEY VALUE" in line for line in output_lines)

    output_lines.clear()
    handle_set_command("/set LLM_PROVIDER", print_func=output_lines.append)
    assert any("Usage: /set KEY VALUE" in line for line in output_lines)


def test_set_command_unknown_and_secret_keys():
    """Verify /set refuses unknown and secret keys with friendly message."""
    output_lines: list[str] = []
    handle_set_command("/set UNKNOWN_KEY 123", print_func=output_lines.append)
    assert any("That setting can't be changed here." in line for line in output_lines)

    output_lines.clear()
    handle_set_command("/set GEMINI_API_KEY secret_abc", print_func=output_lines.append)
    assert any("That setting can't be changed here." in line for line in output_lines)

    output_lines.clear()
    handle_set_command("/set SOME_PASSWORD_KEY pass", print_func=output_lines.append)
    assert any("That setting can't be changed here." in line for line in output_lines)


def test_set_command_invalid_value(tmp_path: Path):
    """Verify /set reports validation errors when input is invalid."""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=auto\n", encoding="utf-8")

    output_lines: list[str] = []
    handle_set_command(
        "/set LLM_PROVIDER invalid_provider",
        env_path=env_file,
        print_func=output_lines.append,
    )
    assert any("Validation error:" in line for line in output_lines)
    assert any("Allowed options: auto, ollama, gemini, mock" in line for line in output_lines)


def test_set_command_confirm_no(tmp_path: Path):
    """Verify answering 'n' leaves .env untouched and prints 'Setting unchanged.'."""
    env_file = tmp_path / ".env"
    env_file.write_text("SPEAK_REPLIES=0\n", encoding="utf-8")

    agent = FakeAgent()
    output_lines: list[str] = []
    prompt_asked: list[str] = []

    def fake_input(prompt: str) -> str:
        prompt_asked.append(prompt)
        return "n"

    handle_set_command(
        "/set SPEAK_REPLIES 1",
        agent=agent,
        env_path=env_file,
        input_func=fake_input,
        print_func=output_lines.append,
    )

    assert len(prompt_asked) == 1
    assert "Change SPEAK_REPLIES from 0 to 1? (y/n)" in prompt_asked[0]
    assert any("Setting unchanged." in line for line in output_lines)
    assert env_file.read_text(encoding="utf-8") == "SPEAK_REPLIES=0\n"
    assert not (tmp_path / ".env.bak").exists()
    assert len(agent.audit.entries) == 0


def test_set_command_confirm_yes(tmp_path: Path):
    """Verify answering 'y' persists setting, creates .env.bak, and logs audit."""
    env_file = tmp_path / ".env"
    env_file.write_text("SPEAK_REPLIES=0\n", encoding="utf-8")

    agent = FakeAgent()
    output_lines: list[str] = []
    prompt_asked: list[str] = []

    def fake_input(prompt: str) -> str:
        prompt_asked.append(prompt)
        return "y"

    handle_set_command(
        "/set SPEAK_REPLIES 1",
        agent=agent,
        env_path=env_file,
        input_func=fake_input,
        print_func=output_lines.append,
    )

    assert len(prompt_asked) == 1
    assert "Change SPEAK_REPLIES from 0 to 1? (y/n)" in prompt_asked[0]

    # Verify .env updated
    assert "SPEAK_REPLIES=1" in env_file.read_text(encoding="utf-8")

    # Verify .env.bak created
    bak_file = tmp_path / ".env.bak"
    assert bak_file.exists()
    assert bak_file.read_text(encoding="utf-8") == "SPEAK_REPLIES=0\n"

    # Verify output notices
    assert any("Setting updated: SPEAK_REPLIES=1" in line for line in output_lines)
    assert any(".env.bak was created." in line for line in output_lines)
    assert any("Restart ORBIT to apply." in line for line in output_lines)

    # Verify audit logging
    assert len(agent.audit.entries) == 1
    entry = agent.audit.entries[0]
    assert entry["decision"] == "settings_change"
    assert entry["args"]["key"] == "SPEAK_REPLIES"
    assert entry["args"]["old_value"] == "0"
    assert entry["args"]["new_value"] == "1"


def test_no_secret_leak_in_cli(tmp_path: Path):
    """Ensure canary secrets never appear in /settings or /set outputs."""
    secret_canary = "super_canary_secret_value_xyz999"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"GEMINI_API_KEY={secret_canary}\n"
        "LLM_PROVIDER=auto\n",
        encoding="utf-8",
    )

    environ = {
        "GEMINI_API_KEY": secret_canary,
        "SECRET_TOKEN": secret_canary,
    }

    # 1. /settings output
    output_lines: list[str] = []
    print_settings_table(
        env_path=env_file,
        environ=environ,
        print_func=output_lines.append,
    )
    combined = "\n".join(output_lines)
    assert secret_canary not in combined

    # 2. /set output
    output_lines.clear()
    handle_set_command(
        f"/set GEMINI_API_KEY {secret_canary}",
        env_path=env_file,
        environ=environ,
        print_func=output_lines.append,
    )
    combined_set = "\n".join(output_lines)
    assert secret_canary not in combined_set
