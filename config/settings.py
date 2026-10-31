import sys
import os
from dotenv import load_dotenv


from typing import Any, Optional, Union
from config.paths import (
    get_audit_log_path,
    get_browser_profile_dir,
    get_data_dir,
    get_diag_dir,
    get_docs_dir,
    get_logs_dir,
    get_memory_db_path,
    get_rag_db_path,
    get_reminders_db_path,
    get_routines_db_path,
    get_runs_log_path,
    get_workspace_dir,
    voices_dir,
)

# Load environment variables from .env file
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.8-flash")

MAX_TOOL_CALLS: int = int(os.getenv("MAX_TOOL_CALLS", "12"))
MAX_CONSECUTIVE_ERRORS: int = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "3"))
MAX_TASK_SECONDS: float = float(os.getenv("MAX_TASK_SECONDS", "120"))

MEMORY_MAX_ITEMS: int = int(os.getenv("MEMORY_MAX_ITEMS", "500"))
MEMORY_INJECT_LIMIT: int = int(os.getenv("MEMORY_INJECT_LIMIT", "5"))

import logging
import warnings

logger = logging.getLogger(__name__)

ALLOWED_LLM_PROVIDERS = ("auto", "gemini", "ollama", "mock")
ALLOWED_VOICE_REVIEWS = ("auto", "always", "never")
ALLOWED_TTS_ENGINES = ("sapi", "piper")


def _parse_tts_engine(val: Optional[str], default: str = "sapi") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_TTS_ENGINES:
        return v
    msg = f"Unknown TTS_ENGINE '{val}'; falling back to '{default}'."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default


def _parse_piper_voice(val: Optional[str], default: str = "") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip()
    from voice.voices import validate_voice_filename
    is_valid, err_msg = validate_voice_filename(v)
    if is_valid:
        return v
    msg = f"Invalid PIPER_VOICE '{val}' ({err_msg}); falling back to '{default}'."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default


def _parse_float_range(val: Optional[str], default: float, min_val: float, max_val: float) -> float:
    if val is None or not val.strip():
        return default
    try:
        fv = float(val.strip())
        if min_val <= fv <= max_val:
            return fv
    except (ValueError, AttributeError):
        pass
    msg = f"Invalid float value '{val}' (must be between {min_val} and {max_val}); falling back to {default}."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default


def _parse_voice_review(val: Optional[str], default: str = "auto") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_VOICE_REVIEWS:
        return v
    msg = f"Unknown VOICE_REVIEW '{val}'; falling back to '{default}'."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default


def _parse_stt_beam(val: Optional[str], default: int = 1) -> int:
    if val is None or not val.strip():
        return default
    try:
        iv = int(val.strip())
        if 1 <= iv <= 5:
            return iv
    except (ValueError, AttributeError):
        pass
    msg = f"Invalid STT_BEAM '{val}' (must be 1-5); falling back to {default}."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default


def _parse_stt_model(val: Optional[str], default: str = "base.en") -> str:
    ALLOWED_STT_MODELS = ("tiny.en", "base.en", "small.en", "tiny", "base", "small")
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_STT_MODELS:
        return v
    msg = f"Unknown STT_MODEL '{val}'; falling back to '{default}'."
    logger.warning(msg)
    warnings.warn(msg, UserWarning, stacklevel=2)
    return default



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




RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "4"))
RAG_MAX_CHARS: int = int(os.getenv("RAG_MAX_CHARS", "2500"))
RAG_INCLUDE_FLAGGED: bool = os.getenv("RAG_INCLUDE_FLAGGED", "false").lower() in ("true", "1", "yes")

ALLOWED_RAG_AUTO_SCORING = ("fraction", "embed", "hybrid", "rarest3", "idf")

RAG_EMBEDDINGS: bool = _parse_bool(os.getenv("RAG_EMBEDDINGS"), False)
RAG_EMBED_MODEL: str = _parse_str(os.getenv("RAG_EMBED_MODEL"), "embeddinggemma")
RAG_EMBED_CPU_ONLY: bool = _parse_bool(os.getenv("RAG_EMBED_CPU_ONLY"), True)
RAG_EMBED_BATCH: int = _parse_int(os.getenv("RAG_EMBED_BATCH"), 16)
RAG_EMBED_THRESHOLD: float = _parse_float(os.getenv("RAG_EMBED_THRESHOLD"), 0.65)
RAG_EMBED_MARGIN: float = _parse_float(os.getenv("RAG_EMBED_MARGIN"), 0.08)
RAG_EMBED_TOOL: bool = _parse_bool(os.getenv("RAG_EMBED_TOOL"), False)


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


STT_MODEL: str = _parse_stt_model(os.getenv("STT_MODEL"), "base.en")
STT_LANGUAGE: str = _parse_str(os.getenv("STT_LANGUAGE"), "en")
MIC_DEVICE: Optional[Union[int, str]] = _parse_mic_device(os.getenv("MIC_DEVICE"))
MAX_RECORD_SECONDS: int = _parse_int(os.getenv("MAX_RECORD_SECONDS"), 30)
TTS_ENGINE: str = _parse_tts_engine(os.getenv("TTS_ENGINE"), "sapi")
PIPER_VOICE: str = _parse_piper_voice(os.getenv("PIPER_VOICE"), "")
TTS_SPEED: float = _parse_float_range(os.getenv("TTS_SPEED"), 1.0, 0.8, 1.3)
TTS_VOLUME: float = _parse_float_range(os.getenv("TTS_VOLUME"), 1.0, 0.3, 1.0)
TTS_RATE: int = _parse_int(os.getenv("TTS_RATE"), int(180 * TTS_SPEED)) if os.getenv("TTS_RATE") else int(180 * TTS_SPEED)
SPEAK_MAX_CHARS: int = _parse_int(os.getenv("SPEAK_MAX_CHARS"), 500)
SPEAK_REPLIES: bool = _parse_bool(os.getenv("SPEAK_REPLIES"), False)
VOICE_IN_UI: bool = _parse_bool(os.getenv("VOICE_IN_UI"), True)
VOICE_HIGHPASS: int = _parse_int(os.getenv("VOICE_HIGHPASS"), 0)
VOICE_AGC: int = _parse_int(os.getenv("VOICE_AGC"), 0)
VOICE_DENOISE: int = _parse_int(os.getenv("VOICE_DENOISE"), 0)
STT_BEAM: int = _parse_stt_beam(os.getenv("STT_BEAM"), 1)
STT_PROMPT: int = _parse_int(os.getenv("STT_PROMPT"), 0)
VOICE_ALIASES: int = _parse_int(os.getenv("VOICE_ALIASES"), 0)
VOICE_REVIEW: str = _parse_voice_review(os.getenv("VOICE_REVIEW"), "auto")

WAKE_PHRASE: str = os.getenv("WAKE_PHRASE", "hey orbit")
WAKE_SENSITIVITY: float = _parse_float(os.getenv("WAKE_SENSITIVITY"), 0.80)
WAKE_STT_MODEL: str = os.getenv("WAKE_STT_MODEL", "tiny.en")
VAD_THRESHOLD: float = _parse_float(os.getenv("VAD_THRESHOLD"), 0.01)
SILENCE_SECONDS: float = _parse_float(os.getenv("SILENCE_SECONDS"), 0.8)
WAKE_COOLDOWN_SECONDS: float = _parse_float(os.getenv("WAKE_COOLDOWN_SECONDS"), 3.0)
WAKE_LISTEN_TIMEOUT: float = _parse_float(os.getenv("WAKE_LISTEN_TIMEOUT"), 6.0)

HANDS_FREE: int = _parse_int(os.getenv("HANDS_FREE"), 0)
if os.getenv("ORBIT_EVAL") == "1" or os.getenv("DEMO_MODE") == "1":
    HANDS_FREE = 0
HANDS_FREE_IDLE_MINUTES: int = _parse_int(os.getenv("HANDS_FREE_IDLE_MINUTES"), 10)
HANDS_FREE_FOLLOWUP_SECONDS: int = _parse_int(os.getenv("HANDS_FREE_FOLLOWUP_SECONDS"), 0)
HANDS_FREE_CUES: int = _parse_int(os.getenv("HANDS_FREE_CUES"), 1)
STREAM_REPLIES: bool = _parse_bool(os.getenv("STREAM_REPLIES"), True)
COMMAND_SHORTCUTS: bool = _parse_bool(os.getenv("COMMAND_SHORTCUTS"), True)
CHAINS_ENABLED: bool = _parse_bool(os.getenv("CHAINS_ENABLED"), True)
SESSION_CONTEXT: bool = _parse_bool(os.getenv("SESSION_CONTEXT"), True)
SESSION_TTL_SECONDS: float = _parse_float(os.getenv("SESSION_TTL_SECONDS"), 1800.0)
REMINDERS_ENABLED: bool = _parse_bool(os.getenv("REMINDERS_ENABLED"), True)
SYSTEM_CONTROLS_ENABLED: bool = _parse_bool(os.getenv("SYSTEM_CONTROLS_ENABLED"), sys.platform == "win32")
WINDOW_CONTROL_ENABLED: bool = _parse_bool(os.getenv("WINDOW_CONTROL_ENABLED"), True)
FILES_ENABLED: bool = _parse_bool(os.getenv("FILES_ENABLED"), True)
SCREENSHOTS_ENABLED: bool = _parse_bool(os.getenv("SCREENSHOTS_ENABLED"), True)
TYPING_ENABLED: bool = _parse_bool(os.getenv("TYPING_ENABLED"), False)
SLEEP_ENABLED: bool = _parse_bool(os.getenv("SLEEP_ENABLED"), False)
MESSAGING_ENABLED: bool = _parse_bool(os.getenv("MESSAGING_ENABLED"), True)
_raw_send_mode = os.getenv("WHATSAPP_SEND_MODE", "draft").strip().lower()
WHATSAPP_SEND_MODE: str = _raw_send_mode if _raw_send_mode in ("draft", "send") else "draft"
WHATSAPP_PROCESS_NAMES: tuple[str, ...] = ("WhatsApp.exe", "WhatsApp.Root.exe")

TOOL_ROUTING: bool = _parse_bool(os.getenv("TOOL_ROUTING"), False)
DOCTOR_ON_START: bool = _parse_bool(os.getenv("DOCTOR_ON_START"), True)


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


def reminders_enabled() -> bool:
    """Read the environment at call time to determine whether reminders are enabled."""
    return _parse_bool(os.getenv("REMINDERS_ENABLED"), True)


def system_controls_enabled() -> bool:
    """Read the environment at call time to determine whether Windows system controls are enabled."""
    return _parse_bool(os.getenv("SYSTEM_CONTROLS_ENABLED"), sys.platform == "win32")


def tool_routing_enabled() -> bool:
    """Read the environment at call time to determine whether tool routing is enabled."""
    return _parse_bool(os.getenv("TOOL_ROUTING"), False)


def _parse_plan_preview(val: Optional[str], default: str = "auto") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ("auto", "always", "never"):
        return v
    return default


PLAN_PREVIEW: str = _parse_plan_preview(os.getenv("PLAN_PREVIEW"), "auto")
PLAN_MAX_STEPS: int = _parse_int(os.getenv("PLAN_MAX_STEPS"), 10)
PLAN_MAX_SECONDS: float = _parse_float(os.getenv("PLAN_MAX_SECONDS"), 180.0)


ROUTINES_ENABLED: bool = _parse_bool(os.getenv("ROUTINES_ENABLED"), True)



def _parse_routine_preview(val: Optional[str], default: str = "always") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ("always", "never"):
        return v
    return default


ROUTINE_PREVIEW: str = _parse_routine_preview(os.getenv("ROUTINE_PREVIEW"), "always")


def routines_enabled() -> bool:
    """Read the environment at call time to determine whether routines are enabled."""
    return _parse_bool(os.getenv("ROUTINES_ENABLED"), True)


ALLOWED_UI_STYLE = ("hud", "classic")
ALLOWED_UI_THEME = ("cyan", "gold", "mono")
ALLOWED_UI_MOTION = ("full", "reduced", "off")
ALLOWED_HUD_FPS = (15, 24, 30)
ALLOWED_UI_REALISM = ("full", "lite", "off")


def _parse_ui_realism(val: Optional[str], default: str = "full") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_UI_REALISM:
        return v
    return default


def _parse_ui_style(val: Optional[str], default: str = "hud") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_UI_STYLE:
        return v
    return default


def _parse_ui_theme(val: Optional[str], default: str = "cyan") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_UI_THEME:
        return v
    return default


def _parse_ui_motion(val: Optional[str], default: str = "full") -> str:
    if val is None or not val.strip():
        return default
    v = val.strip().lower()
    if v in ALLOWED_UI_MOTION:
        return v
    return default


def _parse_ui_interactive(val: Any, default: int = 1) -> int:
    if val is None:
        return default
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("0", "false", "no", "off"):
            return 0
        if v in ("1", "true", "yes", "on"):
            return 1
        return default
    try:
        return 1 if int(val) else 0
    except (ValueError, TypeError):
        return default


def _parse_hud_fps(val: Any, default: int = 30) -> int:
    if val is None:
        return default
    try:
        v = int(val)
        if v in ALLOWED_HUD_FPS:
            return v
    except (ValueError, TypeError):
        pass
    return default


UI_STYLE: str = _parse_ui_style(os.getenv("UI_STYLE"), "hud")
UI_THEME: str = _parse_ui_theme(os.getenv("UI_THEME"), "cyan")
UI_MOTION: str = _parse_ui_motion(os.getenv("UI_MOTION"), "full")
HUD_FPS: int = _parse_hud_fps(os.getenv("HUD_FPS"), 30)
UI_REALISM: str = _parse_ui_realism(os.getenv("UI_REALISM"), "full")
UI_INTERACTIVE: int = _parse_ui_interactive(os.getenv("UI_INTERACTIVE"), 1)
INSIGHTS_ENABLED: int = _parse_ui_interactive(os.getenv("INSIGHTS_ENABLED"), 1)
SYSTEM_TAB_GPU: int = _parse_ui_interactive(os.getenv("SYSTEM_TAB_GPU"), 1)
UI_STATUS_CHIPS: int = _parse_ui_interactive(os.getenv("UI_STATUS_CHIPS"), 1)


def __getattr__(name: str) -> Any:
    if name == "BROWSER_ENABLED":
        return browser_enabled()
    if name == "CHAINS_ENABLED":
        return _parse_bool(os.getenv("CHAINS_ENABLED"), True)
    if name == "PLAN_PREVIEW":
        return _parse_plan_preview(os.getenv("PLAN_PREVIEW"), "auto")
    if name == "PLAN_MAX_STEPS":
        return _parse_int(os.getenv("PLAN_MAX_STEPS"), 10)
    if name == "PLAN_MAX_SECONDS":
        return _parse_float(os.getenv("PLAN_MAX_SECONDS"), 180.0)
    if name == "SPEAK_REPLIES":
        return _parse_bool(os.getenv("SPEAK_REPLIES"), False)
    if name == "VOICE_IN_UI":
        return _parse_bool(os.getenv("VOICE_IN_UI"), True)
    if name == "STT_MODEL":
        return _parse_stt_model(os.getenv("STT_MODEL"), "base.en")
    if name == "VOICE_HIGHPASS":
        return _parse_int(os.getenv("VOICE_HIGHPASS"), 0)
    if name == "VOICE_AGC":
        return _parse_int(os.getenv("VOICE_AGC"), 0)
    if name == "VOICE_DENOISE":
        return _parse_int(os.getenv("VOICE_DENOISE"), 0)
    if name == "STT_BEAM":
        return _parse_stt_beam(os.getenv("STT_BEAM"), 1)
    if name == "STT_PROMPT":
        return _parse_int(os.getenv("STT_PROMPT"), 0)
    if name == "VOICE_ALIASES":
        return _parse_int(os.getenv("VOICE_ALIASES"), 0)
    if name == "VOICE_REVIEW":
        return _parse_voice_review(os.getenv("VOICE_REVIEW"), "auto")
    if name == "STT_LANGUAGE":
        return _parse_str(os.getenv("STT_LANGUAGE"), "en")
    if name == "MIC_DEVICE":
        return _parse_mic_device(os.getenv("MIC_DEVICE"))
    if name == "MAX_RECORD_SECONDS":
        return _parse_int(os.getenv("MAX_RECORD_SECONDS"), 30)
    if name == "REMINDERS_ENABLED":
        return _parse_bool(os.getenv("REMINDERS_ENABLED"), True)
    if name == "REMINDERS_DB_PATH":
        return get_reminders_db_path()
    if name == "ROUTINES_ENABLED":
        return routines_enabled()
    if name == "ROUTINES_DB_PATH":
        return get_routines_db_path()
    if name == "ROUTINE_PREVIEW":
        return _parse_routine_preview(os.getenv("ROUTINE_PREVIEW"), "always")
    if name == "TOOL_ROUTING":
        return tool_routing_enabled()
    if name == "DOCTOR_ON_START":
        return _parse_bool(os.getenv("DOCTOR_ON_START"), True)
    if name == "UI_STYLE":
        return _parse_ui_style(os.getenv("UI_STYLE"), "hud")
    if name == "UI_THEME":
        return _parse_ui_theme(os.getenv("UI_THEME"), "cyan")
    if name == "UI_MOTION":
        return _parse_ui_motion(os.getenv("UI_MOTION"), "full")
    if name == "HUD_FPS":
        return _parse_hud_fps(os.getenv("HUD_FPS"), 30)
    if name == "UI_REALISM":
        return _parse_ui_realism(os.getenv("UI_REALISM"), "full")
    if name == "UI_INTERACTIVE":
        return _parse_ui_interactive(os.getenv("UI_INTERACTIVE"), 1)
    if name == "INSIGHTS_ENABLED":
        return _parse_ui_interactive(os.getenv("INSIGHTS_ENABLED"), 1)
    if name == "SYSTEM_TAB_GPU":
        return _parse_ui_interactive(os.getenv("SYSTEM_TAB_GPU"), 1)
    if name == "UI_STATUS_CHIPS":
        return _parse_ui_interactive(os.getenv("UI_STATUS_CHIPS"), 1)
    if name == "RAG_EMBEDDINGS":
        return _parse_bool(os.getenv("RAG_EMBEDDINGS"), False)
    if name == "RAG_EMBED_MODEL":
        return _parse_str(os.getenv("RAG_EMBED_MODEL"), "embeddinggemma")
    if name == "RAG_EMBED_CPU_ONLY":
        return _parse_bool(os.getenv("RAG_EMBED_CPU_ONLY"), True)
    if name == "RAG_EMBED_BATCH":
        return _parse_int(os.getenv("RAG_EMBED_BATCH"), 16)
    if name == "RAG_AUTO_SCORING":
        return _parse_rag_auto_scoring(os.getenv("RAG_AUTO_SCORING"), "fraction")
    if name == "RAG_EMBED_THRESHOLD":
        return _parse_float(os.getenv("RAG_EMBED_THRESHOLD"), 0.65)
    if name == "RAG_EMBED_MARGIN":
        return _parse_float(os.getenv("RAG_EMBED_MARGIN"), 0.08)
    if name == "RAG_EMBED_TOOL":
        return _parse_bool(os.getenv("RAG_EMBED_TOOL"), False)
    if name == "WORKSPACE_DIR":
        return get_workspace_dir()
    if name == "DOCS_DIR":
        return get_docs_dir()
    if name == "MEMORY_DB_PATH":
        return get_memory_db_path()
    if name == "RAG_DB_PATH":
        return get_rag_db_path()
    if name == "BROWSER_PROFILE_DIR":
        return str(get_browser_profile_dir())
    if name == "AUDIT_LOG_PATH":
        return get_audit_log_path()
    if name == "RUNS_LOG_PATH":
        return get_runs_log_path()
    if name == "DIAG_DIR":
        return get_diag_dir()
    if name == "LOGS_DIR":
        return get_logs_dir()
    if name == "DATA_DIR":
        return get_data_dir()
    if name == "VOICES_DIR":
        return voices_dir()
    if name == "TTS_ENGINE":
        return _parse_tts_engine(os.getenv("TTS_ENGINE"), "sapi")
    if name == "PIPER_VOICE":
        return _parse_piper_voice(os.getenv("PIPER_VOICE"), "")
    if name == "TTS_SPEED":
        return _parse_float_range(os.getenv("TTS_SPEED"), 1.0, 0.8, 1.3)
    if name == "TTS_VOLUME":
        return _parse_float_range(os.getenv("TTS_VOLUME"), 1.0, 0.3, 1.0)
    if name == "SYSTEM_CONTROLS_ENABLED":
        return _parse_bool(os.getenv("SYSTEM_CONTROLS_ENABLED"), sys.platform == "win32")
    if name == "WINDOW_CONTROL_ENABLED":
        return _parse_bool(os.getenv("WINDOW_CONTROL_ENABLED"), True)
    if name == "FILES_ENABLED":
        return _parse_bool(os.getenv("FILES_ENABLED"), True)
    if name == "SCREENSHOTS_ENABLED":
        return _parse_bool(os.getenv("SCREENSHOTS_ENABLED"), True)
    if name == "TYPING_ENABLED":
        return _parse_bool(os.getenv("TYPING_ENABLED"), False)
    if name == "SLEEP_ENABLED":
        return _parse_bool(os.getenv("SLEEP_ENABLED"), False)
    if name == "MESSAGING_ENABLED":
        return _parse_bool(os.getenv("MESSAGING_ENABLED"), True)
    if name == "WHATSAPP_SEND_MODE":
        mode = os.getenv("WHATSAPP_SEND_MODE", "draft").strip().lower()
        return mode if mode in ("draft", "send") else "draft"
    if name == "WHATSAPP_PROCESS_NAMES":
        return ("WhatsApp.exe", "WhatsApp.Root.exe")
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__() -> list[str]:
    return sorted(
        set(globals().keys())
        | {
            "WORKSPACE_DIR",
            "DOCS_DIR",
            "MEMORY_DB_PATH",
            "RAG_DB_PATH",
            "REMINDERS_DB_PATH",
            "ROUTINES_DB_PATH",
            "BROWSER_PROFILE_DIR",
            "AUDIT_LOG_PATH",
            "RUNS_LOG_PATH",
            "DIAG_DIR",
            "LOGS_DIR",
            "DATA_DIR",
            "BROWSER_ENABLED",
            "CHAINS_ENABLED",
            "PLAN_PREVIEW",
            "PLAN_MAX_STEPS",
            "PLAN_MAX_SECONDS",
            "SPEAK_REPLIES",
            "VOICE_IN_UI",
            "VOICE_HIGHPASS",
            "VOICE_AGC",
            "VOICE_DENOISE",
            "STT_BEAM",
            "STT_PROMPT",
            "VOICE_ALIASES",
            "VOICE_REVIEW",
            "STT_MODEL",
            "STT_LANGUAGE",
            "MIC_DEVICE",
            "MAX_RECORD_SECONDS",
            "REMINDERS_ENABLED",
            "ROUTINES_ENABLED",
            "ROUTINE_PREVIEW",
            "TOOL_ROUTING",
            "DOCTOR_ON_START",
            "UI_STYLE",
            "UI_THEME",
            "UI_MOTION",
            "HUD_FPS",
        "UI_REALISM",
        "UI_INTERACTIVE",
            "INSIGHTS_ENABLED",
            "SYSTEM_TAB_GPU",
            "UI_STATUS_CHIPS",
            "VOICES_DIR",
            "TTS_ENGINE",
            "PIPER_VOICE",
            "TTS_SPEED",
            "TTS_VOLUME",
            "SYSTEM_CONTROLS_ENABLED",
            "WINDOW_CONTROL_ENABLED",
            "FILES_ENABLED",
            "SCREENSHOTS_ENABLED",
            "TYPING_ENABLED",
            "SLEEP_ENABLED",
        }
    )


BROWSER_CHANNEL: str = _parse_str(os.getenv("BROWSER_CHANNEL"), "chrome")

BROWSER_COMMAND_TIMEOUT_S: int = _parse_int(os.getenv("BROWSER_COMMAND_TIMEOUT_S"), 20)
BROWSER_ALLOW_SIGNIN: bool = _parse_bool(os.getenv("BROWSER_ALLOW_SIGNIN"), False)
BROWSER_CHROMIUM_SANDBOX: bool = _parse_bool(os.getenv("BROWSER_CHROMIUM_SANDBOX"), True)
BROWSER_AUTO_RESTART: bool = _parse_bool(os.getenv("BROWSER_AUTO_RESTART"), True)
BROWSER_BLOCK_COOLDOWN_MINUTES: int = _parse_int(os.getenv("BROWSER_BLOCK_COOLDOWN_MINUTES"), 30)
GOOGLE_SEARCH_IN_CONTROLLED_BROWSER: bool = _parse_bool(os.getenv("GOOGLE_SEARCH_IN_CONTROLLED_BROWSER"), False)



def require_api_key() -> str:
    """Return GEMINI_API_KEY if configured, or raise a friendly ValueError."""
    key = os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY
    if not key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Please set it in your environment or define it in a .env file."
        )
    return key


