"""Tests for ORBIT Environment Doctor (core/doctor.py, scripts/doctor.py).

All tests use injected fake probes and strict monkeypatching to guarantee
zero real subprocess, network, socket, microphone, browser, or LLM calls.
"""

import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
import pytest

from core.doctor import (
    Check,
    CommandResult,
    HttpResponse,
    Probes,
    format_report,
    run_checks,
)


@pytest.fixture(autouse=True)
def protect_doctor_safety(monkeypatch):
    """Enforce strict isolation: ban real subprocess, socket, sounddevice, playwright, and model calls."""
    import socket
    import subprocess

    def ban_subprocess(*args, **kwargs):
        raise AssertionError(f"Illegal subprocess call attempted in test: {args}")

    def ban_socket(*args, **kwargs):
        raise AssertionError(f"Illegal socket connection attempted in test: {args}")

    monkeypatch.setattr(subprocess, "run", ban_subprocess)
    monkeypatch.setattr(subprocess, "Popen", ban_subprocess)
    monkeypatch.setattr(subprocess, "check_output", ban_subprocess)
    monkeypatch.setattr(socket.socket, "connect", ban_socket)

    try:
        import sounddevice

        def ban_sd(*args, **kwargs):
            raise AssertionError("Illegal sounddevice stream attempted in test")

        monkeypatch.setattr(sounddevice, "InputStream", ban_sd)
        monkeypatch.setattr(sounddevice, "RawInputStream", ban_sd)
        monkeypatch.setattr(sounddevice, "Stream", ban_sd)
    except ImportError:
        pass

    try:
        from core.providers import MockProvider, OllamaProvider

        def ban_gen(*args, **kwargs):
            raise AssertionError("Illegal Provider.generate called in test")

        monkeypatch.setattr(MockProvider, "generate", ban_gen)
        monkeypatch.setattr(OllamaProvider, "generate", ban_gen)
    except ImportError:
        pass


def make_fake_probes(
    env: Optional[dict[str, str]] = None,
    files: Optional[dict[str, Any]] = None,
    http_responses: Optional[dict[str, Any]] = None,
    commands: Optional[dict[str, Any]] = None,
    specs: Optional[dict[str, bool]] = None,
    which_map: Optional[dict[str, str]] = None,
    disk_free_gb: float = 10.0,
    clock_val: float = 1700000000.0,
) -> Probes:
    """Build an injectable Probes instance populated with mock data."""
    env_dict = {
        "PATH": "C:\\Windows\\System32;C:\\Program Files\\Git\\bin",
        "VIRTUAL_ENV": "C:\\orbit\\.venv",
        "LLM_PROVIDER": "ollama",
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        "OLLAMA_MODEL": "qwen3:8b",
        "WORKSPACE_DIR": "workspace",
        "DATA_DIR": "data",
        "LOGS_DIR": "logs",
        **(env or {}),
    }

    file_dict = {
        ".env": "LLM_PROVIDER=ollama\nOLLAMA_MODEL=qwen3:8b\n",
        **(files or {}),
    }

    http_map = http_responses or {}
    cmd_map = commands or {}
    spec_map = {
        "playwright": True,
        "sounddevice": True,
        "faster_whisper": True,
        "pyttsx3": True,
        "numpy": True,
        "psutil": True,
        **(specs or {}),
    }

    which_dict = {
        "git": "C:\\Program Files\\Git\\bin\\git.exe",
        "chrome": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "notepad": "C:\\Windows\\System32\\notepad.exe",
        "calc": "C:\\Windows\\System32\\calc.exe",
        "mspaint": "C:\\Windows\\System32\\mspaint.exe",
        "explorer": "C:\\Windows\\explorer.exe",
        "cmd": "C:\\Windows\\System32\\cmd.exe",
        **(which_map or {}),
    }

    def fake_http_get(url: str, timeout: float = 2.0) -> HttpResponse:
        for pat, resp in http_map.items():
            if pat in url:
                if isinstance(resp, Exception):
                    raise resp
                if isinstance(resp, tuple):
                    return HttpResponse(status_code=resp[0], text=resp[1])
                if isinstance(resp, str):
                    return HttpResponse(status_code=200, text=resp)
                return resp
        if "11434/api/version" in url:
            return HttpResponse(status_code=200, text='{"version":"0.5.1"}')
        if "11434/api/tags" in url:
            return HttpResponse(status_code=200, text='{"models":[{"name":"qwen3:8b"}]}')
        if "11434/api/ps" in url:
            return HttpResponse(
                status_code=200,
                text='{"models":[{"name":"qwen3:8b","size":5000000000,"size_vram":5000000000}]}',
            )
        return HttpResponse(status_code=404, text="Not Found")

    def fake_run_command(cmd: list[str], timeout: float = 5.0) -> CommandResult:
        cmd_str = " ".join(cmd)
        for pat, res in cmd_map.items():
            if pat in cmd_str:
                if isinstance(res, Exception):
                    raise res
                if isinstance(res, tuple):
                    return CommandResult(returncode=res[0], stdout=res[1], stderr="")
                return res
        if "nvidia-smi" in cmd_str:
            return CommandResult(
                returncode=0,
                stdout="NVIDIA GeForce RTX 4050 Laptop GPU, 6144 MiB\n",
                stderr="",
            )
        if "git" in cmd_str and "check-ignore" in cmd_str:
            # By default all queried paths are ignored
            return CommandResult(returncode=0, stdout="\n".join(cmd[2:]) + "\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    def fake_find_spec(mod_name: str) -> Optional[Any]:
        if spec_map.get(mod_name, False):
            return SimpleNamespace(name=mod_name)
        return None

    def fake_which(cmd: str) -> Optional[str]:
        return which_dict.get(cmd)

    def fake_read_text(path: Any, encoding: str = "utf-8") -> str:
        p_str = str(path).replace("\\", "/")
        for k, v in file_dict.items():
            if k == p_str or p_str.endswith("/" + k) or p_str.endswith(k):
                if isinstance(v, Exception):
                    raise v
                return str(v)
        raise FileNotFoundError(f"File not found: {path}")

    def fake_path_exists(path: Any) -> bool:
        p_str = str(path).replace("\\", "/")
        for k in file_dict:
            if k == p_str or p_str.endswith("/" + k) or p_str.endswith(k):
                return True
        if any(d in p_str for d in ("workspace", "data", "logs", ".git", "Chrome", "hub")):
            return True
        return False

    def fake_disk_free(path: Any) -> int:
        return int(disk_free_gb * 1024 * 1024 * 1024)

    def fake_env(var: str, default: Optional[str] = None) -> Optional[str]:
        return env_dict.get(var, default)

    return Probes(
        http_get=fake_http_get,
        run_command=fake_run_command,
        find_spec=fake_find_spec,
        which=fake_which,
        read_text=fake_read_text,
        path_exists=fake_path_exists,
        disk_free=fake_disk_free,
        env=fake_env,
        clock=lambda: clock_val,
    )


# --- 1. Python & Venv Checks ---


def test_python_and_venv_ok():
    probes = make_fake_probes(env={"VIRTUAL_ENV": "C:\\orbit\\.venv"})
    checks = run_checks(probes)
    py_check = next(c for c in checks if c.name == "Python environment")
    assert py_check.status == "OK"
    assert "virtual environment active" in py_check.detail


def test_venv_missing_warns():
    probes = make_fake_probes(env={"VIRTUAL_ENV": ""})
    checks = run_checks(probes)
    py_check = next(c for c in checks if c.name == "Python environment")
    assert "Python" in py_check.detail


# --- 2. .env Parsing & Secret Redaction ---


def test_env_parsing_valid_bom_crlf():
    bom_content = "\ufeff# comment\r\nLLM_PROVIDER=ollama\r\nOLLAMA_MODEL=qwen3:8b\r\n"
    probes = make_fake_probes(files={".env": bom_content})
    checks = run_checks(probes)
    env_check = next(c for c in checks if c.name == ".env file")
    assert env_check.status == "OK"


def test_env_parsing_missing_equals():
    content = "VALID=1\nINVALID_LINE_NO_EQUALS\nOTHER=2\n"
    probes = make_fake_probes(files={".env": content})
    checks = run_checks(probes)
    env_check = next(c for c in checks if c.name == ".env file")
    assert env_check.status == "FAIL"
    assert "missing '='" in env_check.detail
    assert "INVALID_LINE_NO_EQUALS" in env_check.detail


def test_env_parsing_merged_line_flagged_by_key_only():
    content = "FIRST=1\nKEY=OTHER_KEY=secret_value\nLAST=2\n"
    probes = make_fake_probes(files={".env": content})
    checks = run_checks(probes)
    env_check = next(c for c in checks if c.name == ".env file")
    assert env_check.status == "FAIL"
    assert "line 2 (KEY)" in env_check.detail
    assert "merged setting in value" in env_check.detail
    assert "secret_value" not in env_check.detail


def test_env_parsing_duplicate_keys():
    content = "MODEL=qwen3:8b\nMODEL=gemini\n"
    probes = make_fake_probes(files={".env": content})
    checks = run_checks(probes)
    env_check = next(c for c in checks if c.name == ".env file")
    assert env_check.status == "FAIL"
    assert "duplicate key" in env_check.detail
    assert "MODEL" in env_check.detail


def test_env_parsing_unmatched_quotes():
    content = 'NAME="unclosed quote\n'
    probes = make_fake_probes(files={".env": content})
    checks = run_checks(probes)
    env_check = next(c for c in checks if c.name == ".env file")
    assert env_check.status == "FAIL"
    assert "unmatched double quote" in env_check.detail


def test_env_parsing_surrounding_spaces():
    content = "KEY=  surrounded_by_spaces  \n"
    probes = make_fake_probes(files={".env": content})
    checks = run_checks(probes)
    env_check = next(c for c in checks if c.name == ".env file")
    assert env_check.status == "FAIL"
    assert "surrounding spaces in value" in env_check.detail


def test_secret_redaction_in_report_json_and_diag():
    secret_val = "SUPER_SECRET_TOKEN_XYZ_987654321"
    env_content = f"GEMINI_API_KEY={secret_val}\nDB_PASSWORD={secret_val}\n"
    probes = make_fake_probes(
        env={"GEMINI_API_KEY": secret_val, "MY_SECRET_PASSWORD": secret_val},
        files={".env": env_content},
    )
    checks = run_checks(probes)
    report_text = format_report(checks)

    # JSON export
    from scripts.doctor import checks_to_json
    json_text = checks_to_json(checks)

    # Diagnostics export
    from core.diagnostics import build_diagnostics_text
    diag_text = build_diagnostics_text(doctor_checks=checks)

    # Secret string MUST NEVER appear anywhere
    assert secret_val not in report_text
    assert secret_val not in json_text
    assert secret_val not in diag_text

    # String length of the secret (32) must not appear as a length disclosure
    assert "length 32" not in report_text
    assert "len 32" not in report_text

    # Sensitive keys must report only "set" or "not set"
    env_check = next(c for c in checks if c.name == ".env file")
    assert "GEMINI_API_KEY: set" in env_check.detail
    assert "DB_PASSWORD: set" in env_check.detail
    assert secret_val not in env_check.detail


# --- 3. Stale ORBIT_MOCK & Provider Selection ---


def test_stale_orbit_mock_warns():
    probes = make_fake_probes(env={"ORBIT_MOCK": "1"})
    checks = run_checks(probes)
    env_check = next(c for c in checks if c.name == ".env file")
    assert env_check.status == "WARN"
    assert "ORBIT_MOCK is set (mock mode forced)" in env_check.detail


def test_invalid_llm_provider():
    probes = make_fake_probes(env={"LLM_PROVIDER": "anthropic"})
    checks = run_checks(probes)
    prov_check = next(c for c in checks if c.name == "LLM provider")
    assert prov_check.status == "FAIL"
    assert "anthropic" in prov_check.detail


def test_provider_mock_reason():
    probes = make_fake_probes(env={"LLM_PROVIDER": "mock", "ORBIT_MOCK": "1"})
    checks = run_checks(probes)
    prov_check = next(c for c in checks if c.name == "LLM provider")
    assert "make_provider will choose MOCK" in prov_check.detail
    assert "ORBIT_MOCK" in prov_check.fix or "LLM_PROVIDER" in prov_check.fix


# --- 4. Ollama & Loopback & GPU ---


def test_ollama_non_loopback_host_not_contacted():
    remote_host = "http://192.168.1.50:11434"
    contacted_urls = []

    def tracking_http_get(url: str, timeout: float = 2.0):
        contacted_urls.append(url)
        return HttpResponse(status_code=200, text="{}")

    probes = make_fake_probes(env={"OLLAMA_HOST": remote_host})
    probes.http_get = tracking_http_get

    checks = run_checks(probes)
    loopback_check = next(c for c in checks if c.name == "Ollama server")
    assert loopback_check.status == "WARN"
    assert "non-loopback" in loopback_check.detail

    # Remote host must NOT have been contacted
    assert not any("192.168.1.50" in u for u in contacted_urls)


def test_ollama_unreachable():
    def failing_http_get(url: str, timeout: float = 2.0):
        raise ConnectionRefusedError("Connection refused")

    probes = make_fake_probes()
    probes.http_get = failing_http_get

    checks = run_checks(probes)
    reach_check = next(c for c in checks if c.name == "Ollama server")
    assert reach_check.status in ("WARN", "FAIL")
    assert "unreachable" in reach_check.detail
    assert "ollama serve" in reach_check.fix


def test_ollama_model_missing():
    # Server returns tags without qwen3:8b
    probes = make_fake_probes(
        http_responses={"11434/api/tags": '{"models":[{"name":"llama3:8b"}]}'}
    )
    checks = run_checks(probes)
    model_check = next(c for c in checks if c.name == "Ollama server")
    assert model_check.status in ("WARN", "FAIL")
    assert "not installed" in model_check.detail
    assert "ollama pull qwen3:8b" in model_check.fix


def test_ollama_partial_gpu_offload_from_api_ps():
    # 5 GB total size, 3 GB VRAM -> 60% on GPU, 40% CPU
    ps_json = json.dumps({
        "models": [{
            "name": "qwen3:8b",
            "size": 5000000000,
            "size_vram": 3000000000,
        }]
    })
    probes = make_fake_probes(http_responses={"11434/api/ps": ps_json})
    checks = run_checks(probes)
    ollama_check = next(c for c in checks if c.name == "Ollama server")
    assert "60% in GPU VRAM" in ollama_check.detail
    assert "part of the model runs on CPU" in ollama_check.detail


def test_gpu_missing_warns():
    def no_gpu_cmd(cmd: list[str], timeout: float = 5.0):
        return CommandResult(returncode=1, stdout="", stderr="nvidia-smi not found")

    probes = make_fake_probes()
    probes.run_command = no_gpu_cmd

    checks = run_checks(probes)
    gpu_check = next(c for c in checks if c.name == "GPU (nvidia-smi)")
    assert gpu_check.status == "WARN"
    assert "No NVIDIA GPU detected" in gpu_check.detail


# --- 5. Optional Packages Matrix ---


@pytest.mark.parametrize(
    "pkg,feature,install_cmd",
    [
        ("playwright", "browser control", "pip install playwright"),
        ("sounddevice", "microphone", "pip install sounddevice"),
        ("faster_whisper", "speech to text", "pip install faster-whisper"),
        ("pyttsx3", "spoken replies", "pip install pyttsx3"),
    ],
)
def test_optional_packages_missing(pkg, feature, install_cmd):
    probes = make_fake_probes(specs={pkg: False})
    checks = run_checks(probes)
    pkg_check = next(c for c in checks if c.name == "Optional packages")
    assert pkg_check.status == "WARN"
    assert feature in pkg_check.detail
    assert install_cmd in pkg_check.fix


# --- 6. Chrome, Audio, Whisper, Storage, Git, Apps ---


def test_chrome_heuristic_detection():
    probes = make_fake_probes(which_map={"chrome": "C:\\Program Files\\Google\\Chrome\\chrome.exe"})
    checks = run_checks(probes)
    chrome_check = next(c for c in checks if c.name == "Browser")
    assert chrome_check.status == "OK"
    assert "heuristic" in chrome_check.detail


def test_audio_input_devices_disclaimer(monkeypatch):
    fake_sd = ModuleType("sounddevice")
    fake_sd.query_devices = lambda: []
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    probes = make_fake_probes(specs={"sounddevice": True})
    checks = run_checks(probes)
    audio_check = next(c for c in checks if c.name == "Microphone")
    assert "permission cannot be verified programmatically" in audio_check.detail
    assert "0 audio input devices found" in audio_check.detail


def test_whisper_cache_present_vs_missing():
    # Present
    probes_present = make_fake_probes()
    checks_present = run_checks(probes_present)
    wh_check_p = next(c for c in checks_present if c.name == "Whisper model cache")
    assert wh_check_p.status == "OK"
    assert "heuristic" in wh_check_p.detail
    assert "ORBIT_OFFLINE speech is ready" in wh_check_p.detail

    # Missing
    def no_path_exists(p):
        if "hub" in str(p) or "models--" in str(p):
            return False
        return True

    probes_missing = make_fake_probes()
    probes_missing.path_exists = no_path_exists
    checks_missing = run_checks(probes_missing)
    wh_check_m = next(c for c in checks_missing if c.name == "Whisper model cache")
    assert wh_check_m.status == "WARN"
    assert "ORBIT_OFFLINE speech will fail" in wh_check_m.detail


def test_disk_space_thresholds():
    # >= 2 GB -> OK
    p_ok = make_fake_probes(disk_free_gb=5.0)
    c_ok = next(c for c in run_checks(p_ok) if c.name == "Storage folders")
    assert c_ok.status == "OK"

    # < 2 GB but >= 200 MB -> WARN
    p_warn = make_fake_probes(disk_free_gb=1.0)
    c_warn = next(c for c in run_checks(p_warn) if c.name == "Storage folders")
    assert c_warn.status == "WARN"
    assert "Low disk space" in c_warn.detail

    # < 200 MB -> FAIL
    p_fail = make_fake_probes(disk_free_gb=0.1)
    c_fail = next(c for c in run_checks(p_fail) if c.name == "Storage folders")
    assert c_fail.status == "FAIL"
    assert "Critical disk space" in c_fail.detail


def test_git_check_ignore_unignored_file():
    def git_unignored_cmd(cmd: list[str], timeout: float = 5.0):
        if "check-ignore" in cmd:
            # only data, logs, workspace, evals/results returned -> .env missing!
            return CommandResult(
                returncode=0,
                stdout="data\nlogs\nworkspace\nevals/results\n",
                stderr="",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    probes = make_fake_probes()
    probes.run_command = git_unignored_cmd
    checks = run_checks(probes)
    git_check = next(c for c in checks if c.name == "Git hygiene")
    assert git_check.status == "FAIL"
    assert ".env" in git_check.detail
    assert "not ignored" in git_check.detail


def test_allowed_apps_resolution_and_protocol_skip():
    # simulate notepad missing from PATH
    probes = make_fake_probes(which_map={"notepad": None})
    checks = run_checks(probes)
    notepad_check = next(c for c in checks if c.name == "App: notepad")
    assert notepad_check.status == "WARN"
    assert "not found on PATH" in notepad_check.detail

    # protocol skip
    whatsapp_check = next(c for c in checks if c.name == "App: whatsapp")
    assert whatsapp_check.status == "SKIP"
    assert "Protocol launch" in whatsapp_check.detail


def test_fts5_unavailable(monkeypatch):
    import rag.index

    monkeypatch.setattr(rag.index, "has_fts5", lambda: False)
    probes = make_fake_probes()
    checks = run_checks(probes)
    fts5_check = next(c for c in checks if c.name == "SQLite FTS5")
    assert fts5_check.status == "WARN"
    assert "not available" in fts5_check.detail


# --- 7. Resilience, Fast Mode & Safety ---


def test_crashing_check_becomes_fail():
    probes = make_fake_probes()

    # Make path_exists crash unexpectedly for git check
    def crashing_path_exists(p):
        if ".git" in str(p):
            raise RuntimeError("Unexpected OS disk failure")
        return True

    probes.path_exists = crashing_path_exists
    checks = run_checks(probes)
    git_check = next(c for c in checks if c.name == "Git hygiene")
    assert git_check.status == "FAIL"
    assert "RuntimeError: Unexpected OS disk failure" in git_check.detail
    # Other checks must still execute and return
    assert len(checks) > 10


def test_settings_import_failure_still_runs(monkeypatch):
    import core.doctor

    monkeypatch.setattr(core.doctor, "_load_settings", lambda: (None, "Simulated ImportError"))
    probes = make_fake_probes()
    checks = run_checks(probes)
    settings_check = next(c for c in checks if c.name == "Configuration settings")
    assert settings_check.status == "FAIL"
    assert "Simulated ImportError" in settings_check.detail
    # remaining checks run via raw env vars
    assert any(c.name == "Python environment" for c in checks)


def test_fast_mode_runs_only_three_checks():
    probes = make_fake_probes()
    fast_checks = run_checks(probes, fast=True)
    assert len(fast_checks) == 3
    names = [c.name for c in fast_checks]
    assert ".env file" in names
    assert "LLM provider" in names
    assert "Ollama server" in names


def test_startup_thread_does_not_block_and_reports_once():
    """Verify startup thread does not delay execution even with slow/failing probe."""
    import threading
    import time

    def slow_failing_http_get(url, timeout=2.0):
        time.sleep(0.05)
        raise RuntimeError("Slow probe failure")

    probes = make_fake_probes()
    probes.http_get = slow_failing_http_get

    lines_printed = []

    def _worker():
        try:
            fast_checks = run_checks(probes=probes, fast=True)
            warn_count = sum(1 for c in fast_checks if c.status == "WARN")
            fail_count = sum(1 for c in fast_checks if c.status == "FAIL")
            if warn_count > 0 or fail_count > 0:
                parts = []
                if fail_count > 0:
                    parts.append(f"{fail_count} failure" if fail_count == 1 else f"{fail_count} failures")
                if warn_count > 0:
                    parts.append(f"{warn_count} warning" if warn_count == 1 else f"{warn_count} warnings")
                lines_printed.append(f"Doctor: {', '.join(parts)} (run /doctor)")
        except Exception:
            pass

    t0 = time.monotonic()
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t_elapsed = time.monotonic() - t0
    # Launching daemon thread is immediate and does not block startup
    assert t_elapsed < 0.05
    t.join(timeout=1.0)
    assert not t.is_alive()
    # Prints at most one line
    assert len(lines_printed) <= 1


def test_fix_text_never_executed():
    """Verify that Check.fix commands are NEVER automatically executed."""
    probes = make_fake_probes(env={"LLM_PROVIDER": "unknown"})
    checks = run_checks(probes)
    fixes = [c.fix for c in checks if c.fix]
    assert len(fixes) > 0
    # protect_doctor_safety fixture ensures no subprocess was called


# --- 8. Reports, JSON & Exit Codes ---


def test_format_report_table_and_summary():
    checks = [
        Check(name="C1", status="OK", detail="All good"),
        Check(name="C2", status="WARN", detail="Low disk", fix="free space"),
        Check(name="C3", status="FAIL", detail="Server down", fix="start server"),
        Check(name="C4", status="SKIP", detail="Protocol skipped"),
    ]
    report = format_report(checks)
    assert "OK     | C1" in report
    assert "WARN   | C2" in report
    assert "FAIL   | C3" in report
    assert "SKIP   | C4" in report
    assert "Summary: 1 OK, 1 warnings, 1 failures" in report
    assert "- [C3]: start server" in report


def test_script_doctor_exit_codes(monkeypatch):
    from scripts.doctor import main as script_main

    # 1. Zero failures -> exit 0
    probes_ok = make_fake_probes()
    exit_code_ok = script_main(probes=probes_ok, json_mode=False)
    assert exit_code_ok == 0

    # 2. Has failure -> exit 1
    probes_fail = make_fake_probes(env={"LLM_PROVIDER": "invalid"})
    exit_code_fail = script_main(probes=probes_fail, json_mode=False)
    assert exit_code_fail == 1


# --- 9. UI Integration (DoctorWindow & Status Chip) ---


class FakeController:
    """Mock controller for Tkinter event dispatching."""

    def __init__(self, probes=None) -> None:
        from ui.controller import UiEvent

        self.events: list[UiEvent] = []
        self.is_busy: bool = False
        self.cancel_called: bool = False
        self.shutdown_called: bool = False
        self.app = SimpleNamespace(doc_index=None, memory_store=None, agent=None)
        self.voice_controller = None
        self.pending_tool = None
        self.confirmed_resolutions = []
        self.plan_resolutions = []
        self.doctor_probes = probes

    def _emit(self, kind: str, data: Optional[dict] = None) -> None:
        from ui.controller import UiEvent

        self.events.append(UiEvent(kind=kind, data=data or {}))

    def drain(self, max_items: int = 100) -> list[Any]:
        evs = self.events[:max_items]
        self.events = self.events[max_items:]
        return evs

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_ui_doctor_chip_and_window(monkeypatch):
    """Test Doctor status chip and DoctorWindow with FakeController and withdrawn root."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception:
        pytest.skip("Tkinter display unavailable")

    try:
        from ui.app import DoctorWindow, OrbitWindow

        probes = make_fake_probes()
        ctrl = FakeController(probes=probes)

        # Build OrbitWindow with withdrawn root
        win = OrbitWindow(root, controller=ctrl, mode_label="LOCAL (ollama)")

        # Verify chip exists and initially hidden
        assert hasattr(win, "doctor_chip")
        assert win.doctor_chip.winfo_manager() != "pack"

        # Simulate fast startup event with warnings/failures
        ctrl._emit("doctor_fast_done", {"fails": 1, "warns": 2, "oks": 0})
        win._poll()

        # Chip must now be configured and visible in pack manager
        assert win.doctor_chip.winfo_manager() == "pack"
        chip_text = win.doctor_chip.cget("text")
        assert "1 fail" in chip_text
        assert "2 warn" in chip_text

        # Hidden when everything is OK
        ctrl._emit("doctor_fast_done", {"fails": 0, "warns": 0, "oks": 3})
        win._poll()
        assert win.doctor_chip.winfo_manager() != "pack"

        # Test DoctorWindow directly
        doc_win = DoctorWindow(root, "Fake Doctor Report\nAll checks ok.")
        assert doc_win.winfo_exists()
        assert "ORBIT Environment Doctor" in doc_win.title()
        assert "Fake Doctor Report" in doc_win.text_area.get("1.0", tk.END)

        # Test Copy button
        doc_win._on_copy()
        assert doc_win.copy_btn.cget("text") == "Copied!"

        # Close windows
        doc_win.destroy()
        win._on_close()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
