import os
from dotenv import load_dotenv

from pathlib import Path
from typing import Any, Optional, Union

# Load environment variables from .env file
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.8-flash")
WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", "workspace")).resolve()
MAX_TOOL_CALLS: int = int(os.getenv("MAX_TOOL_CALLS", "12"))
MAX_CONSECUTIVE_ERRORS: int = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "3"))
MAX_TASK_SECONDS: float = float(os.getenv("MAX_TASK_SECONDS", "120"))
MEMORY_DB_PATH: Path = Path(os.getenv("MEMORY_DB_PATH", "data/memory.db")).resolve()
MEMORY_MAX_ITEMS: int = int(os.getenv("MEMORY_MAX_ITEMS", "500"))
MEMORY_INJECT_LIMIT: int = int(os.getenv("MEMORY_INJECT_LIMIT", "5"))

import logging
import warnings

logger = logging.getLogger(__name__)

ALLOWED_LLM_PROVIDERS = ("auto", "gemini", "ollama", "mock")


def _parse_llm_provider(val: Optional[str], default: str = "auto") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_LLM_PROVIDERS:
        return v
    msg = f"Unknown LLM_PROVIDER '{val}'; falling back to '{default}'."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default


def _parse_optional_int(val: Optional[str], default: Optional[int] = None) -> Optional[int]:
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except (ValueError, AttributeError):
        return default


def _parse_str(val: Optional[str], default: str) -> str:
    if val is None or not val.strip():
        return default
    return val.strip()


def _parse_int(val: Optional[str], default: int) -> int:
    if val is None:
        return default
    try:
        return int(val.strip())
    except (ValueError, AttributeError):
        return default


def _parse_mic_device(val: Optional[str]) -> Optional[Union[int, str]]:
    if val is None or not val.strip():
        return None
    val_clean = val.strip()
    try:
        return int(val_clean)
    except ValueError:
        return val_clean


def _parse_float(val: Optional[str], default: float) -> float:
    if val is None:
        return default
    try:
        return float(val.strip())
    except (ValueError, AttributeError):
        return default


def _parse_bool(val: Optional[str], default: bool) -> bool:
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return default


LLM_PROVIDER: str = _parse_llm_provider(os.getenv("LLM_PROVIDER"), "auto")
OLLAMA_HOST: str = _parse_str(os.getenv("OLLAMA_HOST"), "http://127.0.0.1:11434")
OLLAMA_MODEL: str = _parse_str(os.getenv("OLLAMA_MODEL"), "qwen3:8b")
OLLAMA_NUM_CTX: int = _parse_int(os.getenv("OLLAMA_NUM_CTX"), 8192)
OLLAMA_TEMPERATURE: float = _parse_float(os.getenv("OLLAMA_TEMPERATURE"), 0.2)
OLLAMA_SEED: Optional[int] = _parse_optional_int(os.getenv("OLLAMA_SEED"), None)
OLLAMA_THINK: bool = _parse_bool(os.getenv("OLLAMA_THINK"), False)
OLLAMA_KEEP_ALIVE: str = _parse_str(os.getenv("OLLAMA_KEEP_ALIVE"), "30m")
OLLAMA_TIMEOUT_S: int = _parse_int(os.getenv("OLLAMA_TIMEOUT_S"), 180)
OLLAMA_ALLOW_REMOTE: bool = _parse_bool(os.getenv("OLLAMA_ALLOW_REMOTE"), False)
OLLAMA_INLINE_TOOLS: bool = _parse_bool(os.getenv("OLLAMA_INLINE_TOOLS"), False)
ORBIT_OFFLINE: bool = _parse_bool(os.getenv("ORBIT_OFFLINE"), False)


DOCS_DIR: Path = Path(os.getenv("DOCS_DIR", str(WORKSPACE_DIR / "docs"))).resolve()
RAG_DB_PATH: Path = Path(os.getenv("RAG_DB_PATH", "data/rag.db")).resolve()
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "4"))
RAG_MAX_CHARS: int = int(os.getenv("RAG_MAX_CHARS", "2500"))
RAG_INCLUDE_FLAGGED: bool = os.getenv("RAG_INCLUDE_FLAGGED", "false").lower() in ("true", "1", "yes")

ALLOWED_RAG_AUTO_SCORING = ("fraction", "rarest3", "idf")


def _parse_rag_auto_scoring(val: Optional[str], default: str = "fraction") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_RAG_AUTO_SCORING:
        return v
    msg = f"Unknown RAG_AUTO_SCORING '{val}'; falling back to '{default}'."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default


RAG_AUTO: bool = _parse_bool(os.getenv("RAG_AUTO"), True)
RAG_AUTO_MIN_TERMS: int = _parse_int(os.getenv("RAG_AUTO_MIN_TERMS"), 2)
RAG_AUTO_MIN_COVERAGE: float = _parse_float(os.getenv("RAG_AUTO_MIN_COVERAGE"), 0.5)
RAG_AUTO_TOP_K: int = _parse_int(os.getenv("RAG_AUTO_TOP_K"), 3)
RAG_AUTO_MAX_CHARS: int = _parse_int(os.getenv("RAG_AUTO_MAX_CHARS"), 1500)
RAG_AUTO_SCORING: str = _parse_rag_auto_scoring(os.getenv("RAG_AUTO_SCORING"), "fraction")


STT_MODEL: str = os.getenv("STT_MODEL", "base.en")
STT_LANGUAGE: str = os.getenv("STT_LANGUAGE", "en")
MIC_DEVICE: Optional[Union[int, str]] = _parse_mic_device(os.getenv("MIC_DEVICE"))
MAX_RECORD_SECONDS: int = _parse_int(os.getenv("MAX_RECORD_SECONDS"), 30)
TTS_RATE: int = _parse_int(os.getenv("TTS_RATE"), 180)
SPEAK_MAX_CHARS: int = _parse_int(os.getenv("SPEAK_MAX_CHARS"), 500)

WAKE_PHRASE: str = os.getenv("WAKE_PHRASE", "hey orbit")
WAKE_STT_MODEL: str = os.getenv("WAKE_STT_MODEL", "tiny.en")
VAD_THRESHOLD: float = _parse_float(os.getenv("VAD_THRESHOLD"), 0.01)
SILENCE_SECONDS: float = _parse_float(os.getenv("SILENCE_SECONDS"), 0.8)
WAKE_COOLDOWN_SECONDS: float = _parse_float(os.getenv("WAKE_COOLDOWN_SECONDS"), 3.0)
WAKE_LISTEN_TIMEOUT: float = _parse_float(os.getenv("WAKE_LISTEN_TIMEOUT"), 6.0)
STREAM_REPLIES: bool = _parse_bool(os.getenv("STREAM_REPLIES"), True)
COMMAND_SHORTCUTS: bool = _parse_bool(os.getenv("COMMAND_SHORTCUTS"), True)
SESSION_CONTEXT: bool = _parse_bool(os.getenv("SESSION_CONTEXT"), True)
SESSION_TTL_SECONDS: float = _parse_float(os.getenv("SESSION_TTL_SECONDS"), 1800.0)


def _parse_browser_enabled(val: Optional[str] = None) -> bool:
    if val is not None and val.strip():
        v = val.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


def browser_enabled() -> bool:
    """Read the environment at call time to determine whether browser automation is enabled."""
    return _parse_browser_enabled(os.getenv("BROWSER_ENABLED"))


def __getattr__(name: str) -> Any:
    if name == "BROWSER_ENABLED":
        return browser_enabled()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


BROWSER_CHANNEL: str = _parse_str(os.getenv("BROWSER_CHANNEL"), "chrome")
BROWSER_PROFILE_DIR: str = _parse_str(os.getenv("BROWSER_PROFILE_DIR"), "data/browser_profile")
BROWSER_COMMAND_TIMEOUT_S: int = _parse_int(os.getenv("BROWSER_COMMAND_TIMEOUT_S"), 20)
BROWSER_ALLOW_SIGNIN: bool = _parse_bool(os.getenv("BROWSER_ALLOW_SIGNIN"), False)
BROWSER_CHROMIUM_SANDBOX: bool = _parse_bool(os.getenv("BROWSER_CHROMIUM_SANDBOX"), True)


def require_api_key() -> str:
    """Return GEMINI_API_KEY if configured, or raise a friendly ValueError."""
    key = os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY
    if not key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Please set it in your environment or define it in a .env file."
        )
    return key


