"""Tests for core/providers.py provider factory, precedence rules, banner, and status."""

from unittest.mock import patch

import pytest

from config import settings
from core.llm import GeminiProvider, MockProvider
from core.ollama_provider import ModelNotFound, OllamaProvider, OllamaUnavailable
from core.providers import ProviderChoice, make_provider
from main import format_startup_banner, format_status, get_status_info


def test_orbit_mock_wins(monkeypatch):
    """Verify ORBIT_MOCK=1 unconditionally returns MockProvider with reason in notice."""
    monkeypatch.setenv("ORBIT_MOCK", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "real_key_123")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")

    choice = make_provider()
    assert isinstance(choice, ProviderChoice)
    assert isinstance(choice.provider, MockProvider)
    assert choice.label == "MOCK"
    assert choice.notice is not None
    assert "ORBIT_MOCK" in choice.notice


def test_auto_with_key_gives_gemini(monkeypatch):
    """Verify auto mode with GEMINI_API_KEY set gives GeminiProvider."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("GEMINI_API_KEY", "test_gemini_key")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test_gemini_key")
    monkeypatch.setattr(settings, "MODEL_NAME", "gemini-3.8-flash")

    with patch("core.llm.genai.Client"):
        choice = make_provider()
        assert isinstance(choice.provider, GeminiProvider)
        assert choice.label == "LIVE gemini-3.8-flash"
        assert choice.notice is None


def test_auto_without_key_reachable_ollama_picks_ollama(monkeypatch):
    """Verify auto mode without key and with a reachable Ollama server picks ollama."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3:8b")

    with patch("core.providers.check_ollama_alive", return_value=True):
        with patch("core.providers.list_models", return_value=["qwen3:8b", "llama3:latest"]):
            choice = make_provider()
            assert isinstance(choice.provider, OllamaProvider)
            assert choice.label == "LOCAL qwen3:8b"
            assert choice.notice is None


def test_auto_without_either_gives_mock_with_notice_and_reason(monkeypatch):
    """Verify auto mode without key and unreachable Ollama returns MockProvider with detailed reason."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")

    with patch("core.providers.check_ollama_alive", return_value=False):
        choice = make_provider()
        assert isinstance(choice.provider, MockProvider)
        assert choice.label == "MOCK"
        assert choice.notice is not None
        assert "GEMINI_API_KEY" in choice.notice
        assert "http://127.0.0.1:11434" in choice.notice
        assert "not reachable" in choice.notice


def test_auto_ollama_reachable_but_model_missing_gives_mock_with_reason(monkeypatch):
    """Verify auto mode with Ollama reachable but configured model missing gives MockProvider with notice."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")

    with patch("core.providers.check_ollama_alive", return_value=True):
        with patch("core.providers.list_models", return_value=["mistral:latest"]):
            choice = make_provider()
            assert isinstance(choice.provider, MockProvider)
            assert choice.label == "MOCK"
            assert choice.notice is not None
            assert "qwen3:8b" in choice.notice
            assert "not installed" in choice.notice


def test_explicit_ollama_unreachable_raises_error(monkeypatch):
    """Verify explicit ollama with unreachable server raises OllamaUnavailable and never falls back to mock."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")

    with patch("core.providers.check_ollama_alive", return_value=False):
        with pytest.raises(OllamaUnavailable) as exc_info:
            make_provider()
        assert "Ollama is not running at http://127.0.0.1:11434" in str(exc_info.value)


def test_explicit_ollama_model_not_installed_raises_error(monkeypatch):
    """Verify explicit ollama with missing model raises ModelNotFound."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3:8b")

    with patch("core.providers.check_ollama_alive", return_value=True):
        with patch("core.providers.list_models", return_value=["other-model:latest"]):
            with pytest.raises(ModelNotFound) as exc_info:
                make_provider()
            assert "Model 'qwen3:8b' is not installed" in str(exc_info.value)
            assert "ollama pull qwen3:8b" in str(exc_info.value)


def test_explicit_gemini_missing_key_raises_error(monkeypatch):
    """Verify explicit gemini without API key raises ValueError and never falls back to mock."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")

    with pytest.raises(ValueError) as exc_info:
        make_provider()
    assert "GEMINI_API_KEY is not set" in str(exc_info.value)


def test_offline_explicit_gemini_raises(monkeypatch):
    """Verify ORBIT_OFFLINE=1 with explicit gemini request raises ValueError."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("ORBIT_OFFLINE", "1")
    monkeypatch.setattr(settings, "ORBIT_OFFLINE", True)

    with pytest.raises(ValueError) as exc_info:
        make_provider(name="gemini")
    assert "ORBIT_OFFLINE is on: 'gemini' provider is not permitted" in str(exc_info.value)


def test_offline_auto_picks_ollama_when_available(monkeypatch):
    """Verify ORBIT_OFFLINE=1 with auto provider resolves to ollama when running."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("ORBIT_OFFLINE", "1")
    monkeypatch.setattr(settings, "ORBIT_OFFLINE", True)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3:8b")

    with patch("core.providers.check_ollama_alive", return_value=True):
        with patch("core.providers.list_models", return_value=["qwen3:8b"]):
            choice = make_provider()
            assert isinstance(choice.provider, OllamaProvider)
            assert choice.label == "LOCAL qwen3:8b"


def test_offline_auto_falls_back_to_mock_when_unreachable(monkeypatch):
    """Verify ORBIT_OFFLINE=1 with auto provider falls back to mock when Ollama is unreachable."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("ORBIT_OFFLINE", "1")
    monkeypatch.setattr(settings, "ORBIT_OFFLINE", True)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")

    with patch("core.providers.check_ollama_alive", return_value=False):
        choice = make_provider()
        assert isinstance(choice.provider, MockProvider)
        assert choice.label == "MOCK"
        assert "ORBIT_OFFLINE is on and Ollama is not reachable" in choice.notice


def test_unknown_llm_provider_falls_back_to_auto(monkeypatch):
    """Verify unknown LLM_PROVIDER reports a warning and falls back to auto resolution."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "unknown_provider_xyz")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)

    with patch("core.providers.check_ollama_alive", return_value=False):
        with pytest.warns(UserWarning, match="Unknown LLM_PROVIDER"):
            choice = make_provider()
        assert isinstance(choice.provider, MockProvider)
        assert choice.label == "MOCK"


def test_ollama_provider_constructed(monkeypatch):
    """Verify selecting ollama with running server returns an OllamaProvider instance."""
    monkeypatch.delenv("ORBIT_MOCK", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3:8b")

    with patch("core.providers.check_ollama_alive", return_value=True):
        with patch("core.providers.list_models", return_value=["qwen3:8b"]):
            choice = make_provider(name="ollama")
            assert isinstance(choice.provider, OllamaProvider)
            assert choice.label == "LOCAL qwen3:8b"


def test_provider_choice_tuple_unpacking():
    """Verify ProviderChoice supports 3-element tuple unpacking."""
    choice = ProviderChoice(provider="prov", label="lbl", notice="ntc")
    p, l, n = choice
    assert p == "prov"
    assert l == "lbl"
    assert n == "ntc"


def test_banner_mock_formatting():
    """Verify format_startup_banner produces expected single line and loud MOCK MODE warning."""
    banner = format_startup_banner(
        label="MOCK",
        tool_count=5,
        memory_count=2,
        notice="No GEMINI_API_KEY set and Ollama is not reachable at http://127.0.0.1:11434.",
    )
    lines = banner.splitlines()
    assert lines[0] == "ORBIT | provider: MOCK | tools: 5 | memory: 2 items"
    assert "MOCK MODE" in banner
    assert "*** MOCK MODE: replies are NOT from a real model." in lines[1]
    assert "No GEMINI_API_KEY" in lines[1]


def test_banner_non_mock_formatting():
    """Verify format_startup_banner for real providers contains no MOCK MODE warning."""
    banner = format_startup_banner(
        label="LOCAL qwen3:8b",
        tool_count=8,
        memory_count=0,
        notice=None,
    )
    assert banner == "ORBIT | provider: LOCAL qwen3:8b | tools: 8 | memory: 0 items"
    assert "MOCK MODE" not in banner

    live_banner = format_startup_banner(
        label="LIVE gemini-3.8-flash",
        tool_count=12,
        memory_count=4,
        notice="Temporary rate limit warning",
    )
    assert "ORBIT | provider: LIVE gemini-3.8-flash | tools: 12 | memory: 4 items" in live_banner
    assert "Temporary rate limit warning" in live_banner
    assert "MOCK MODE" not in live_banner


def test_status_info_and_formatting(monkeypatch):
    """Verify get_status_info and format_status output."""
    monkeypatch.setattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setattr(settings, "ORBIT_OFFLINE", False)

    with patch("core.ollama_provider.server_version", return_value="0.5.4"):
        with patch("core.ollama_provider.list_models", return_value=["qwen3:8b"]):
            info = get_status_info("LOCAL qwen3:8b")
            assert info["provider_label"] == "LOCAL qwen3:8b"
            assert info["model"] == "qwen3:8b"
            assert info["server_reachable"] is True
            assert info["server_version"] == "0.5.4"
            assert info["model_installed"] is True

            formatted = format_status(info)
            assert "Provider label: LOCAL qwen3:8b" in formatted
            assert "Active model: qwen3:8b" in formatted
            assert "Ollama host: http://127.0.0.1:11434" in formatted
            assert "ORBIT_OFFLINE: OFF" in formatted
            assert "Ollama server: Reachable (v0.5.4)" in formatted
            assert "Ollama model 'qwen3:8b': Installed / reachable" in formatted
