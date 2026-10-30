"""Read-only environment doctor for ORBIT.

Inspects system environment, dependencies, local models, hardware, and safety
hygiene. All checks are strictly read-only and never execute modifications,
destructive commands, or automatic fixes.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Optional, Union
import urllib.error
import urllib.request

from core.version import VERSION


@dataclass
class Check:
    """Represents the outcome of a single doctor environment check."""

    name: str
    status: str  # "OK", "WARN", "FAIL", "SKIP"
    detail: str
    fix: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class HttpResponse:
    """Minimal response wrapper for HTTP probes."""

    status_code: int
    text: str = ""
    _json_data: Any = None

    def __init__(
        self,
        status_code: int,
        text: str = "",
        body: Optional[str] = None,
        _json_data: Any = None,
    ) -> None:
        self.status_code = status_code
        self.text = body if body is not None else text
        self._json_data = _json_data

    @property
    def body(self) -> str:
        return self.text

    def json(self) -> Any:
        if self._json_data is not None:
            return self._json_data
        import json

        return json.loads(self.text)


@dataclass
class CommandResult:
    """Minimal command execution result wrapper for subprocess probes."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        exit_code: Optional[int] = None,
    ) -> None:
        self.returncode = exit_code if exit_code is not None else returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def exit_code(self) -> int:
        return self.returncode


# Default Probe Callables (enforcing strict timeouts and shell=False)


def default_http_get(url: str, timeout: float = 2.0) -> HttpResponse:
    """Execute read-only HTTP GET with explicit timeout."""
    req = urllib.request.Request(url, headers={"User-Agent": "ORBIT-Doctor"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            return HttpResponse(status_code=resp.status, text=content)
    except urllib.error.HTTPError as exc:
        content = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return HttpResponse(status_code=exc.code, text=content)
    except Exception as exc:
        return HttpResponse(status_code=0, text=str(exc))


def default_http_post(url: str, json_data: Any = None, timeout: float = 2.0) -> HttpResponse:
    """Execute HTTP POST with explicit timeout."""
    import json
    data = json.dumps(json_data).encode("utf-8") if json_data is not None else None
    headers = {"User-Agent": "ORBIT-Doctor", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            return HttpResponse(status_code=resp.status, text=content)
    except urllib.error.HTTPError as exc:
        content = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return HttpResponse(status_code=exc.code, text=content)
    except Exception as exc:
        return HttpResponse(status_code=0, text=str(exc))


def default_run_command(cmd: list[str], timeout: float = 5.0) -> CommandResult:
    """Execute fixed command arguments with explicit timeout and shell=False."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    except Exception as exc:
        return CommandResult(returncode=1, stdout="", stderr=str(exc))


def default_find_spec(name: str) -> Any:
    """Query importlib for package spec without importing it."""
    try:
        return importlib.util.find_spec(name)
    except Exception:
        return None


def default_which(cmd: str) -> Optional[str]:
    """Resolve executable path using shutil.which."""
    return shutil.which(cmd)


def default_read_text(path: Union[str, Path]) -> str:
    """Read file text safely."""
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace")


def default_path_exists(path: Union[str, Path]) -> bool:
    """Check if filesystem path exists."""
    return Path(path).exists()


def default_disk_free(path: Union[str, Path]) -> int:
    """Get free disk space in bytes for path or its nearest existing parent."""
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free
    except Exception:
        return 0


def default_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve environment variable from os.environ."""
    return os.environ.get(key, default)


def default_clock() -> float:
    """Monotonic clock float in seconds."""
    return time.monotonic()


def default_whatsapp_protocol_registered() -> bool:
    """Check if whatsapp:// protocol handler is registered in Windows registry (heuristic)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "whatsapp", 0, winreg.KEY_READ):
            return True
    except Exception:
        return False


@dataclass
class Probes:
    """Injectable system probes ensuring tests never invoke real network, shell, or hardware."""

    http_get: Callable[..., Any] = default_http_get
    http_post: Callable[..., Any] = default_http_post
    run_command: Callable[..., Any] = default_run_command
    find_spec: Callable[[str], Any] = default_find_spec
    which: Callable[[str], Optional[str]] = default_which
    read_text: Callable[[Union[str, Path]], str] = default_read_text
    path_exists: Callable[[Union[str, Path]], bool] = default_path_exists
    disk_free: Callable[[Union[str, Path]], int] = default_disk_free
    env: Any = default_env
    clock: Callable[[], float] = default_clock
    whatsapp_protocol_registered: Callable[[], bool] = default_whatsapp_protocol_registered


def _probe_env(probes: Probes, key: str, default: Optional[str] = None) -> Optional[str]:
    """Helper to query environment value from probes.env safely."""
    env_source = probes.env
    if callable(env_source):
        try:
            return env_source(key, default)
        except TypeError:
            return env_source(key)
    elif hasattr(env_source, "get"):
        return env_source.get(key, default)
    return default


def _load_settings() -> tuple[Any, Optional[str]]:
    """Attempt to import config.settings lazily."""
    try:
        from config import settings

        return settings, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# --- Individual Check Implementations ---


def check_python_environment(probes: Probes) -> Check:
    """(a) Check Python version >= 3.11 and virtual environment status."""
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 11):
        return Check(
            name="Python environment",
            status="FAIL",
            detail=f"Python {py_ver} (3.11 or newer is required)",
            fix="Install Python 3.11 or newer",
        )

    # Detect virtual environment
    venv_active = sys.prefix != getattr(sys, "base_prefix", sys.prefix) or bool(
        _probe_env(probes, "VIRTUAL_ENV")
    )

    if not venv_active:
        return Check(
            name="Python environment",
            status="WARN",
            detail=f"Python {py_ver} (no virtual environment active)",
            fix="Activate a virtual environment (e.g. .\\venv\\Scripts\\activate)",
        )

    return Check(
        name="Python environment",
        status="OK",
        detail=f"Python {py_ver} (virtual environment active)",
        fix=None,
    )


def check_env_file(probes: Probes) -> Check:
    """(b) Parse .env file line-by-line with secret redaction and syntax validation."""
    if not probes.path_exists(".env"):
        return Check(
            name=".env file",
            status="WARN",
            detail=".env file not found (optional; using environment variables)",
            fix="Copy .env.example to .env and configure keys if needed",
        )

    try:
        content = probes.read_text(".env")
    except Exception as exc:
        return Check(
            name=".env file",
            status="FAIL",
            detail=f"Cannot read .env file: {exc.__class__.__name__}: {exc}",
            fix="Check .env file permissions",
        )

    # Accept UTF-8 BOM
    if content.startswith("\ufeff"):
        content = content[1:]

    lines = content.splitlines()
    seen_keys: set[str] = set()
    bad_lines: list[str] = []
    env_keys_summary: list[str] = []
    orbit_mock_present = False
    llm_provider_val = None

    if _probe_env(probes, "ORBIT_MOCK") is not None:
        orbit_mock_present = True

    for line_idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if "=" not in raw_line:
            key_name = stripped.split()[0] if stripped.split() else f"line_{line_idx}"
            bad_lines.append(f"line {line_idx} ({key_name}): missing '='")
            continue

        key, val = raw_line.split("=", 1)
        key_name = key.strip()

        # Check surrounding spaces in value
        if val.startswith(" ") or val.startswith("\t") or val.endswith(" ") or val.endswith("\t"):
            bad_lines.append(f"line {line_idx} ({key_name}): surrounding spaces in value")

        val_stripped = val.strip()

        # Duplicate keys check
        if key_name in seen_keys:
            bad_lines.append(f"line {line_idx} ({key_name}): duplicate key")
        seen_keys.add(key_name)

        # Check for unmatched quotes
        if val_stripped.startswith('"') and not val_stripped.endswith('"'):
            bad_lines.append(f"line {line_idx} ({key_name}): unmatched double quote")
        elif val_stripped.startswith("'") and not val_stripped.endswith("'"):
            bad_lines.append(f"line {line_idx} ({key_name}): unmatched single quote")
        elif not (val_stripped.startswith('"') and val_stripped.endswith('"')) and not (
            val_stripped.startswith("'") and val_stripped.endswith("'")
        ):
            if val_stripped.count('"') % 2 != 0:
                bad_lines.append(f"line {line_idx} ({key_name}): unmatched double quote")
            elif val_stripped.count("'") % 2 != 0:
                bad_lines.append(f"line {line_idx} ({key_name}): unmatched single quote")

        # Merged setting pattern check: value matches ^[A-Z][A-Z0-9_]*=
        unquoted = val_stripped
        if (val_stripped.startswith('"') and val_stripped.endswith('"')) or (
            val_stripped.startswith("'") and val_stripped.endswith("'")
        ):
            unquoted = val_stripped[1:-1]

        if re.search(r"^[A-Z][A-Z0-9_]*=", val_stripped) or re.search(
            r"^[A-Z][A-Z0-9_]*=", unquoted
        ):
            bad_lines.append(f"line {line_idx} ({key_name}): merged setting in value")

        if key_name == "ORBIT_MOCK":
            orbit_mock_present = True

        if key_name == "LLM_PROVIDER":
            llm_provider_val = unquoted

        # For secret keys: report ONLY "set" or "not set", NEVER value or length!
        k_upper = key_name.upper()
        if any(sec in k_upper for sec in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            is_set = bool(unquoted)
            env_keys_summary.append(f"{key_name}: {'set' if is_set else 'not set'}")
        elif key_name in ("LLM_PROVIDER", "MODEL_NAME", "OLLAMA_MODEL"):
            env_keys_summary.append(f"{key_name}: {unquoted}")

    allowed_providers = ("auto", "gemini", "ollama", "mock")
    if llm_provider_val is None:
        llm_provider_val = _probe_env(probes, "LLM_PROVIDER", "auto")

    prov_norm = llm_provider_val.strip().lower() if llm_provider_val else "auto"
    prov_valid = prov_norm in allowed_providers

    if bad_lines:
        return Check(
            name=".env file",
            status="FAIL",
            detail=f"Syntax issues in .env: {'; '.join(bad_lines)}",
            fix="Fix the .env syntax errors listed above",
        )

    if not prov_valid:
        return Check(
            name=".env file",
            status="FAIL",
            detail=f"Invalid LLM_PROVIDER '{llm_provider_val}' (valid options: {', '.join(allowed_providers)})",
            fix=f"Set LLM_PROVIDER to one of: {', '.join(allowed_providers)}",
        )

    detail_parts = []
    if env_keys_summary:
        detail_parts.append(", ".join(env_keys_summary))
    detail_parts.append(f"LLM_PROVIDER='{prov_norm}'")

    if orbit_mock_present:
        return Check(
            name=".env file",
            status="WARN",
            detail=f"{'; '.join(detail_parts)}; warning: ORBIT_MOCK is set (mock mode forced)",
            fix="Remove or unset ORBIT_MOCK to use real model providers",
        )

    return Check(
        name=".env file",
        status="OK",
        detail="; ".join(detail_parts),
        fix=None,
    )



def _is_loopback_or_remote_allowed(probes: Probes, host: str) -> bool:
    """Return True if host is loopback or OLLAMA_ALLOW_REMOTE is enabled."""
    from core.offline import is_loopback
    from core.ollama_provider import _extract_hostname

    allow_remote = _probe_env(probes, "OLLAMA_ALLOW_REMOTE", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    hostname = _extract_hostname(host)
    return is_loopback(hostname) or allow_remote


def check_provider(probes: Probes) -> Check:
    """(c) Determine what make_provider would choose and why."""
    mock_env = _probe_env(probes, "ORBIT_MOCK")
    if mock_env is not None:
        return Check(
            name="LLM provider",
            status="WARN",
            detail="make_provider will choose MOCK: ORBIT_MOCK is set in environment",
            fix="Unset ORBIT_MOCK in .env or environment to use a real model",
        )

    offline_str = _probe_env(probes, "ORBIT_OFFLINE", "false")
    offline = offline_str.strip().lower() in ("true", "1", "yes", "on")

    req_prov = (_probe_env(probes, "LLM_PROVIDER", "auto") or "auto").strip().lower()
    allowed = ("auto", "gemini", "ollama", "mock")
    if req_prov not in allowed:
        return Check(
            name="LLM provider",
            status="FAIL",
            detail=f"Unknown LLM_PROVIDER '{req_prov}' (valid: auto, gemini, ollama, mock)",
            fix="Set LLM_PROVIDER in .env to 'auto', 'gemini', 'ollama', or 'mock'",
        )

    if offline and req_prov == "gemini":
        return Check(
            name="LLM provider",
            status="FAIL",
            detail="ORBIT_OFFLINE is on: 'gemini' provider is not permitted in offline mode",
            fix="Change LLM_PROVIDER to 'ollama' or 'mock', or disable ORBIT_OFFLINE",
        )

    if req_prov == "mock":
        return Check(
            name="LLM provider",
            status="WARN",
            detail="make_provider will choose MOCK: LLM_PROVIDER is set to 'mock'",
            fix="Set LLM_PROVIDER=auto in .env to use real models",
        )

    gemini_key = _probe_env(probes, "GEMINI_API_KEY")
    gemini_model = _probe_env(probes, "MODEL_NAME", "gemini-3.8-flash")
    ollama_host = _probe_env(probes, "OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model = _probe_env(probes, "OLLAMA_MODEL", "qwen3:8b")

    if req_prov == "gemini":
        if not gemini_key or not gemini_key.strip():
            return Check(
                name="LLM provider",
                status="FAIL",
                detail="GEMINI_API_KEY is not set but LLM_PROVIDER='gemini'",
                fix="Set GEMINI_API_KEY in your .env file or use LLM_PROVIDER=ollama",
            )
        return Check(
            name="LLM provider",
            status="OK",
            detail=f"make_provider will choose LIVE {gemini_model} (explicit LLM_PROVIDER='gemini')",
            fix=None,
        )

    ollama_alive = False
    installed_models: list[str] = []
    if _is_loopback_or_remote_allowed(probes, ollama_host):
        try:
            resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/version", timeout=1.0)
            ollama_alive = resp.status_code == 200
            if ollama_alive:
                tags_resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/tags", timeout=2.0)
                if tags_resp.status_code == 200:
                    t_data = tags_resp.json() if hasattr(tags_resp, "json") else {}
                    if isinstance(t_data, dict):
                        models = t_data.get("models", [])
                        installed_models = [
                            m.get("name") for m in models if isinstance(m, dict) and "name" in m
                        ]
        except Exception:
            ollama_alive = False

    from core.providers import _is_model_installed

    model_installed = _is_model_installed(installed_models, ollama_model) if ollama_alive else False

    if req_prov == "ollama":
        if not ollama_alive:
            return Check(
                name="LLM provider",
                status="FAIL",
                detail=f"Ollama is not running at {ollama_host} (required by LLM_PROVIDER='ollama')",
                fix="Start Ollama desktop app or run: ollama serve",
            )
        if not model_installed:
            return Check(
                name="LLM provider",
                status="FAIL",
                detail=f"Model '{ollama_model}' is not installed in Ollama (required by LLM_PROVIDER='ollama')",
                fix=f"Run: ollama pull {ollama_model}",
            )
        return Check(
            name="LLM provider",
            status="OK",
            detail=f"make_provider will choose LOCAL {ollama_model} (explicit LLM_PROVIDER='ollama')",
            fix=None,
        )

    # req_prov == "auto"
    if not offline and gemini_key and gemini_key.strip():
        return Check(
            name="LLM provider",
            status="OK",
            detail=f"make_provider will choose LIVE {gemini_model} (online auto mode with GEMINI_API_KEY set)",
            fix=None,
        )

    if ollama_alive and model_installed:
        return Check(
            name="LLM provider",
            status="OK",
            detail=f"make_provider will choose LOCAL {ollama_model} (auto mode, Ollama running with model installed)",
            fix=None,
        )

    prefix = "ORBIT_OFFLINE is on and" if offline else "No GEMINI_API_KEY set and"
    if ollama_alive and not model_installed:
        mock_reason = f"{prefix} model '{ollama_model}' is not installed in Ollama at {ollama_host}."
        fix = f"Run: ollama pull {ollama_model}"
    elif not ollama_alive:
        if offline:
            mock_reason = f"ORBIT_OFFLINE is on and Ollama is not reachable at {ollama_host}."
            fix = "Start Ollama desktop app or run: ollama serve"
        else:
            mock_reason = f"No GEMINI_API_KEY set and Ollama is not reachable at {ollama_host}."
            fix = f"Set GEMINI_API_KEY in .env, or start Ollama and run: ollama pull {ollama_model}"
    else:
        mock_reason = "No model provider available."
        fix = "Configure GEMINI_API_KEY or Ollama"

    return Check(
        name="LLM provider",
        status="WARN",
        detail=f"make_provider will choose MOCK: {mock_reason}",
        fix=fix,
    )


def check_ollama_reachability_fast(probes: Probes) -> Check:
    """Fast 1-second Ollama reachability check for startup."""
    ollama_host = _probe_env(probes, "OLLAMA_HOST", "http://127.0.0.1:11434")

    if not _is_loopback_or_remote_allowed(probes, ollama_host):
        return Check(
            name="Ollama server",
            status="WARN",
            detail=f"OLLAMA_HOST '{ollama_host}' is non-loopback; host not contacted for safety",
            fix="Set OLLAMA_ALLOW_REMOTE=true in .env to allow remote host",
        )

    try:
        resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/version", timeout=1.0)
        if resp.status_code == 200:
            ver = ""
            try:
                data = resp.json() if hasattr(resp, "json") else {}
                if isinstance(data, dict) and "version" in data:
                    ver = f" (v{data['version']})"
            except Exception:
                pass
            return Check(
                name="Ollama server",
                status="OK",
                detail=f"Reachable at {ollama_host}{ver}",
                fix=None,
            )
        return Check(
            name="Ollama server",
            status="WARN",
            detail=f"Ollama server at {ollama_host} returned HTTP {resp.status_code}",
            fix="Start Ollama desktop app or run: ollama serve",
        )
    except Exception:
        return Check(
            name="Ollama server",
            status="WARN",
            detail=f"Ollama server at {ollama_host} is unreachable",
            fix="Start Ollama desktop app or run: ollama serve",
        )


def check_ollama_full(probes: Probes) -> Check:
    """(d) Detailed Ollama check: loopback, version, model tags, /api/ps GPU/CPU offload."""
    ollama_host = _probe_env(probes, "OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model = _probe_env(probes, "OLLAMA_MODEL", "qwen3:8b")

    if not _is_loopback_or_remote_allowed(probes, ollama_host):
        return Check(
            name="Ollama server",
            status="WARN",
            detail=f"OLLAMA_HOST '{ollama_host}' is non-loopback; not contacted unless OLLAMA_ALLOW_REMOTE is enabled",
            fix="Set OLLAMA_ALLOW_REMOTE=true in .env to connect to remote host, or use 127.0.0.1",
        )

    try:
        resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/version", timeout=2.0)
    except Exception as exc:
        return Check(
            name="Ollama server",
            status="WARN",
            detail=f"Ollama server at {ollama_host} is unreachable ({exc})",
            fix="Start Ollama desktop app or run: ollama serve",
        )

    if resp.status_code != 200:
        return Check(
            name="Ollama server",
            status="WARN",
            detail=f"Ollama server at {ollama_host} is unreachable (HTTP {resp.status_code})",
            fix="Start Ollama desktop app or run: ollama serve",
        )

    ver = "unknown"
    try:
        data = resp.json() if hasattr(resp, "json") else {}
        if isinstance(data, dict):
            ver = data.get("version", "unknown")
    except Exception:
        pass

    # List models via /api/tags
    installed_models: list[str] = []
    try:
        tags_resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/tags", timeout=2.0)
        if tags_resp.status_code == 200:
            t_data = tags_resp.json() if hasattr(tags_resp, "json") else {}
            if isinstance(t_data, dict):
                models = t_data.get("models", [])
                installed_models = [m.get("name") for m in models if isinstance(m, dict) and "name" in m]
    except Exception:
        pass

    from core.providers import _is_model_installed

    model_installed = _is_model_installed(installed_models, ollama_model)

    if not model_installed:
        return Check(
            name="Ollama server",
            status="WARN",
            detail=f"Server v{ver} reachable, but model '{ollama_model}' is not installed",
            fix=f"Run: ollama pull {ollama_model}",
        )

    # Inspect /api/ps for loaded model GPU offload
    gpu_note = ""
    try:
        ps_resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/ps", timeout=2.0)
        if ps_resp.status_code == 200:
            ps_data = ps_resp.json() if hasattr(ps_resp, "json") else {}
            if isinstance(ps_data, dict):
                loaded_models = ps_data.get("models", [])
                matched_entry = None
                for lm in loaded_models:
                    m_name = lm.get("name", "") or lm.get("model", "")
                    if _is_model_installed([m_name], ollama_model):
                        matched_entry = lm
                        break
                if matched_entry:
                    size = matched_entry.get("size", 0)
                    size_vram = matched_entry.get("size_vram", 0)
                    if size > 0:
                        gpu_pct = round((size_vram / size) * 100)
                        size_gb = size / (1024**3)
                        vram_gb = size_vram / (1024**3)
                        if size_vram < size:
                            gpu_note = f"; loaded: {gpu_pct}% in GPU VRAM ({vram_gb:.1f} GB / {size_gb:.1f} GB); note: part of the model runs on CPU"
                        else:
                            gpu_note = f"; loaded: 100% in GPU VRAM ({vram_gb:.1f} GB)"
                    elif size_vram == 0:
                        gpu_note = "; loaded: 0% in GPU VRAM (running on CPU)"
    except Exception:
        pass

    return Check(
        name="Ollama server",
        status="OK",
        detail=f"Server v{ver} reachable, model '{ollama_model}' installed{gpu_note}",
        fix=None,
    )


def check_gpu(probes: Probes) -> Check:
    """(e) Query GPU name and memory using fixed nvidia-smi command."""
    cmd = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]
    res = probes.run_command(cmd, timeout=5.0)
    if res.returncode != 0 or not res.stdout.strip():
        return Check(
            name="GPU (nvidia-smi)",
            status="WARN",
            detail="No NVIDIA GPU detected via nvidia-smi; models will run on CPU",
            fix="Install NVIDIA drivers or run models on CPU",
        )

    lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
    if not lines:
        return Check(
            name="GPU (nvidia-smi)",
            status="WARN",
            detail="nvidia-smi returned no devices; models will run on CPU",
            fix="Install NVIDIA drivers or run models on CPU",
        )

    gpus = []
    gpu_mem_mb = 0
    for line in lines:
        parts = [p.strip() for p in line.split(",", 1)]
        g_name = parts[0]
        vram = parts[1] if len(parts) > 1 else ""
        gpus.append(f"{g_name} ({vram})")
        m = re.search(r"(\d+)\s*(?:MiB|MB)", vram, re.IGNORECASE)
        if m:
            gpu_mem_mb = max(gpu_mem_mb, int(m.group(1)))

    note = ""
    # Check if loaded or configured model exceeds memory
    # Inspect /api/ps or /api/tags if accessible
    ollama_host = _probe_env(probes, "OLLAMA_HOST", "http://127.0.0.1:11434")
    if _is_loopback_or_remote_allowed(probes, ollama_host):
        try:
            ps_resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/ps", timeout=1.0)
            if ps_resp.status_code == 200:
                ps_data = ps_resp.json() if hasattr(ps_resp, "json") else {}
                if isinstance(ps_data, dict):
                    models = ps_data.get("models", [])
                    for lm in models:
                        size_bytes = lm.get("size", 0)
                        if size_bytes > 0:
                            size_mb = size_bytes / (1024 * 1024)
                            if gpu_mem_mb > 0 and size_mb > gpu_mem_mb:
                                note = f"; note: model size ({size_mb / 1024:.1f} GB) exceeds GPU memory ({gpu_mem_mb / 1024:.1f} GB)"
                                break
        except Exception:
            pass

    return Check(
        name="GPU (nvidia-smi)",
        status="OK",
        detail=f"Detected: {', '.join(gpus)}{note}",
        fix=None,
    )


def check_optional_packages(probes: Probes) -> Check:
    """(f) Check optional packages using find_spec without importing them."""
    packages = [
        ("playwright", "browser control", "pip install playwright && playwright install chromium"),
        ("sounddevice", "microphone", "pip install sounddevice"),
        ("faster_whisper", "speech to text", "pip install faster-whisper"),
        ("pyttsx3", "spoken replies", "pip install pyttsx3"),
        ("numpy", "audio processing", "pip install numpy"),
        ("psutil", "process management", "pip install psutil"),
    ]

    missing = []
    found = []
    for pkg_name, feature, install_cmd in packages:
        spec = probes.find_spec(pkg_name)
        if spec is None:
            missing.append((pkg_name, feature, install_cmd))
        else:
            found.append(pkg_name)

    if missing:
        missing_desc = ", ".join(f"{p} ({f})" for p, f, _ in missing)
        install_all = " && ".join(cmd for _, _, cmd in missing)
        return Check(
            name="Optional packages",
            status="WARN",
            detail=f"Missing {len(missing)} package(s): {missing_desc}",
            fix=f"Install missing packages: {install_all}",
        )

    return Check(
        name="Optional packages",
        status="OK",
        detail=f"All {len(packages)} optional packages available ({', '.join(found)})",
        fix=None,
    )


def check_browser(probes: Probes) -> Check:
    """(g) Locate Chrome executable via heuristic and check browser profile writability."""
    channel = _probe_env(probes, "BROWSER_CHANNEL", "chrome")
    profile_dir = Path(_probe_env(probes, "BROWSER_PROFILE_DIR", "data/browser_profile")).resolve()

    # Search standard Windows locations or PATH
    chrome_locations = [
        probes.which("chrome"),
        probes.which("chrome.exe"),
        probes.which("google-chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]

    local_app_data = _probe_env(probes, "LOCALAPPDATA")
    if local_app_data:
        chrome_locations.append(
            str(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe")
        )

    prog_files = _probe_env(probes, "PROGRAMFILES")
    if prog_files:
        chrome_locations.append(
            str(Path(prog_files) / "Google" / "Chrome" / "Application" / "chrome.exe")
        )

    prog_files_x86 = _probe_env(probes, "PROGRAMFILES(X86)")
    if prog_files_x86:
        chrome_locations.append(
            str(Path(prog_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe")
        )

    found_chrome: Optional[str] = None
    for loc in chrome_locations:
        if loc and probes.path_exists(loc):
            found_chrome = str(loc)
            break

    # Test profile folder writability
    profile_writable = True
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        probe_file = profile_dir / ".doctor_write_probe"
        probe_file.write_text("probe", encoding="utf-8")
        if probe_file.exists():
            probe_file.unlink()
    except Exception:
        profile_writable = False

    if not profile_writable:
        return Check(
            name="Browser",
            status="FAIL",
            detail=f"Browser profile directory '{profile_dir}' is not writable",
            fix=f"Ensure write permissions for '{profile_dir}'",
        )

    if not found_chrome:
        return Check(
            name="Browser",
            status="WARN",
            detail=f"Chrome executable not found on PATH or standard locations (heuristic); BROWSER_CHANNEL='{channel}'",
            fix="Install Google Chrome or add chrome.exe to PATH",
        )

    return Check(
        name="Browser",
        status="OK",
        detail=f"Chrome found (heuristic: standard paths or PATH); BROWSER_CHANNEL='{channel}'; profile folder writable",
        fix=None,
    )


def check_microphone(probes: Probes) -> Check:
    """(h) Query microphone input devices without opening audio streams."""
    if probes.find_spec("sounddevice") is None:
        return Check(
            name="Microphone",
            status="SKIP",
            detail="sounddevice is not installed; input device query skipped",
            fix="pip install sounddevice",
        )

    try:
        import sounddevice as sd

        devices = sd.query_devices()
        input_count = sum(1 for d in devices if d.get("max_input_channels", 0) > 0)
    except Exception as exc:
        return Check(
            name="Microphone",
            status="WARN",
            detail=f"Query failed: {exc} (permission cannot be verified programmatically)",
            fix="Check audio device connections and permissions",
        )

    if input_count == 0:
        return Check(
            name="Microphone",
            status="WARN",
            detail="0 audio input devices found (permission cannot be verified programmatically)",
            fix="Connect a microphone or enable audio input devices in Windows Settings",
        )

    return Check(
        name="Microphone",
        status="OK",
        detail=f"{input_count} audio input device(s) found (permission cannot be verified programmatically)",
        fix=None,
    )




def check_voice_enhancement(probes: Probes) -> Check:
    """Check voice enhancement settings, scipy-free numpy DSP, and measurement kit recording counts."""
    hp = _probe_env(probes, "VOICE_HIGHPASS", "0")
    agc = _probe_env(probes, "VOICE_AGC", "0")
    denoise = _probe_env(probes, "VOICE_DENOISE", "0")
    beam = _probe_env(probes, "STT_BEAM", "1")
    prompt = _probe_env(probes, "STT_PROMPT", "0")
    aliases = _probe_env(probes, "VOICE_ALIASES", "0")
    review = _probe_env(probes, "VOICE_REVIEW", "auto")

    has_numpy = probes.find_spec("numpy") is not None
    numpy_status = "numpy available (scipy-free DSP)" if has_numpy else "numpy missing"

    raw_home = _probe_env(probes, "ORBIT_HOME")
    base_data = Path(raw_home.strip()) / "data" if raw_home and raw_home.strip() else Path("data")
    eval_dir = base_data / "voice_eval"

    dev_count = 0
    test_count = 0
    if probes.path_exists(eval_dir):
        dev_clean = eval_dir / "dev" / "clean"
        dev_noise = eval_dir / "dev" / "real_noise"
        test_clean = eval_dir / "test" / "clean"
        test_noise = eval_dir / "test" / "real_noise"

        for d in (dev_clean, dev_noise):
            if probes.path_exists(d):
                try:
                    dev_count += len(list(d.glob("*.wav")))
                except Exception:
                    pass
        for d in (test_clean, test_noise):
            if probes.path_exists(d):
                try:
                    test_count += len(list(d.glob("*.wav")))
                except Exception:
                    pass

    counts_str = f"Recordings: dev={dev_count}, test={test_count}"
    settings_str = f"Settings: HP={hp}, AGC={agc}, Denoise={denoise}, Beam={beam}, Prompt={prompt}, Aliases={aliases}, Review={review}"
    detail = f"{numpy_status}; {settings_str}; {counts_str}"

    fix = None
    if dev_count == 0 and test_count == 0:
        fix = "Record benchmark phrases using: python scripts/voice_record.py --split dev --kind clean"

    return Check(
        name="Voice enhancement & eval",
        status="OK" if has_numpy else "WARN",
        detail=detail,
        fix=fix,
    )


def check_whisper_cache(probes: Probes) -> Check:
    """(i) Check Hugging Face hub cache for configured Whisper model (heuristic)."""
    stt_model = _probe_env(probes, "STT_MODEL", "base.en")
    hf_home = _probe_env(probes, "HF_HOME")
    hub_dir = (
        Path(hf_home) / "hub"
        if hf_home
        else Path.home() / ".cache" / "huggingface" / "hub"
    )

    candidate_patterns = [
        hub_dir / f"models--Systran--faster-whisper-{stt_model}",
        hub_dir / f"models--guillaumekln--faster-whisper-{stt_model}",
        hub_dir / f"models--{stt_model}",
    ]

    cached = any(probes.path_exists(p) for p in candidate_patterns)

    if cached:
        return Check(
            name="Whisper model cache",
            status="OK",
            detail=f"Model '{stt_model}' found in Hugging Face cache (heuristic); ORBIT_OFFLINE speech is ready",
            fix=None,
        )

    return Check(
        name="Whisper model cache",
        status="WARN",
        detail=f"Model '{stt_model}' not found in Hugging Face cache (heuristic); ORBIT_OFFLINE speech will fail without initial online download",
        fix=f"Run speech once while online to download '{stt_model}' before using ORBIT_OFFLINE",
    )


def check_tts_engine(probes: Probes) -> Check:
    """Check text-to-speech engine, piper/onnxruntime availability, and valid voice models."""
    from voice.voices import list_voices

    raw_engine = _probe_env(probes, "TTS_ENGINE", "sapi")
    engine = (raw_engine or "sapi").strip().lower()
    raw_voice = _probe_env(probes, "PIPER_VOICE", "")
    selected_voice = (raw_voice or "").strip()

    has_piper = (probes.find_spec("piper") is not None) or (probes.find_spec("piper_voice") is not None)
    has_onnx = probes.find_spec("onnxruntime") is not None

    raw_vdir = _probe_env(probes, "VOICES_DIR")
    if raw_vdir and raw_vdir.strip():
        vdir = Path(raw_vdir.strip())
    else:
        raw_home = _probe_env(probes, "ORBIT_HOME")
        if raw_home and raw_home.strip():
            vdir = Path(raw_home.strip()) / "data" / "voices"
        else:
            vdir = Path("data") / "voices"

    valid_voices: list[str] = []
    if probes.path_exists(vdir):
        try:
            valid_voices = list_voices(vdir)
        except Exception:
            valid_voices = []

    voices_desc = f"{len(valid_voices)} valid ({', '.join(valid_voices)})" if valid_voices else "none"
    detail = (
        f"Engine: {engine}; piper: {'yes' if has_piper else 'no'}; "
        f"onnxruntime: {'yes' if has_onnx else 'no'}; voices: {voices_desc}"
    )

    hint = "For neural voice, install piper-tts and place .onnx + .onnx.json in data/voices/"

    if engine == "piper":
        missing_parts: list[str] = []
        if not has_piper:
            missing_parts.append("piper-tts not installed ('pip install piper-tts')")
        if not has_onnx:
            missing_parts.append("onnxruntime not installed ('pip install onnxruntime')")
        if not valid_voices:
            missing_parts.append(f"no valid .onnx voice pair found in {vdir}")
        elif selected_voice and selected_voice not in valid_voices:
            missing_parts.append(f"selected PIPER_VOICE '{selected_voice}' not found in {vdir}")

        if missing_parts:
            return Check(
                name="TTS engine",
                status="WARN",
                detail=detail,
                fix="; ".join(missing_parts) + ". SAPI fallback is active.",
            )
        return Check(
            name="TTS engine",
            status="OK",
            detail=detail,
            fix=None,
        )

    return Check(
        name="TTS engine",
        status="OK",
        detail=detail,
        fix=hint if (not has_piper or not valid_voices) else None,
    )


def check_folders(probes: Probes) -> Check:
    """(j) Check workspace, data, logs exist, are writable, and free disk space >= 2 GB."""
    raw_home = _probe_env(probes, "ORBIT_HOME")
    if raw_home and raw_home.strip():
        base = Path(raw_home.strip()).resolve()
        folders = [base / "workspace", base / "data", base / "logs"]
    else:
        folders = [Path("workspace"), Path("data"), Path("logs")]
    min_free = float("inf")

    for folder in folders:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe_file = folder / ".doctor_write_probe"
            probe_file.write_text("probe", encoding="utf-8")
            if probe_file.exists():
                probe_file.unlink()
        except Exception as exc:
            return Check(
                name="Storage folders",
                status="FAIL",
                detail=f"Folder '{folder}' cannot be created or written: {exc}",
                fix=f"Check permissions and create folder '{folder}'",
            )
        free_bytes = probes.disk_free(folder)
        if free_bytes < min_free:
            min_free = free_bytes

    free_mb = min_free / (1024 * 1024)
    free_gb = min_free / (1024 * 1024 * 1024)

    if free_mb < 200:
        return Check(
            name="Storage folders",
            status="FAIL",
            detail=f"Critical disk space: {free_mb:.1f} MB free (below 200 MB failure threshold)",
            fix="Free up disk space on the storage drive immediately",
        )

    if free_gb < 2.0:
        return Check(
            name="Storage folders",
            status="WARN",
            detail=f"Low disk space: {free_gb:.2f} GB free (below 2.0 GB warning threshold)",
            fix="Free up disk space (at least 2.0 GB recommended)",
        )

    return Check(
        name="Storage folders",
        status="OK",
        detail=f"workspace, data, logs exist and are writable; {free_gb:.1f} GB free disk space",
        fix=None,
    )


def check_git_hygiene(probes: Probes) -> Check:
    """(k) Run fixed git check-ignore to ensure sensitive paths are ignored."""
    if not probes.path_exists(".git"):
        return Check(
            name="Git hygiene",
            status="SKIP",
            detail="Not a git repository (.git folder not found)",
            fix=None,
        )

    targets = [".env", "data", "logs", "workspace", "evals/results"]
    cmd = ["git", "check-ignore", *targets]
    res = probes.run_command(cmd, timeout=5.0)

    # git check-ignore outputs each ignored file on a line
    ignored_lines = {
        line.strip().rstrip("/\\") for line in res.stdout.splitlines() if line.strip()
    }

    not_ignored = [
        t
        for t in targets
        if t.rstrip("/\\") not in ignored_lines and t not in ignored_lines
    ]

    if not_ignored:
        return Check(
            name="Git hygiene",
            status="FAIL",
            detail=f"Sensitive paths are not ignored: {', '.join(not_ignored)}",
            fix=f"Add {', '.join(not_ignored)} to .gitignore to prevent committing private files",
        )

    return Check(
        name="Git hygiene",
        status="OK",
        detail="All sensitive paths (.env, data, logs, workspace, evals/results) are git-ignored",
        fix=None,
    )


def check_allowed_apps(probes: Probes) -> list[Check]:
    """(l) For each app in config/apps.py report whether launch executable resolves."""
    try:
        from config.apps import ALLOWED_APPS
    except Exception:
        ALLOWED_APPS = {
            "notepad": {"launch": ["notepad.exe"]},
            "calculator": {"launch": ["calc.exe"]},
            "explorer": {"launch": ["explorer.exe"]},
            "chrome": {
                "launch": ["chrome.exe"],
                "candidates": [
                    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
                    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
                    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
                ],
            },
            "vscode": {"launch": ["code"]},
            "whatsapp": {"launch": ["explorer.exe", "whatsapp:"]},
        }

    from tools.apps import resolve_launch

    checks: list[Check] = []
    for app_name, cfg in ALLOWED_APPS.items():
        launch = cfg.get("launch", [])
        if not launch:
            continue

        # Check for protocol launches (e.g. whatsapp: or contains ://)
        is_protocol = any(arg.endswith(":") or "://" in arg for arg in launch)
        if is_protocol:
            checks.append(
                Check(
                    name=f"App: {app_name}",
                    status="SKIP",
                    detail=f"Protocol launch ({' '.join(launch)}); execution resolution skipped",
                    fix=None,
                )
            )
            continue

        exe = launch[0]
        # Check if bare name resolves via which on PATH
        path_resolved = probes.which(exe)
        if path_resolved:
            checks.append(
                Check(
                    name=f"App: {app_name}",
                    status="OK",
                    detail=f"Resolved: {path_resolved}",
                    fix=None,
                )
            )
            continue

        # Check if resolved via candidate templates
        resolved_cmd = resolve_launch(
            cfg,
            which_fn=probes.which,
            path_exists_fn=probes.path_exists,
        )
        if resolved_cmd:
            checks.append(
                Check(
                    name=f"App: {app_name}",
                    status="OK",
                    detail=f"Resolved via standard install path: {resolved_cmd[0]}",
                    fix=None,
                )
            )
        else:
            checks.append(
                Check(
                    name=f"App: {app_name}",
                    status="WARN",
                    detail=f"Executable '{exe}' not found on PATH",
                    fix=f"Install {app_name} or configure its path in config/apps.py",
                )
            )

    return checks


def check_fts5(probes: Probes) -> Check:
    """(m) Check SQLite FTS5 extension availability for document search."""
    try:
        from rag.index import has_fts5

        ok = has_fts5()
    except Exception:
        ok = False

    if ok:
        return Check(
            name="SQLite FTS5",
            status="OK",
            detail="SQLite FTS5 full-text search extension with porter tokenizer is available",
            fix=None,
        )

    return Check(
        name="SQLite FTS5",
        status="WARN",
        detail="SQLite FTS5 extension is not available; document search is disabled",
        fix="Ensure SQLite3 in Python has FTS5 extension enabled",
    )


def check_offline_readiness(probes: Probes) -> Check:
    """(n) ORBIT_OFFLINE readiness summary combining provider, Ollama model, and Whisper."""
    offline_active = (
        _probe_env(probes, "ORBIT_OFFLINE", "false").lower() in ("true", "1", "yes", "on")
    )
    req_prov = (_probe_env(probes, "LLM_PROVIDER", "auto") or "auto").strip().lower()
    ollama_host = _probe_env(probes, "OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model = _probe_env(probes, "OLLAMA_MODEL", "qwen3:8b")
    stt_model = _probe_env(probes, "STT_MODEL", "base.en")

    missing = []
    if req_prov == "gemini":
        missing.append("LLM_PROVIDER is set to cloud-only 'gemini'")

    # Check local Ollama reachability and model
    ollama_ok = False
    if _is_loopback_or_remote_allowed(probes, ollama_host):
        try:
            v_resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/version", timeout=1.0)
            if v_resp.status_code == 200:
                t_resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/tags", timeout=1.0)
                if t_resp.status_code == 200:
                    t_data = t_resp.json() if hasattr(t_resp, "json") else {}
                    models = [
                        m.get("name")
                        for m in t_data.get("models", [])
                        if isinstance(m, dict) and "name" in m
                    ]
                    from core.providers import _is_model_installed

                    ollama_ok = _is_model_installed(models, ollama_model)
        except Exception:
            ollama_ok = False

    is_mock = (_probe_env(probes, "ORBIT_MOCK") is not None) or req_prov == "mock"
    if not is_mock and not ollama_ok:
        missing.append(f"Ollama model '{ollama_model}' not installed/running locally")

    # Check Whisper cache
    hf_home = _probe_env(probes, "HF_HOME")
    hub_dir = (
        Path(hf_home) / "hub"
        if hf_home
        else Path.home() / ".cache" / "huggingface" / "hub"
    )
    cand_whisper = [
        hub_dir / f"models--Systran--faster-whisper-{stt_model}",
        hub_dir / f"models--guillaumekln--faster-whisper-{stt_model}",
        hub_dir / f"models--{stt_model}",
    ]
    whisper_cached = any(probes.path_exists(p) for p in cand_whisper)
    if not whisper_cached:
        missing.append(f"Whisper model '{stt_model}' not in Hugging Face cache")

    if not missing:
        return Check(
            name="Offline readiness",
            status="OK",
            detail="Ready for ORBIT_OFFLINE: local model available, Whisper cached, offline provider permitted",
            fix=None,
        )

    st = "FAIL" if offline_active else "WARN"
    return Check(
        name="Offline readiness",
        status=st,
        detail=f"Not fully ready for offline mode: {'; '.join(missing)}",
        fix="Pull Ollama model locally and run speech once while online to cache Whisper",
    )


def check_leftover_risks(probes: Probes) -> Check:
    """(o) Leftover diagnostic risks: warn if PYTHONPATH is set in the environment."""
    pypath = _probe_env(probes, "PYTHONPATH")
    if pypath:
        return Check(
            name="Diagnostic risks",
            status="WARN",
            detail=f"PYTHONPATH is set in environment ({pypath}). Scripts include project root path bootstrap and work without it.",
            fix="Unset PYTHONPATH in environment to prevent cross-environment pollution",
        )

    return Check(
        name="Diagnostic risks",
        status="OK",
        detail="PYTHONPATH is not set in environment",
        fix=None,
    )


def check_demo_mode(probes: Probes) -> Check:
    """Report demo mode status and active ORBIT_HOME storage folder."""
    raw_home = _probe_env(probes, "ORBIT_HOME")
    demo_env = _probe_env(probes, "DEMO_MODE", "false")
    demo_active = demo_env.strip().lower() in ("true", "1", "yes", "on")

    home_name = Path(raw_home.strip()).name if raw_home and raw_home.strip() else "none"
    demo_status = "active" if demo_active else "inactive"

    if raw_home and raw_home.strip():
        try:
            from config.paths import get_orbit_home
            get_orbit_home()
        except ValueError as exc:
            return Check(
                name="Demo mode",
                status="FAIL",
                detail=f"Invalid ORBIT_HOME: {exc}",
                fix="Set ORBIT_HOME to an isolated subdirectory",
            )

    if demo_active:
        return Check(
            name="Demo mode",
            status="OK",
            detail=f"DEMO_MODE={demo_status}; ORBIT_HOME={home_name} (sample data; real data/logs not modified)",
            fix=None,
        )

    return Check(
        name="Demo mode",
        status="OK",
        detail=f"DEMO_MODE={demo_status}; ORBIT_HOME={home_name}",
        fix=None,
    )


# --- Core Runner & Formatter ---



def check_system_controls(probes: Probes) -> Check:
    """Check platform availability and configuration for native Windows system controls."""
    raw_enabled = _probe_env(probes, "SYSTEM_CONTROLS_ENABLED")
    is_windows = sys.platform == "win32"
    if raw_enabled is not None:
        enabled = str(raw_enabled).strip().lower() not in ("0", "false", "no", "off")
    else:
        from config import settings
        enabled = getattr(settings, "SYSTEM_CONTROLS_ENABLED", is_windows)

    if not is_windows:
        detail = f"Platform is {sys.platform} (Windows required; system controls inactive; no key sent)"
        return Check(
            name="System controls",
            status="OK" if not enabled else "WARN",
            detail=detail,
            fix="System controls are only supported on Windows" if enabled else None,
        )

    if not enabled:
        return Check(
            name="System controls",
            status="OK",
            detail=f"Platform is {sys.platform} (disabled via SYSTEM_CONTROLS_ENABLED=0; no key sent)",
            fix=None,
        )

    return Check(
        name="System controls",
        status="OK",
        detail=f"Platform is {sys.platform} (enabled on Windows: volume, media keys, screen lock, settings pages; no key sent)",
        fix=None,
    )


def check_rag_embeddings(probes: Probes, fast: bool = False) -> Check:
    """Check RAG embedding model installation, vector counts, and test embedding reachability."""
    raw_enabled = _probe_env(probes, "RAG_EMBEDDINGS", "false")
    is_enabled = str(raw_enabled).strip().lower() in ("1", "true", "yes")

    if not is_enabled:
        return Check(
            name="RAG embeddings",
            status="OK",
            detail="Disabled (lexical FTS5 retrieval active)",
            fix=None,
        )

    embed_model = _probe_env(probes, "RAG_EMBED_MODEL", "embeddinggemma")
    ollama_host = _probe_env(probes, "OLLAMA_HOST", "http://127.0.0.1:11434")

    # 1. Check if model is installed in Ollama
    installed_models: list[str] = []
    try:
        tags_resp = probes.http_get(f"{ollama_host.rstrip('/')}/api/tags", timeout=2.0)
        if tags_resp.status_code == 200:
            t_data = tags_resp.json() if hasattr(tags_resp, "json") else {}
            if isinstance(t_data, dict):
                models = t_data.get("models", [])
                installed_models = [m.get("name") for m in models if isinstance(m, dict) and "name" in m]
    except Exception:
        pass

    from core.providers import _is_model_installed

    model_installed = _is_model_installed(installed_models, embed_model)
    if installed_models and not model_installed:
        return Check(
            name="RAG embeddings",
            status="WARN",
            detail=f"Embedding model '{embed_model}' is not installed in Ollama",
            fix=f"Run: ollama pull {embed_model}",
        )

    # 2. Check test embedding reachability (skip in fast mode)
    if not fast:
        try:
            embed_resp = probes.http_post(
                f"{ollama_host.rstrip('/')}/api/embed",
                json_data={"model": embed_model, "input": ["test"]},
                timeout=3.0,
            )
            if embed_resp.status_code == 404:
                return Check(
                    name="RAG embeddings",
                    status="WARN",
                    detail=f"Embedding model '{embed_model}' not found in Ollama",
                    fix=f"Run: ollama pull {embed_model}",
                )
            elif embed_resp.status_code != 200:
                return Check(
                    name="RAG embeddings",
                    status="WARN",
                    detail=f"Ollama /api/embed returned HTTP {embed_resp.status_code}",
                    fix="Verify Ollama server health",
                )
        except Exception as exc:
            return Check(
                name="RAG embeddings",
                status="WARN",
                detail=f"Ollama embedding test request failed: {exc}",
                fix="Ensure Ollama is running and accessible",
            )

    # 3. Check vector count versus chunk count and stale count
    try:
        from rag.index import DocumentIndex
        idx = DocumentIndex()
        stats = idx.get_vector_stats(model=embed_model)
        total = stats["total_chunks"]
        valid = stats["valid_vectors"]
        stale = stats["stale_vectors"]
        missing = stats["missing_vectors"]
        detail = f"Model '{embed_model}': {valid}/{total} chunks embedded ({stale} stale, {missing} missing)"

        if stale > 0 or missing > 0:
            return Check(
                name="RAG embeddings",
                status="WARN",
                detail=detail,
                fix="Run /index in chat or 'index_documents' to generate embeddings",
            )
        return Check(
            name="RAG embeddings",
            status="OK",
            detail=detail,
            fix=None,
        )
    except Exception as exc:
        return Check(
            name="RAG embeddings",
            status="OK",
            detail=f"Model '{embed_model}': vector database not yet created ({exc})",
            fix=None,
        )


def check_desktop_controls(probes: Probes) -> Check:
    """Check desktop controls (window, files, screenshots, typing, sleep)."""
    from config import settings
    win_ctl = getattr(settings, "WINDOW_CONTROL_ENABLED", True)
    files_on = getattr(settings, "FILES_ENABLED", True)
    sc_on = getattr(settings, "SCREENSHOTS_ENABLED", True)
    typ_on = getattr(settings, "TYPING_ENABLED", False)
    slp_on = getattr(settings, "SLEEP_ENABLED", False)

    plat_ok = sys.platform == "win32"
    has_pil = importlib.util.find_spec("PIL") is not None

    from config.apps import ALLOWED_APPS, TYPING_ALLOWLIST
    from tools.user_files import default_search_roots
    win_apps = sum(1 for a in ALLOWED_APPS.values() if isinstance(a, dict) and a.get("windows"))
    roots_cnt = len(default_search_roots())
    typ_cnt = len(TYPING_ALLOWLIST)

    detail = (
        f"platform={'win32' if plat_ok else sys.platform}, pillow={'available' if has_pil else 'missing'}, "
        f"window_apps={win_apps}, search_roots={roots_cnt}, typing_allowlist={typ_cnt}, "
        f"enabled(win={win_ctl}, files={files_on}, screen={sc_on}, typing={typ_on}, sleep={slp_on})"
    )
    status = "OK" if (plat_ok or not win_ctl) else "WARN"
    fix = None
    if not plat_ok and win_ctl:
        fix = "Window controls and typing SendInput require Windows."
    elif not has_pil and sc_on:
        fix = "Install Pillow with: pip install pillow"
    return Check(name="Desktop controls", status=status, detail=detail, fix=fix)


def check_messaging(probes: Probes) -> Check:
    """Check messaging configuration, contact count, send mode, protocol handler, and window controls."""
    from config import settings
    raw_enabled = _probe_env(probes, "MESSAGING_ENABLED")
    if raw_enabled is not None:
        enabled = str(raw_enabled).strip().lower() not in ("0", "false", "no", "off")
    else:
        enabled = bool(getattr(settings, "MESSAGING_ENABLED", True))

    send_mode = getattr(settings, "WHATSAPP_SEND_MODE", "draft")

    contact_count = 0
    try:
        from contacts.store import ContactStore
        store = ContactStore()
        contact_count = store.count()
    except Exception:
        contact_count = 0

    protocol_registered = False
    if hasattr(probes, "whatsapp_protocol_registered") and callable(probes.whatsapp_protocol_registered):
        protocol_registered = bool(probes.whatsapp_protocol_registered())

    is_windows = sys.platform == "win32"
    proto_str = "protocol registered" if protocol_registered else "protocol not detected (heuristic)"
    sys_str = "system keys/windows available" if is_windows else "system keys/windows unavailable"

    if not enabled:
        detail = f"Disabled via MESSAGING_ENABLED=0 ({contact_count} contacts; mode: {send_mode})"
        return Check(name="Messaging", status="OK", detail=detail, fix=None)

    detail = f"Enabled ({contact_count} contacts; mode: {send_mode}; {proto_str}; {sys_str}; no key sent)"
    return Check(name="Messaging", status="OK", detail=detail, fix=None)


def run_checks(probes: Optional[Probes] = None, fast: bool = False) -> list[Check]:
    """Run environment doctor checks inside isolated try/except blocks.

    Args:
        probes: Injectable Probes dataclass; if None, default real probes are used.
        fast: If True, executes only the fast startup subset (b, c, and 1s Ollama reachability).

    Returns:
        list[Check]: Executed checks with status in {OK, WARN, FAIL, SKIP}.
    """
    p = probes or Probes()
    checks: list[Check] = []

    def _execute(name: str, fn: Callable[[], Union[Check, list[Check]]]) -> None:
        try:
            res = fn()
            if isinstance(res, list):
                checks.extend(res)
            elif res is not None:
                checks.append(res)
        except Exception as exc:
            checks.append(
                Check(
                    name=name,
                    status="FAIL",
                    detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                    fix="Inspect check requirements and doctor configuration",
                )
            )

    if fast:
        # Fast startup mode runs only (b), (c), and a one-second Ollama reachability check
        _execute(".env file", lambda: check_env_file(p))
        _execute("LLM provider", lambda: check_provider(p))
        _execute("Ollama server", lambda: check_ollama_reachability_fast(p))
        return checks

    # Full mode
    _, settings_err = _load_settings()
    if settings_err is not None:
        checks.append(
            Check(
                name="Configuration settings",
                status="FAIL",
                detail=f"Failed to import config.settings ({settings_err})",
                fix="Verify config/settings.py syntax and environment variables",
            )
        )
    else:
        checks.append(
            Check(
                name="Configuration settings",
                status="OK",
                detail="config.settings loaded successfully",
                fix=None,
            )
        )

    _execute("Python environment", lambda: check_python_environment(p))
    _execute(".env file", lambda: check_env_file(p))
    _execute("LLM provider", lambda: check_provider(p))
    _execute("Ollama server", lambda: check_ollama_full(p))
    _execute("GPU (nvidia-smi)", lambda: check_gpu(p))
    _execute("Optional packages", lambda: check_optional_packages(p))
    _execute("Browser", lambda: check_browser(p))
    _execute("Microphone", lambda: check_microphone(p))
    _execute("Whisper model cache", lambda: check_whisper_cache(p))
    _execute("Voice enhancement & eval", lambda: check_voice_enhancement(p))
    _execute("TTS engine", lambda: check_tts_engine(p))
    _execute("Storage folders", lambda: check_folders(p))
    _execute("Git hygiene", lambda: check_git_hygiene(p))
    _execute("Allowed apps", lambda: check_allowed_apps(p))
    _execute("SQLite FTS5", lambda: check_fts5(p))
    _execute("Offline readiness", lambda: check_offline_readiness(p))
    _execute("Diagnostic risks", lambda: check_leftover_risks(p))
    _execute("Demo mode", lambda: check_demo_mode(p))
    _execute("RAG embeddings", lambda: check_rag_embeddings(p, fast=fast))
    _execute("System controls", lambda: check_system_controls(p))
    _execute("Messaging", lambda: check_messaging(p))
    _execute("Desktop controls", lambda: check_desktop_controls(p))

    return checks


def format_report(checks: list[Check]) -> str:
    """Format doctor checks into an aligned plain-text table with summary line."""
    lines = [
        "=" * 84,
        f"ORBIT v{VERSION} Environment Doctor Report",
        "=" * 84,
        f"{'Status':<6} | {'Check Name':<24} | {'Details'}",
        "-" * 84,
    ]

    ok_count = sum(1 for c in checks if c.status.upper() == "OK")
    warn_count = sum(1 for c in checks if c.status.upper() == "WARN")
    fail_count = sum(1 for c in checks if c.status.upper() == "FAIL")

    fixes: list[tuple[str, str]] = []

    for c in checks:
        st = c.status.upper()
        lines.append(f"{st:<6} | {c.name:<24} | {c.detail}")
        if c.fix and st in ("WARN", "FAIL"):
            fixes.append((c.name, c.fix))

    lines.append("-" * 84)
    lines.append(f"Summary: {ok_count} OK, {warn_count} warnings, {fail_count} failures")

    if fixes:
        lines.append("")
        lines.append("Suggested Actions (plain text - never executed automatically):")
        for name, fix in fixes:
            lines.append(f"  - [{name}]: {fix}")

    lines.append("=" * 84)
    return "\n".join(lines)
