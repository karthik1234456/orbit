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
    """Verify that .env and .env.bak are never tracked in git."""
    tracked = _get_tracked_files()
    assert ".env" not in tracked, ".env file must never be tracked in git"
    assert ".env.bak" not in tracked, ".env.bak file must never be tracked in git"
    assert not any(
        f == ".env" or f.endswith("/.env") for f in tracked
    ), "A .env file is tracked in git"
    assert not any(
        f == ".env.bak" or f.endswith("/.env.bak") for f in tracked
    ), "A .env.bak file is tracked in git"


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


def _is_binary_file(path: Path) -> bool:
    """Return True if path is a binary file based on extension or null byte detection."""
    binary_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf",
        ".pyc", ".pyd", ".exe", ".dll", ".so", ".dylib",
        ".bin", ".zip", ".tar", ".gz", ".db", ".sqlite", ".woff", ".woff2",
    }
    if path.suffix.lower() in binary_exts:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
            if b"\x00" in chunk:
                return True
            chunk.decode("utf-8")
            return False
    except (UnicodeDecodeError, Exception):
        return True


def test_no_secret_tokens_in_tracked_files():
    """Verify no tracked text file contains token-shaped credentials or secret literals."""
    tracked = _get_tracked_files()

    xox_prefix = "x" + "ox[abprse]-"
    aws_prefix = "AK" + "IA"
    gh_prefix = "gh" + "[pousr]_[A-Za-z0-9]{36,}"
    gh_pat_prefix = "github" + "_pat_[A-Za-z0-9_]{22,}"
    google_prefix = "AI" + "za"
    pem_prefix = "-" * 5 + "BEGIN"
    pem_suffix = "PRIVATE" + " KEY" + "-" * 5
    sk_prefix = "s" + "k-"
    ey_j = "ey" + "J"

    patterns = [
        ("Slack token", re.compile(rf"{xox_prefix}[A-Za-z0-9-]+")),
        ("AWS access key", re.compile(rf"\b{aws_prefix}[0-9A-Z]{{16}}\b")),
        ("GitHub token", re.compile(rf"\b({gh_prefix}|{gh_pat_prefix})\b")),
        ("Google API key", re.compile(rf"\b{google_prefix}[0-9A-Za-z_-]{{35}}\b")),
        ("PEM private key header", re.compile(rf"{pem_prefix}[A-Z ]*{pem_suffix}")),
        ("sk- style key", re.compile(rf"\b{sk_prefix}[A-Za-z0-9_-]{{20,}}\b")),
        ("JWT token", re.compile(rf"\b{ey_j}[A-Za-z0-9_-]+\.{ey_j}[A-Za-z0-9_-]+")),
    ]

    aws_documented_example = "AK" + "IAIOSFODNN7EXAMPLE"
    violations: list[str] = []

    for file_path_str in tracked:
        if file_path_str.startswith("evals/results/"):
            continue

        file_path = Path(file_path_str)
        if not file_path.is_file() or _is_binary_file(file_path):
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for line_no, line in enumerate(content.splitlines(), start=1):
            for name, pattern in patterns:
                match = pattern.search(line)
                if match:
                    if name == "AWS access key" and match.group(0) == aws_documented_example:
                        continue
                    # Report file and line number only; never print the matched secret value
                    violations.append(f"{file_path_str}:{line_no} ({name})")

    assert not violations, (
        "Credential pattern(s) found in tracked file(s):\n"
        + "\n".join(violations)
    )


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

    required = [".env", ".env.bak", "logs/", "data/", "workspace/", "evals/results/", "demo_home/", "data/voice_eval/"]
    for req in required:
        req_clean = req.rstrip("/")
        match = any(
            e == req or e.rstrip("/") == req_clean for e in entries
        )
        assert match, f".gitignore is missing required entry: '{req}'"


def test_routines_db_not_tracked():
    """Verify that data/routines.db is never tracked in git."""
    tracked = _get_tracked_files()
    assert "data/routines.db" not in tracked, "data/routines.db must never be tracked in git"
    assert not any(
        f.endswith("routines.db") for f in tracked
    ), "A routines.db database file is tracked in git"


def test_no_sqlite_db_files_tracked():
    """Verify that no *.db database file is ever tracked in git."""
    tracked = _get_tracked_files()
    db_files = [f for f in tracked if f.endswith(".db")]
    assert not db_files, f"Database files (*.db) are tracked in git: {db_files}"


def test_voice_eval_not_tracked():
    """Verify that nothing under data/voice_eval/ is tracked in git."""
    tracked = _get_tracked_files()
    assert not any(f.startswith("data/voice_eval/") for f in tracked), (
        "Files under data/voice_eval/ must never be tracked in git"
    )
