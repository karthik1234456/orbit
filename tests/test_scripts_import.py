"""Tests verifying all scripts can be imported from different working directories without PYTHONPATH."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS = sorted([
    p.name for p in SCRIPTS_DIR.glob("*.py")
    if p.name != "__init__.py" and not p.name.startswith(".")
])


def test_scripts_list_not_empty():
    """Verify scripts directory is discovered properly."""
    assert len(SCRIPTS) >= 5
    assert "browser_check.py" in SCRIPTS
    assert "browser_selftest.py" in SCRIPTS
    assert "intent_probe.py" in SCRIPTS
    assert "local_check.py" in SCRIPTS
    assert "voice_check.py" in SCRIPTS


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_import_script_from_different_directory_without_pythonpath(tmp_path: Path, script_name: str):
    """Verify each script can be imported from a separate working directory with PYTHONPATH removed."""
    script_stem = Path(script_name).stem
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    # 1. Test importing module with only scripts/ on sys.path (mimicking running from another directory)
    # The script's sys.path bootstrap must add the repo root so internal project imports succeed.
    code_as_script = f"""
import sys
import os

assert 'PYTHONPATH' not in os.environ
sys.path.insert(0, r'{SCRIPTS_DIR}')
import {script_stem}
print('IMPORT_OK')
"""
    res1 = subprocess.run(
        [sys.executable, "-c", code_as_script],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res1.returncode == 0, (
        f"Failed to import {script_name} via scripts dir from {tmp_path}:\n"
        f"STDOUT: {res1.stdout}\nSTDERR: {res1.stderr}"
    )
    assert "IMPORT_OK" in res1.stdout

    # 2. Test importing as scripts.<script_stem> with repo root on sys.path
    code_as_pkg = f"""
import sys
import os

assert 'PYTHONPATH' not in os.environ
sys.path.insert(0, r'{REPO_ROOT}')
import scripts.{script_stem}
print('PKG_IMPORT_OK')
"""
    res2 = subprocess.run(
        [sys.executable, "-c", code_as_pkg],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res2.returncode == 0, (
        f"Failed to import scripts.{script_stem} from {tmp_path}:\n"
        f"STDOUT: {res2.stdout}\nSTDERR: {res2.stderr}"
    )
    assert "PKG_IMPORT_OK" in res2.stdout
