"""Repository hygiene, secret leak prevention, and git tracking checks."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


def _is_git_repository() -> bool:
    """Return True if git CLI is available and the current directory is inside a git work tree."""
    if not shutil.which("git"):
        return False
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


# Skip the entire module if git is not available or directory is not a git repository
if not _is_git_repository():
    pytest.skip("Git is unavailable or directory is not a git repository.", allow_module_level=True)


def _get_tracked_files() -> list[str]:
    """Retrieve list of files tracked by git."""
    proc = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]


def test_env_file_not_tracked():
    """Verify that .env is never tracked in git."""
    tracked = _get_tracked_files()
    assert ".env" not in tracked, ".env file must never be tracked in git"
    assert not any(
        f == ".env" or f.endswith("/.env") for f in tracked
    ), "A .env file is tracked in git"


def test_no_tracked_files_in_ignored_directories():
    """Verify no tracked files live in logs/, data/, or workspace/, except .gitkeep."""
    tracked = _get_tracked_files()
    forbidden_prefixes = ("logs/", "data/", "workspace/")

    for file_path in tracked:
        for prefix in forbidden_prefixes:
            if file_path.startswith(prefix):
                assert (
                    Path(file_path).name == ".gitkeep"
                ), f"Tracked file '{file_path}' lives in forbidden folder '{prefix}' and is not .gitkeep"


def test_no_real_looking_google_keys_in_tracked_files():
    """Verify no tracked file contains a real Google API key without the word FAKE."""
    tracked = _get_tracked_files()
    key_pattern = re.compile(r"AIza[0-9A-Za-z_-]{35}")

    for file_path_str in tracked:
        file_path = Path(file_path_str)
        if not file_path.is_file():
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for line_no, line in enumerate(content.splitlines(), start=1):
            if key_pattern.search(line):
                assert (
                    "FAKE" in line
                ), f"Potential real Google key found in {file_path_str}:{line_no} without 'FAKE': {line.strip()}"


def test_env_example_contains_only_placeholder_values():
    """Verify .env.example contains only placeholder values (your_key_here)."""
    env_example = Path(".env.example")
    assert env_example.exists(), ".env.example file must exist"

    content = env_example.read_text(encoding="utf-8")
    assert "your_key_here" in content, ".env.example must use 'your_key_here' placeholders"

    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) > 0, ".env.example must contain configuration keys"

    for line in lines:
        assert "=" in line, f"Malformed line in .env.example: {line}"
        key, val = line.split("=", 1)
        val = val.strip()
        assert (
            val == "your_key_here"
        ), f"Key '{key}' in .env.example does not have placeholder value 'your_key_here': '{val}'"


def test_gitignore_lists_required_entries():
    """Verify .gitignore contains .env, logs/, data/, workspace/, and evals/results/."""
    gitignore = Path(".gitignore")
    assert gitignore.exists(), ".gitignore file must exist"

    content = gitignore.read_text(encoding="utf-8")
    entries = {
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.startswith("#")
    }

    required = [".env", "logs/", "data/", "workspace/", "evals/results/"]
    for req in required:
        req_clean = req.rstrip("/")
        match = any(
            e == req or e.rstrip("/") == req_clean for e in entries
        )
        assert match, f".gitignore is missing required entry: '{req}'"
