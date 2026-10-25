"""Provider factory and selection logic for ORBIT."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Optional

from core.llm import GeminiProvider, MockProvider
from core.ollama_provider import (
    ModelNotFound,
    OllamaProvider,
    OllamaUnavailable,
    list_models,
)


@dataclass
class ProviderChoice:
    """Selected LLM provider along with UI label and optional informational notice."""

    provider: Any
    label: str
    notice: Optional[str] = None

    def __iter__(self):
        yield self.provider
        yield self.label
        yield self.notice


def check_ollama_alive(host: str = "http://127.0.0.1:11434", timeout: float = 1.0) -> bool:
    """Return True if the Ollama server responds successfully to GET /api/version within timeout."""
    import urllib.request

    url = f"{host.rstrip('/')}/api/version"
    req = urllib.request.Request(url, headers={"User-Agent": "ORBIT"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def is_orbit_offline() -> bool:
    """Return True if ORBIT_OFFLINE is active via environment or settings."""
    env_val = os.getenv("ORBIT_OFFLINE")
    if env_val is not None:
        from config.settings import _parse_bool

        return _parse_bool(env_val, False)
    from config import settings

    return bool(getattr(settings, "ORBIT_OFFLINE", False))


def _is_model_installed(installed_models: Optional[list[str]], target_model: str) -> bool:
    """Check if target_model exists in the installed models list."""
    if installed_models is None:
        return False
    target = target_model.strip().lower()
    target_base = target.split(":")[0] if ":" in target else target
    for m in installed_models:
        m_lower = m.lower()
        if m_lower == target:
            return True
        if ":" not in target and m_lower == f"{target}:latest":
            return True
        if target.endswith(":latest") and m_lower == target_base:
            return True
    return False


def _resolve_requested_provider(name: Optional[str] = None) -> tuple[str, bool]:
    """Return (normalized_provider_name, is_explicit)."""
    from config import settings
    from config.settings import _parse_llm_provider

    if name is not None:
        parsed = _parse_llm_provider(name)
        return parsed, parsed != "auto"

    env_prov = os.getenv("LLM_PROVIDER")
    if env_prov is not None:
        parsed = _parse_llm_provider(env_prov)
        return parsed, parsed != "auto"

    val = getattr(settings, "LLM_PROVIDER", "auto")
    val_str = val if isinstance(val, str) else str(val)
    parsed = _parse_llm_provider(val_str)
    return parsed, parsed != "auto"


def make_provider(name: Optional[str] = None) -> ProviderChoice:
    """Select and initialize an LLM provider based on configuration and environment.

    Precedence:
    1. ORBIT_MOCK=1 unconditionally returns MockProvider labelled 'MOCK' with notice.
    2. If ORBIT_OFFLINE is on, only 'ollama' or 'mock' are allowed:
       - An explicit 'gemini' request raises a ValueError.
       - 'auto' attempts local ollama, falling back to mock if unreachable/missing.
    3. Explicit LLM_PROVIDER ('ollama', 'gemini', 'mock'):
       - 'gemini': requires GEMINI_API_KEY, raises ValueError if missing. Never falls back to mock.
       - 'ollama': verifies reachability and model installation. Raises OllamaUnavailable or ModelNotFound. Never falls back to mock.
       - 'mock': returns MockProvider labelled 'MOCK'.
    4. 'auto' resolves to:
       - gemini if online and GEMINI_API_KEY is set.
       - otherwise ollama if server is alive and model is installed.
       - otherwise mock with an informative notice stating why (no Gemini key, server unreachable, model not installed).
    """
    from config import settings

    # 1. ORBIT_MOCK=1 wins unconditionally
    if os.getenv("ORBIT_MOCK") == "1":
        notice = "ORBIT_MOCK is set in environment. Set GEMINI_API_KEY or configure Ollama to use a real model."
        return ProviderChoice(provider=MockProvider(), label="MOCK", notice=notice)

    offline = is_orbit_offline()
    requested, is_explicit = _resolve_requested_provider(name)

    # 2. Enforce ORBIT_OFFLINE constraints
    if offline:
        if requested == "gemini":
            raise ValueError(
                "ORBIT_OFFLINE is on: 'gemini' provider is not permitted in offline mode. "
                "Only 'ollama' or 'mock' are allowed."
            )

        if requested not in ("ollama", "mock", "auto"):
            raise ValueError(
                f"ORBIT_OFFLINE is on: '{requested}' provider is not permitted in offline mode. "
                "Only 'ollama' or 'mock' are allowed."
            )

    # 3. Explicit choice resolution (never falls back to mock when explicitly set to ollama or gemini)
    if requested == "mock":
        return ProviderChoice(
            provider=MockProvider(),
            label="MOCK",
            notice="LLM_PROVIDER is set to 'mock'.",
        )

    if requested == "gemini" and is_explicit:
        gemini_key = os.getenv("GEMINI_API_KEY") or getattr(settings, "GEMINI_API_KEY", None)
        if not gemini_key or not gemini_key.strip():
            raise ValueError(
                "GEMINI_API_KEY is not set. Set GEMINI_API_KEY in your .env file or environment, "
                "or use LLM_PROVIDER=ollama for local offline models."
            )
        model_name = getattr(settings, "MODEL_NAME", "gemini-3.8-flash")
        return ProviderChoice(
            provider=GeminiProvider(model_name=model_name),
            label=f"LIVE {model_name}",
            notice=None,
        )

    if requested == "ollama" and is_explicit:
        ollama_host = getattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")
        model_name = getattr(settings, "OLLAMA_MODEL", "qwen3:8b")

        if not check_ollama_alive(ollama_host, timeout=1.0):
            raise OllamaUnavailable(
                f"Ollama is not running at {ollama_host}. Start it with the Ollama app or run: ollama serve"
            )

        installed = list_models(ollama_host)
        if installed is not None and not _is_model_installed(installed, model_name):
            raise ModelNotFound(
                f"Model '{model_name}' is not installed in Ollama. Run: ollama pull {model_name}"
            )

        return ProviderChoice(
            provider=OllamaProvider(model=model_name, host=ollama_host),
            label=f"LOCAL {model_name}",
            notice=None,
        )

    # 4. "auto" resolution (or offline auto)
    ollama_host = getattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model = getattr(settings, "OLLAMA_MODEL", "qwen3:8b")

    if not offline:
        gemini_key = os.getenv("GEMINI_API_KEY") or getattr(settings, "GEMINI_API_KEY", None)
        if gemini_key and gemini_key.strip():
            model_name = getattr(settings, "MODEL_NAME", "gemini-3.8-flash")
            return ProviderChoice(
                provider=GeminiProvider(model_name=model_name),
                label=f"LIVE {model_name}",
                notice=None,
            )

    # Gemini not used/available. Check Ollama.
    if check_ollama_alive(ollama_host, timeout=1.0):
        installed = list_models(ollama_host)
        if installed is not None and not _is_model_installed(installed, ollama_model):
            prefix = "ORBIT_OFFLINE is on and" if offline else "No GEMINI_API_KEY set and"
            notice = f"{prefix} model '{ollama_model}' is not installed in Ollama at {ollama_host}. Run: ollama pull {ollama_model}"
            return ProviderChoice(
                provider=MockProvider(),
                label="MOCK",
                notice=notice,
            )
        return ProviderChoice(
            provider=OllamaProvider(model=ollama_model, host=ollama_host),
            label=f"LOCAL {ollama_model}",
            notice=None,
        )

    # Ollama not reachable either
    if offline:
        notice = f"ORBIT_OFFLINE is on and Ollama is not reachable at {ollama_host}. Start Ollama or run: ollama serve"
    else:
        notice = (
            f"No GEMINI_API_KEY set and Ollama is not reachable at {ollama_host}. "
            "To use Gemini, set GEMINI_API_KEY in .env. To use Ollama, start the Ollama server."
        )

    return ProviderChoice(
        provider=MockProvider(),
        label="MOCK",
        notice=notice,
    )
