"""Safe configuration settings store for ORBIT.

Provides a strict whitelist of editable, non-secret settings, line-by-line .env
parsing with line ending / BOM preservation, atomic saving with .env.bak backup,
and effective value resolution. No tkinter dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Optional, Union

from config import settings


class SettingsError(Exception):
    """Raised when setting validation, parsing, or file saving fails."""
    pass


@dataclass(frozen=True)
class SettingSpec:
    """Specification for an editable, non-secret setting."""

    key: str
    type: str  # "enum", "text", "int", "bool", "float"
    allowed: Optional[tuple[str, ...]] = None
    min_val: Optional[int] = None
    max_val: Optional[int] = None
    min_float: Optional[float] = None
    max_float: Optional[float] = None
    default: str = ""
    description: str = ""
    restart_required: bool = False


# Whitelist of editable, non-secret settings.
# Defaults are read directly from config.settings, never hard-coded independently.
SPECS: dict[str, SettingSpec] = {
    "LLM_PROVIDER": SettingSpec(
        key="LLM_PROVIDER",
        type="enum",
        allowed=("auto", "ollama", "gemini", "mock"),
        default=getattr(settings, "LLM_PROVIDER", "auto"),
        description="Active LLM provider backend",
        restart_required=True,
    ),
    "OLLAMA_MODEL": SettingSpec(
        key="OLLAMA_MODEL",
        type="text",
        default=str(getattr(settings, "OLLAMA_MODEL", "qwen3:8b")),
        description="Local Ollama model tag",
        restart_required=True,
    ),
    "OLLAMA_NUM_CTX": SettingSpec(
        key="OLLAMA_NUM_CTX",
        type="int",
        min_val=2048,
        max_val=32768,
        default=str(getattr(settings, "OLLAMA_NUM_CTX", 8192)),
        description="Ollama context window token size",
        restart_required=True,
    ),
    "BROWSER_ENABLED": SettingSpec(
        key="BROWSER_ENABLED",
        type="enum",
        allowed=("auto", "0", "1"),
        default="auto",
        description="Browser automation control",
        restart_required=True,
    ),
    "SPEAK_REPLIES": SettingSpec(
        key="SPEAK_REPLIES",
        type="bool",
        default="1" if getattr(settings, "SPEAK_REPLIES", False) else "0",
        description="Speak agent responses aloud via TTS",
        restart_required=False,
    ),
    "VOICE_IN_UI": SettingSpec(
        key="VOICE_IN_UI",
        type="bool",
        default="1" if getattr(settings, "VOICE_IN_UI", True) else "0",
        description="Show microphone button and voice controls in UI",
        restart_required=True,
    ),
    "COMMAND_SHORTCUTS": SettingSpec(
        key="COMMAND_SHORTCUTS",
        type="bool",
        default="1" if getattr(settings, "COMMAND_SHORTCUTS", True) else "0",
        description="Fast-path execution for single commands",
        restart_required=False,
    ),
    "CHAINS_ENABLED": SettingSpec(
        key="CHAINS_ENABLED",
        type="bool",
        default="1" if getattr(settings, "CHAINS_ENABLED", True) else "0",
        description="Multi-step chained command routines",
        restart_required=False,
    ),
    "PLAN_PREVIEW": SettingSpec(
        key="PLAN_PREVIEW",
        type="enum",
        allowed=("auto", "always", "never"),
        default=getattr(settings, "PLAN_PREVIEW", "auto"),
        description="Multi-step plan preview confirmation policy",
        restart_required=False,
    ),
    "TOOL_ROUTING": SettingSpec(
        key="TOOL_ROUTING",
        type="bool",
        default="1" if getattr(settings, "TOOL_ROUTING", False) else "0",
        description="Rule-based fast tool router",
        restart_required=False,
    ),
    "REMINDERS_ENABLED": SettingSpec(
        key="REMINDERS_ENABLED",
        type="bool",
        default="1" if getattr(settings, "REMINDERS_ENABLED", True) else "0",
        description="Background reminder and timer scheduler",
        restart_required=True,
    ),
    "SYSTEM_CONTROLS_ENABLED": SettingSpec(
        key="SYSTEM_CONTROLS_ENABLED",
        type="bool",
        default="1" if getattr(settings, "SYSTEM_CONTROLS_ENABLED", sys.platform == "win32") else "0",
        description="Enable native Windows system controls for volume, media keys, screen lock, and settings",
        restart_required=True,
    ),
    "MESSAGING_ENABLED": SettingSpec(
        key="MESSAGING_ENABLED",
        type="bool",
        default="1",
        description="Enable local messaging shortcuts and tools",
        restart_required=False,
    ),
    "WHATSAPP_SEND_MODE": SettingSpec(
        key="WHATSAPP_SEND_MODE",
        type="enum",
        allowed=("draft", "send"),
        default="draft",
        description="WhatsApp execution mode (draft or send)",
        restart_required=False,
    ),
    "ROUTINES_ENABLED": SettingSpec(
        key="ROUTINES_ENABLED",
        type="bool",
        default="1" if getattr(settings, "ROUTINES_ENABLED", True) else "0",
        description="Named persistent action routines",
        restart_required=False,
    ),
    "ROUTINE_PREVIEW": SettingSpec(
        key="ROUTINE_PREVIEW",
        type="enum",
        allowed=("always", "never"),
        default=getattr(settings, "ROUTINE_PREVIEW", "always"),
        description="Routine preview confirmation policy",
        restart_required=False,
    ),
    "SESSION_CONTEXT": SettingSpec(
        key="SESSION_CONTEXT",
        type="bool",
        default="1" if getattr(settings, "SESSION_CONTEXT", True) else "0",
        description="Retain active context across turns",
        restart_required=False,
    ),
    "RAG_AUTO": SettingSpec(
        key="RAG_AUTO",
        type="bool",
        default="1" if getattr(settings, "RAG_AUTO", True) else "0",
        description="Automatic retrieval augmentation from workspace docs",
        restart_required=False,
    ),
    "RAG_EMBEDDINGS": SettingSpec(
        key="RAG_EMBEDDINGS",
        type="bool",
        default="1" if getattr(settings, "RAG_EMBEDDINGS", False) else "0",
        description="Enable embedding-based semantic document retrieval",
        restart_required=True,
    ),
    "RAG_AUTO_SCORING": SettingSpec(
        key="RAG_AUTO_SCORING",
        type="enum",
        allowed=("fraction", "embed", "hybrid"),
        default=getattr(settings, "RAG_AUTO_SCORING", "fraction"),
        description="Automatic retrieval candidate scoring method (fraction, embed, hybrid)",
        restart_required=True,
    ),
    "RAG_EMBED_MODEL": SettingSpec(
        key="RAG_EMBED_MODEL",
        type="text",
        default=str(getattr(settings, "RAG_EMBED_MODEL", "embeddinggemma")),
        description="Local Ollama embedding model name",
        restart_required=True,
    ),
    "RAG_EMBED_THRESHOLD": SettingSpec(
        key="RAG_EMBED_THRESHOLD",
        type="float",
        min_float=0.40,
        max_float=0.90,
        default=str(getattr(settings, "RAG_EMBED_THRESHOLD", 0.65)),
        description="Minimum cosine similarity threshold for embedding retrieval (0.40-0.90)",
        restart_required=True,
    ),
    "RAG_EMBED_MARGIN": SettingSpec(
        key="RAG_EMBED_MARGIN",
        type="float",
        min_float=0.00,
        max_float=0.50,
        default=str(getattr(settings, "RAG_EMBED_MARGIN", 0.08)),
        description="Score margin below top hit for embedding candidate retention (0.00-0.50)",
        restart_required=True,
    ),
    "RAG_EMBED_CPU_ONLY": SettingSpec(
        key="RAG_EMBED_CPU_ONLY",
        type="bool",
        default="1" if getattr(settings, "RAG_EMBED_CPU_ONLY", True) else "0",
        description="Run Ollama embeddings on CPU only with num_gpu=0",
        restart_required=True,
    ),
    "RAG_EMBED_BATCH": SettingSpec(
        key="RAG_EMBED_BATCH",
        type="int",
        min_val=1,
        max_val=128,
        default=str(getattr(settings, "RAG_EMBED_BATCH", 16)),
        description="Batch size for generating chunk embeddings (1-128)",
        restart_required=True,
    ),
    "UI_STYLE": SettingSpec(
        key="UI_STYLE",
        type="enum",
        allowed=("hud", "classic"),
        default=str(getattr(settings, "UI_STYLE", "hud")),
        description="User interface layout style (hud: sci-fi HUD dashboard, classic: standard panels)",
        restart_required=True,
    ),
    "UI_THEME": SettingSpec(
        key="UI_THEME",
        type="enum",
        allowed=("cyan", "gold", "mono"),
        default=str(getattr(settings, "UI_THEME", "cyan")),
        description="Color palette theme (cyan, gold, mono)",
        restart_required=True,
    ),
    "UI_MOTION": SettingSpec(
        key="UI_MOTION",
        type="enum",
        allowed=("full", "reduced", "off"),
        default=str(getattr(settings, "UI_MOTION", "full")),
        description="Animation motion level (full: 30 FPS with particles, reduced: low-rate pulse, off: static)",
        restart_required=True,
    ),
    "HUD_FPS": SettingSpec(
        key="HUD_FPS",
        type="enum",
        allowed=("15", "24", "30"),
        default=str(getattr(settings, "HUD_FPS", 30)),
        description="Target framerate for HUD animations (15, 24, 30)",
        restart_required=True,
    ),
    "UI_INTERACTIVE": SettingSpec(
        key="UI_INTERACTIVE",
        type="bool",
        default="1" if getattr(settings, "UI_INTERACTIVE", 1) else "0",
        description="Enable interactive HUD pointer and keyboard actions",
        restart_required=False,
    ),
    "UI_REALISM": SettingSpec(
        key="UI_REALISM",
        type="enum",
        allowed=("full", "lite", "off"),
        default=str(getattr(settings, "UI_REALISM", "full")),
        description="Cinematic realism rendering mode (full: background bitmap + bloom sprites + glass body + parallax, lite: background bitmap only, off: pure vector)",
        restart_required=True,
    ),
    "DOCTOR_ON_START": SettingSpec(
        key="DOCTOR_ON_START",
        type="bool",
        default="1" if getattr(settings, "DOCTOR_ON_START", True) else "0",
        description="Run environment doctor checks on startup",
        restart_required=True,
    ),
    "VOICE_HIGHPASS": SettingSpec(
        key="VOICE_HIGHPASS",
        type="bool",
        default="1" if getattr(settings, "VOICE_HIGHPASS", 0) else "0",
        description="High-pass filter below 80 Hz to eliminate low-frequency rumble",
        restart_required=False,
    ),
    "VOICE_AGC": SettingSpec(
        key="VOICE_AGC",
        type="bool",
        default="1" if getattr(settings, "VOICE_AGC", 0) else "0",
        description="Automatic gain control to level voice volume and prevent clipping",
        restart_required=False,
    ),
    "VOICE_DENOISE": SettingSpec(
        key="VOICE_DENOISE",
        type="bool",
        default="1" if getattr(settings, "VOICE_DENOISE", 0) else "0",
        description="Spectral gate noise reduction for ambient room noise",
        restart_required=False,
    ),
    "STT_BEAM": SettingSpec(
        key="STT_BEAM",
        type="int",
        min_val=1,
        max_val=5,
        default=str(getattr(settings, "STT_BEAM", 1)),
        description="Whisper beam search decoding size (1-5)",
        restart_required=False,
    ),
    "STT_PROMPT": SettingSpec(
        key="STT_PROMPT",
        type="bool",
        default="1" if getattr(settings, "STT_PROMPT", 0) else "0",
        description="Guide Whisper decoding with fixed app/site command vocabulary",
        restart_required=False,
    ),
    "VOICE_ALIASES": SettingSpec(
        key="VOICE_ALIASES",
        type="bool",
        default="1" if getattr(settings, "VOICE_ALIASES", 0) else "0",
        description="Deterministic split-name alias resolution for app and site names",
        restart_required=False,
    ),
    "VOICE_REVIEW": SettingSpec(
        key="VOICE_REVIEW",
        type="enum",
        allowed=("auto", "always", "never"),
        default=str(getattr(settings, "VOICE_REVIEW", "auto")),
        description="Voice review gate (auto: confidence/noise check, always: review every phrase, never: instant dispatch)",
        restart_required=False,
    ),
    "STT_MODEL": SettingSpec(
        key="STT_MODEL",
        type="enum",
        allowed=("tiny.en", "base.en", "small.en", "tiny", "base", "small"),
        default=str(getattr(settings, "STT_MODEL", "base.en")),
        description="Whisper speech-to-text model name",
        restart_required=True,
    ),
    "STT_LANGUAGE": SettingSpec(
        key="STT_LANGUAGE",
        type="enum",
        allowed=("en", "hi", "empty"),
        default=str(getattr(settings, "STT_LANGUAGE", "en")),
        description="Spoken language code for speech recognition",
        restart_required=True,
    ),
    "TTS_ENGINE": SettingSpec(
        key="TTS_ENGINE",
        type="enum",
        allowed=("sapi", "piper"),
        default=str(getattr(settings, "TTS_ENGINE", "sapi")),
        description="Text-to-speech engine backend (sapi: Windows SAPI5, piper: local neural ONNX)",
        restart_required=True,
    ),
    "PIPER_VOICE": SettingSpec(
        key="PIPER_VOICE",
        type="text",
        default=str(getattr(settings, "PIPER_VOICE", "")),
        description="Voice model file name in data/voices (e.g. en_US-lessac-medium.onnx)",
        restart_required=True,
    ),
    "TTS_SPEED": SettingSpec(
        key="TTS_SPEED",
        type="float",
        min_float=0.8,
        max_float=1.3,
        default=str(getattr(settings, "TTS_SPEED", 1.0)),
        description="Speech synthesis playback speed factor (0.8 - 1.3)",
        restart_required=False,
    ),
    "TTS_VOLUME": SettingSpec(
        key="TTS_VOLUME",
        type="float",
        min_float=0.3,
        max_float=1.0,
        default=str(getattr(settings, "TTS_VOLUME", 1.0)),
        description="Speech synthesis volume level (0.3 - 1.0)",
        restart_required=False,
    ),
    "ORBIT_MOCK": SettingSpec(
        key="ORBIT_MOCK",
        type="bool",
        default="0",
        description="Mock mode (no real model)",
        restart_required=True,
    ),
    "WINDOW_CONTROL_ENABLED": SettingSpec(
        key="WINDOW_CONTROL_ENABLED",
        type="bool",
        default="1" if getattr(settings, "WINDOW_CONTROL_ENABLED", True) else "0",
        description="Enable window focus, minimize, maximize, restore, and snap controls",
        restart_required=False,
    ),
    "FILES_ENABLED": SettingSpec(
        key="FILES_ENABLED",
        type="bool",
        default="1" if getattr(settings, "FILES_ENABLED", True) else "0",
        description="Enable safe file finding, opening, and folder revealing",
        restart_required=False,
    ),
    "SCREENSHOTS_ENABLED": SettingSpec(
        key="SCREENSHOTS_ENABLED",
        type="bool",
        default="1" if getattr(settings, "SCREENSHOTS_ENABLED", True) else "0",
        description="Enable desktop screen capture to workspace/screenshots",
        restart_required=False,
    ),
    "TYPING_ENABLED": SettingSpec(
        key="TYPING_ENABLED",
        type="bool",
        default="1" if getattr(settings, "TYPING_ENABLED", False) else "0",
        description="Enable guarded keyboard typing into allowlisted applications",
        restart_required=False,
    ),
    "SLEEP_ENABLED": SettingSpec(
        key="SLEEP_ENABLED",
        type="bool",
        default="1" if getattr(settings, "SLEEP_ENABLED", False) else "0",
        description="Enable putting computer into sleep mode safely",
        restart_required=True,
    ),
}


def is_secret_like(name: str) -> bool:
    """Return True if setting name contains secret-like keywords."""
    upper = name.upper()
    return any(kw in upper for kw in ("KEY", "TOKEN", "SECRET", "PASSWORD"))


def validate(key: str, raw: Any) -> str:
    """Validate and normalize a raw setting value according to SPECS.

    Returns the normalized string representation.
    Raises SettingsError with a friendly message on any validation failure.
    """
    if is_secret_like(key) or key not in SPECS:
        raise SettingsError("That setting can't be changed here.")

    spec = SPECS[key]
    val = "" if raw is None else str(raw)

    # General constraints
    if len(val) > 100:
        raise SettingsError(f"Value for '{key}' exceeds maximum length of 100 characters.")

    # Reject newlines, carriage returns, NUL, and other control characters
    for c in val:
        code = ord(c)
        if c in "\r\n\0" or code < 32 or code == 127:
            raise SettingsError(f"Value for '{key}' contains invalid control characters or newlines.")

    # Reject leading '=' or '#'
    if val.startswith("=") or val.startswith("#"):
        raise SettingsError(f"Value for '{key}' cannot start with '=' or '#'.")

    # Reject surrounding quotes
    stripped = val.strip()
    if (stripped.startswith('"') and stripped.endswith('"') and len(stripped) >= 2) or (
        stripped.startswith("'") and stripped.endswith("'") and len(stripped) >= 2
    ):
        raise SettingsError(f"Value for '{key}' must not have surrounding quotes.")

    # Type-specific validation
    if spec.type == "bool":
        v_lower = stripped.lower()
        if v_lower in ("true", "1", "yes", "on"):
            return "1"
        if v_lower in ("false", "0", "no", "off"):
            return "0"
        raise SettingsError(
            f"Invalid boolean value '{val}' for '{key}'. Allowed values: 1/0, true/false, yes/no, on/off."
        )

    if spec.type == "enum":
        v_lower = stripped.lower()
        if key == "STT_LANGUAGE":
            if v_lower in ("", "empty"):
                return ""
            if v_lower in ("en", "hi"):
                return v_lower
            raise SettingsError(
                f"Invalid value '{val}' for 'STT_LANGUAGE'. Allowed options: en, hi, empty."
            )
        allowed = spec.allowed or ()
        allowed_map = {opt.lower(): opt for opt in allowed}
        if v_lower in allowed_map:
            return allowed_map[v_lower]
        raise SettingsError(
            f"Invalid value '{val}' for '{key}'. Allowed options: {', '.join(allowed)}."
        )

    if spec.type == "int":
        try:
            num = int(stripped)
        except ValueError:
            raise SettingsError(f"Value for '{key}' must be an integer, got '{val}'.")
        if spec.min_val is not None and num < spec.min_val:
            raise SettingsError(f"Value for '{key}' must be at least {spec.min_val}.")
        if spec.max_val is not None and num > spec.max_val:
            raise SettingsError(f"Value for '{key}' must be at most {spec.max_val}.")
        return str(num)

    if spec.type == "float":
        try:
            f_num = float(stripped)
        except ValueError:
            raise SettingsError(f"Value for '{key}' must be a number, got '{val}'.")
        if spec.min_float is not None and f_num < spec.min_float:
            raise SettingsError(f"Value for '{key}' must be at least {spec.min_float:.2f}.")
        if spec.max_float is not None and f_num > spec.max_float:
            raise SettingsError(f"Value for '{key}' must be at most {spec.max_float:.2f}.")
        norm = f"{f_num:.2f}"
        if norm.endswith(".00"):
            return f"{f_num:.1f}"
        if norm.endswith("0") and not norm.endswith(".0"):
            return norm.rstrip("0")
        return norm

    if spec.type == "text":
        if key == "PIPER_VOICE":
            if not stripped:
                return ""
            from voice.voices import validate_voice_filename
            is_ok, err_msg = validate_voice_filename(stripped)
            if not is_ok:
                raise SettingsError(f"Invalid value for '{key}': {err_msg}")
            return stripped
        if key in ("OLLAMA_MODEL", "RAG_EMBED_MODEL"):
            if not stripped:
                raise SettingsError(f"{key} cannot be empty.")
            if len(stripped) > 60:
                raise SettingsError(f"{key} must be at most 60 characters.")
            if not re.match(r"^[A-Za-z0-9._:/-]+$", stripped):
                raise SettingsError(
                    f"{key} '{stripped}' contains invalid characters. Must match [A-Za-z0-9._:/-]+."
                )
            return stripped
        return stripped

    return stripped


@dataclass
class EffectiveSetting:
    """Resolved setting value, origin source, and override state."""

    key: str
    value: str
    source: str  # "default", ".env", "environment"
    overridden_by_env: bool
    spec: SettingSpec


def _parse_env_file_lines(content: str) -> dict[str, str]:
    """Parse raw .env content into key-value pairs without side effects."""
    env_dict: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k_clean = k.strip()
            v_clean = v.strip()
            if (v_clean.startswith('"') and v_clean.endswith('"')) or (
                v_clean.startswith("'") and v_clean.endswith("'")
            ):
                v_clean = v_clean[1:-1]
            env_dict[k_clean] = v_clean
    return env_dict


def read_effective(
    env_path: Optional[Union[str, Path]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, EffectiveSetting]:
    """Read effective values for all whitelisted settings.

    Resolution rule (mirroring config.settings and python-dotenv override=False):
    Existing process environment variables take precedence over .env file entries.
    If a variable is present in the process environment and differs from the .env entry,
    source is 'environment' and overridden_by_env is True.
    """
    path = Path(env_path) if env_path is not None else Path(".env")
    env_vars = os.environ if environ is None else environ

    env_file_dict: dict[str, str] = {}
    if path.is_file():
        try:
            raw_bytes = path.read_bytes()
            if raw_bytes.startswith(b"\xef\xbb\xbf"):
                raw_bytes = raw_bytes[3:]
            text = raw_bytes.decode("utf-8", errors="replace")
            env_file_dict = _parse_env_file_lines(text)
        except Exception:
            pass

    results: dict[str, EffectiveSetting] = {}

    for key, spec in SPECS.items():
        raw_env_file = env_file_dict.get(key)
        raw_proc_env = env_vars.get(key)

        # Normalize env_file value if present
        norm_env_file: Optional[str] = None
        if raw_env_file is not None:
            try:
                norm_env_file = validate(key, raw_env_file)
            except SettingsError:
                norm_env_file = raw_env_file.strip()

        # Normalize proc_env value if present
        norm_proc_env: Optional[str] = None
        if raw_proc_env is not None:
            try:
                norm_proc_env = validate(key, raw_proc_env)
            except SettingsError:
                norm_proc_env = raw_proc_env.strip()

        # Resolution hierarchy: environment > .env > default
        if norm_proc_env is not None:
            if norm_env_file is not None:
                if norm_proc_env != norm_env_file:
                    results[key] = EffectiveSetting(
                        key=key,
                        value=norm_proc_env,
                        source="environment",
                        overridden_by_env=True,
                        spec=spec,
                    )
                else:
                    results[key] = EffectiveSetting(
                        key=key,
                        value=norm_proc_env,
                        source=".env",
                        overridden_by_env=False,
                        spec=spec,
                    )
            else:
                results[key] = EffectiveSetting(
                    key=key,
                    value=norm_proc_env,
                    source="environment",
                    overridden_by_env=False,
                    spec=spec,
                )
        elif norm_env_file is not None:
            results[key] = EffectiveSetting(
                key=key,
                value=norm_env_file,
                source=".env",
                overridden_by_env=False,
                spec=spec,
            )
        else:
            results[key] = EffectiveSetting(
                key=key,
                value=spec.default,
                source="default",
                overridden_by_env=False,
                spec=spec,
            )

    return results


@dataclass
class SaveResult:
    """Summary of saved settings modifications."""

    changed: dict[str, tuple[str, str]]  # key -> (old_value, new_value)
    warnings: list[str]


def save(
    env_path: Union[str, Path],
    changes: dict[str, Any],
) -> SaveResult:
    """Save setting changes to the target .env file.

    Features:
    - In-place replacement of whitelisted keys preserving order and structure.
    - Appends missing keys under '# set by ORBIT settings'.
    - Preserves UTF-8 BOM and line endings (CRLF vs LF).
    - Preserves comments, unknown keys, and secrets untouched.
    - Refuses to write if duplicate keys or merged lines exist for changed keys.
    - Creates '.env.bak' before writing.
    - Atomic file replacement via os.replace from a temp file in the same directory.
    - Leaves original byte-identical and cleans temp file on any failure.
    - ORBIT_MOCK=1 writes line; ORBIT_MOCK=0 removes line with warning.
    """
    from config.paths import is_demo_mode

    if is_demo_mode():
        raise SettingsError("Settings can't be changed in demo mode")

    path = Path(env_path)

    # 1. Validate all requested changes upfront
    validated_changes: dict[str, str] = {}
    for k, v in changes.items():
        if is_secret_like(k) or k not in SPECS:
            raise SettingsError(f"Cannot change setting '{k}': not in editable whitelist.")
        validated_changes[k] = validate(k, v)

    # 2. Read existing content as bytes
    raw_bytes = b""
    has_bom = False
    newline_str = "\r\n" if sys.platform == "win32" else "\n"

    if path.is_file():
        raw_bytes = path.read_bytes()
        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        body_bytes = raw_bytes[3:] if has_bom else raw_bytes
        # Detect CRLF vs LF
        if b"\r\n" in body_bytes:
            newline_str = "\r\n"
        elif b"\n" in body_bytes:
            newline_str = "\n"
        text = body_bytes.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=False)
    else:
        lines = []

    # 3. Detect duplicate keys and merged lines for keys being modified
    key_line_numbers: dict[str, list[int]] = {}
    merged_line_numbers: dict[str, list[int]] = {}

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in line:
            k_part, v_part = line.split("=", 1)
            k_clean = k_part.strip()
            key_line_numbers.setdefault(k_clean, []).append(idx)

            v_clean = v_part.strip()
            unquoted = v_clean
            if (v_clean.startswith('"') and v_clean.endswith('"')) or (
                v_clean.startswith("'") and v_clean.endswith("'")
            ):
                unquoted = v_clean[1:-1]
            if re.search(r"^[A-Z][A-Z0-9_]*=", v_clean) or re.search(r"^[A-Z][A-Z0-9_]*=", unquoted):
                merged_line_numbers.setdefault(k_clean, []).append(idx)

    for k in validated_changes:
        dups = key_line_numbers.get(k, [])
        if len(dups) > 1:
            line_str = ", ".join(map(str, dups))
            raise SettingsError(f"Refusing to write: key '{k}' appears multiple times (lines {line_str}).")
        merged = merged_line_numbers.get(k, [])
        if merged:
            line_str = ", ".join(map(str, merged))
            raise SettingsError(f"Refusing to write: merged setting value detected for key '{k}' (line {line_str}).")

    # 4. Apply changes in place
    changed: dict[str, tuple[str, str]] = {}
    warnings: list[str] = []
    remaining_to_add = dict(validated_changes)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in line:
            k_part, v_part = line.split("=", 1)
            k_clean = k_part.strip()
            if k_clean in remaining_to_add:
                new_val = remaining_to_add.pop(k_clean)
                old_val = v_part.strip()
                if (old_val.startswith('"') and old_val.endswith('"')) or (
                    old_val.startswith("'") and old_val.endswith("'")
                ):
                    old_val = old_val[1:-1]

                # Special handling for ORBIT_MOCK unchecked
                if k_clean == "ORBIT_MOCK" and new_val == "0":
                    changed[k_clean] = (old_val, "0")
                    warnings.append(
                        "ORBIT_MOCK was removed from .env. If ORBIT_MOCK is set in your process environment or terminal, you must unset or clear it there as ORBIT cannot change environment variables."
                    )
                    continue  # Do not include this line in the output

                changed[k_clean] = (old_val, new_val)
                prefix = line[: line.index("=")]
                new_lines.append(f"{prefix}={new_val}")
                continue

        new_lines.append(line)

    # 5. Append missing keys at the end
    # Special handling: if ORBIT_MOCK was not in .env and requested 0, do not add it
    if "ORBIT_MOCK" in remaining_to_add and remaining_to_add["ORBIT_MOCK"] == "0":
        remaining_to_add.pop("ORBIT_MOCK")
        changed["ORBIT_MOCK"] = ("0", "0")

    if remaining_to_add:
        header_exists = any(l.strip() == "# set by ORBIT settings" for l in new_lines)
        if not header_exists:
            if new_lines and new_lines[-1].strip() != "":
                new_lines.append("")
            new_lines.append("# set by ORBIT settings")
        for k, new_val in remaining_to_add.items():
            changed[k] = (SPECS[k].default, new_val)
            new_lines.append(f"{k}={new_val}")

    # 6. Format final byte output
    out_text = newline_str.join(new_lines)
    if new_lines and not out_text.endswith(newline_str):
        out_text += newline_str

    out_bytes = (b"\xef\xbb\xbf" if has_bom else b"") + out_text.encode("utf-8")

    # 7. Atomic write: write .env.bak first, then temp file, then os.replace
    target_dir = path.parent.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        bak_path = target_dir / (path.name + ".bak")
        bak_path.write_bytes(raw_bytes)

    temp_fd, temp_path_str = tempfile.mkstemp(dir=str(target_dir), prefix=f".{path.name}.tmp_")
    try:
        os.write(temp_fd, out_bytes)
        os.close(temp_fd)
        os.replace(temp_path_str, str(path.resolve()))
    except Exception:
        if os.path.exists(temp_path_str):
            try:
                os.remove(temp_path_str)
            except Exception:
                pass
        raise

    return SaveResult(changed=changed, warnings=warnings)
