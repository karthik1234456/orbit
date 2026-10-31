"""Diagnostics reporting module for ORBIT.

Generates a sanitized diagnostics file (logs/diag-<timestamp>.txt) containing
version, git commit, runtime environment, settings, browser status, context,
recent activity events, recent chat lines, and audit entries.
Redacts credentials, secrets, tokens, and caps file size at 20 KB.
"""

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any, Optional, Union
from urllib.parse import urlparse

from browser.guards import page_type
from config import settings
from config.paths import get_diag_dir, get_orbit_home, is_demo_mode
from core.version import VERSION
from security.audit import AuditLogger, redact_data

# Secret patterns for redacting keys, tokens, and credentials
_SECRET_PATTERNS = [
    # Google API keys
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "[REDACTED_API_KEY]"),
    # sk- style API keys (OpenAI, Anthropic, etc.)
    (re.compile(r"\bsk-[0-9A-Za-z\-_]{16,}"), "[REDACTED_API_KEY]"),
    # AWS access keys
    (re.compile(r"\bAKIA[0-9A-Za-z]{16,}\b"), "[REDACTED_AWS_KEY]"),
    # GitHub personal tokens
    (re.compile(r"\bgh[pousr]_[0-9A-Za-z]{10,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b"), "[REDACTED_GH_TOKEN]"),
    # Slack tokens
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    # JWT tokens
    (re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"), "[REDACTED_JWT]"),
    # Bearer tokens
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]+"), "Bearer [REDACTED_TOKEN]"),
    # .env style assignments (e.g. GEMINI_API_KEY=..., SECRET=...)
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL)[A-Z0-9_]*)\s*=\s*['\"]?[^\s'\"]+['\"]?"), r"\1=[REDACTED]"),
    # Credential key-value assignment in text (password: ..., secret: ...)
    (re.compile(r"(?i)\b(password|passwd|pwd|passcode|secret|token|api[\s_-]*key|apikey|credential)\s*[:=]\s*\S+"), r"\1: [REDACTED]"),
    # Generic high-entropy hex strings of 32+ characters
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED_HEX_TOKEN]"),
]

MAX_DIAG_BYTES: int = 20 * 1024  # 20 KB cap

_LAST_RECORDED_PLAN: Optional[dict] = None


def record_plan(
    plan_id: Any,
    source: str = "chain",
    steps: Optional[list[dict]] = None,
    outcome: str = "done",
) -> None:
    """Record a plan execution summary for diagnostics."""
    global _LAST_RECORDED_PLAN
    _LAST_RECORDED_PLAN = {
        "id": plan_id,
        "source": source,
        "steps": steps or [],
        "outcome": outcome,
    }


def get_last_plan() -> Optional[dict]:
    """Retrieve the last recorded plan execution summary."""
    return _LAST_RECORDED_PLAN


def render_diagnostics(diag_result: Any) -> str:
    """Helper returning report text string whether passed a (path, text) tuple or text string."""
    if isinstance(diag_result, tuple) and len(diag_result) >= 2:
        return str(diag_result[1])
    return str(diag_result)


def _get_git_commit() -> str:
    """Retrieve short git commit hash and dirty marker if git is available."""
    try:
        import subprocess
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        dirty = "-dirty" if status else ""
        return f"{commit}{dirty}"
    except Exception:
        return "unknown"


def redact_secrets_text(text: str) -> str:
    """Apply secret pattern redaction to raw text strings."""
    if not text:
        return ""
    res = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        res = pattern.sub(replacement, res)
    return res


def build_diagnostics_text(
    app: Optional[Any] = None,
    agent: Optional[Any] = None,
    chat_controller: Optional[Any] = None,
    voice_controller: Optional[Any] = None,
    provider_label: Optional[str] = None,
    model_name: Optional[str] = None,
    recent_chat_lines: Optional[list[str]] = None,
    activity_events: Optional[list[dict]] = None,
    audit_logger: Optional[AuditLogger] = None,
    git_commit_override: Optional[str] = None,
    last_plan: Optional[dict] = None,
    reminder_store: Optional[Any] = None,
    doctor_checks: Optional[list[Any]] = None,
    doctor_probes: Optional[Any] = None,
    routine_store: Optional[Any] = None,
) -> str:
    """Gather diagnostic info and construct the sanitized report string."""
    sections: list[str] = []

    # 1. Header and Versions
    git_commit = git_commit_override if git_commit_override is not None else _get_git_commit()
    py_ver = sys.version.split()[0]
    os_ver = f"{platform.system()} {platform.release()} ({platform.version()})"
    now_utc = datetime.now(timezone.utc).isoformat()
    home_dir = get_orbit_home()
    orbit_home_name = home_dir.name if home_dir is not None else "none"
    demo_status = "active" if is_demo_mode() else "inactive"

    sections.append(
        "=== ORBIT SYSTEM DIAGNOSTICS ===\n"
        f"Timestamp: {now_utc}\n"
        f"ORBIT Version: {VERSION}\n"
        f"Git Commit: {git_commit}\n"
        f"Python Version: {py_ver}\n"
        f"OS Version: {os_ver}\n"
        f"ORBIT_HOME: {orbit_home_name}\n"
        f"DEMO_MODE: {demo_status}"
    )

    # 2. Provider and Model
    prov = provider_label
    mod = model_name
    if not prov:
        if agent and getattr(agent, "provider", None):
            prov = getattr(agent.provider, "name", agent.provider.__class__.__name__)
        else:
            prov = "MOCK" if getattr(settings, "ORBIT_MOCK", False) else "unknown"
    if not mod:
        if "LIVE" in str(prov):
            mod = getattr(settings, "MODEL_NAME", "gemini-3.8-flash")
        elif "LOCAL" in str(prov):
            mod = getattr(settings, "OLLAMA_MODEL", "qwen3:8b")
        else:
            mod = getattr(settings, "MODEL_NAME", "unknown")

    sections.append(
        "=== PROVIDER & MODEL ===\n"
        f"Provider: {prov}\n"
        f"Active Model: {mod}"
    )

    # 2b. Voice Subsystem Telemetry
    voice_ctrl = voice_controller
    if voice_ctrl is None and app is not None:
        voice_ctrl = getattr(app, "voice_ctrl", None)
    if voice_ctrl is None and chat_controller is not None:
        voice_ctrl = getattr(chat_controller, "voice_controller", None)

    mic_available = "unknown"
    try:
        from voice.audio import list_input_devices

        devices = list_input_devices()
        mic_available = "yes" if devices else "no"
    except Exception:
        mic_available = "unavailable"

    stt_model = getattr(settings, "STT_MODEL", "base.en")
    stt_lang = getattr(settings, "STT_LANGUAGE", "en")
    speak_flag = getattr(voice_ctrl, "speak_replies", getattr(settings, "SPEAK_REPLIES", False))
    last_voice_err = getattr(voice_ctrl, "last_error", "") or "none"
    last_duration = getattr(voice_ctrl, "last_transcription_duration_s", 0.0)
    duration_str = (f"{last_duration}s" if (isinstance(last_duration, (int, float)) and last_duration > 0) else "none")

    hp = getattr(settings, "VOICE_HIGHPASS", 0)
    agc_val = getattr(settings, "VOICE_AGC", 0)
    denoise_val = getattr(settings, "VOICE_DENOISE", 0)
    beam_val = getattr(settings, "STT_BEAM", 1)
    prompt_val = getattr(settings, "STT_PROMPT", 0)
    aliases_val = getattr(settings, "VOICE_ALIASES", 0)
    review_val = getattr(settings, "VOICE_REVIEW", "auto")

    last_snr = getattr(voice_controller, "last_snr_db", None)
    snr_str = f"{last_snr:.1f} dB" if (last_snr is not None and isinstance(last_snr, (int, float))) else "none"
    last_reason = getattr(voice_controller, "last_review_reason", None) or "none"

    tts_engine = getattr(settings, "TTS_ENGINE", "sapi")
    piper_voice = getattr(settings, "PIPER_VOICE", "") or "none"
    from voice.tts_engines import LAST_TTS_FALLBACK_REASON
    fallback_reason = LAST_TTS_FALLBACK_REASON or "none"

    sections.append(
        "=== VOICE ===\n"
        f"Microphone Available: {mic_available}\n"
        f"Speech Model: {stt_model}\n"
        f"Speech Language: {stt_lang}\n"
        f"TTS Engine: {tts_engine}\n"
        f"Piper Voice: {piper_voice}\n"
        f"Last TTS Fallback Reason: {fallback_reason}\n"
        f"Speak Replies: {speak_flag}\n"
        f"Voice Settings: Highpass={hp}, AGC={agc_val}, Denoise={denoise_val}, Beam={beam_val}, Prompt={prompt_val}, Aliases={aliases_val}, Review={review_val}\n"
        f"Last Estimated SNR: {snr_str}\n"
        f"Last Review Reason: {last_reason}\n"
        f"Last Voice Error: {last_voice_err}\n"
        f"Last Transcription Duration: {duration_str}"
    )

    # 2c. RAG & Embeddings Telemetry
    rag_embed_on = getattr(settings, "RAG_EMBEDDINGS", False)
    rag_embed_model = getattr(settings, "RAG_EMBED_MODEL", "embeddinggemma")
    try:
        import rag.embeddings as emb_mod
        from rag.index import DocumentIndex
        idx = DocumentIndex()
        v_stats = idx.get_vector_stats(model=rag_embed_model)
        counts_str = (
            f"Total Chunks: {v_stats['total_chunks']}, "
            f"Vectors: {v_stats['total_vectors']}, "
            f"Stale: {v_stats['stale_vectors']}, "
            f"Missing: {v_stats['missing_vectors']}"
        )
        fb_reason = getattr(emb_mod, "LAST_EMBED_FALLBACK_REASON", "") or "none"
        cpu_rej = "yes" if getattr(emb_mod, "CPU_OPTIONS_REJECTED", False) else "no"
    except Exception as exc:
        counts_str = f"unavailable ({exc})"
        fb_reason = "none"
        cpu_rej = "no"

    sections.append(
        "=== RAG EMBEDDINGS ===\n"
        f"Embeddings Enabled: {'yes' if rag_embed_on else 'no'}\n"
        f"Embedding Model: {rag_embed_model}\n"
        f"Vector Counts: {counts_str}\n"
        f"Last Fallback Reason: {fb_reason}\n"
        f"CPU Options Rejected: {cpu_rej}"
    )

    # 2d. System Controls Telemetry
    sys_enabled = getattr(settings, "SYSTEM_CONTROLS_ENABLED", sys.platform == "win32")
    env_sys = os.getenv("SYSTEM_CONTROLS_ENABLED")
    if env_sys is not None:
        sys_enabled = env_sys.strip().lower() not in ("0", "false", "no", "off")

    from tools.system_tools import get_rate_limit_refusal_count
    refusals = get_rate_limit_refusal_count()

    sections.append(
        "=== SYSTEM CONTROLS ===\n"
        f"SYSTEM_CONTROLS_ENABLED: {sys_enabled}\n"
        f"Rate Limit Refusals: {refusals}"
    )

    # Laptop Control section
    win_ctl = bool(getattr(settings, "WINDOW_CONTROL_ENABLED", True))
    files_on = bool(getattr(settings, "FILES_ENABLED", True))
    sc_on = bool(getattr(settings, "SCREENSHOTS_ENABLED", True))
    typ_on = bool(getattr(settings, "TYPING_ENABLED", False))
    slp_on = bool(getattr(settings, "SLEEP_ENABLED", False))
    plat_supported = sys.platform == "win32"
    has_pillow = importlib.util.find_spec("PIL") is not None

    from config.apps import ALLOWED_APPS, TYPING_ALLOWLIST
    from tools.user_files import default_search_roots
    apps_with_windows = sum(1 for a in ALLOWED_APPS.values() if isinstance(a, dict) and a.get("windows"))
    roots_count = len(default_search_roots())
    typing_allowlist_count = len(TYPING_ALLOWLIST)

    sections.append(
        "=== LAPTOP CONTROL ===\n"
        f"WINDOW_CONTROL_ENABLED: {win_ctl}\n"
        f"FILES_ENABLED: {files_on}\n"
        f"SCREENSHOTS_ENABLED: {sc_on}\n"
        f"TYPING_ENABLED: {typ_on}\n"
        f"SLEEP_ENABLED: {slp_on}\n"
        f"Platform Supported: {plat_supported}\n"
        f"Pillow Present: {has_pillow}\n"
        f"Apps With Windows: {apps_with_windows}\n"
        f"Search Roots Count: {roots_count}\n"
        f"Typing Allowlist Size: {typing_allowlist_count}"
    )

    # HUD section
    ui_style = getattr(settings, "UI_STYLE", "hud")
    ui_theme = getattr(settings, "UI_THEME", "cyan")
    ui_motion = getattr(settings, "UI_MOTION", "full")
    hud_fps = getattr(settings, "HUD_FPS", 30)

    current_tier = "n/a"
    avg_frame_time = "n/a"
    downgrades = 0

    hud_obj = getattr(app, "hud_canvas", None) if app is not None else None
    hud_model = getattr(hud_obj, "model", None) if hud_obj is not None else getattr(app, "hud_model", None)
    if hud_model is not None:
        current_tier = str(getattr(hud_model, "tier", "n/a"))
        avg_ft = getattr(hud_model, "average_frame_time", None)
        if avg_ft is not None:
            avg_frame_time = f"{avg_ft * 1000.0:.2f} ms"
        downgrades = int(getattr(hud_model, "downgrades_count", 0))

    ui_realism = getattr(settings, "UI_REALISM", "full")
    items_last = "n/a"
    items_avg = "n/a"
    sprites = "n/a"
    bitmap_time = "n/a"
    fallback_val = False

    compute_avg = "n/a"
    canvas_avg = "n/a"
    if hud_obj is not None:
        val_comp = getattr(hud_obj, "compute_ms_average", None)
        if isinstance(val_comp, (int, float)):
            compute_avg = f"{val_comp:.2f} ms"
        elif val_comp is not None and not hasattr(val_comp, "_mock_return_value"):
            compute_avg = str(val_comp)

        val_canv = getattr(hud_obj, "canvas_ms_average", None)
        if isinstance(val_canv, (int, float)):
            canvas_avg = f"{val_canv:.2f} ms"
        elif val_canv is not None and not hasattr(val_canv, "_mock_return_value"):
            canvas_avg = str(val_canv)

        val_last = getattr(hud_obj, "items_touched_last_frame", None)
        if isinstance(val_last, (int, float)):
            items_last = str(val_last)
        elif val_last is not None and not hasattr(val_last, "_mock_return_value"):
            items_last = str(val_last)

        val_avg = getattr(hud_obj, "items_touched_average", None)
        if isinstance(val_avg, (int, float)):
            items_avg = f"{val_avg:.1f}"
        elif val_avg is not None and not hasattr(val_avg, "_mock_return_value"):
            items_avg = str(val_avg)

        val_spr = getattr(hud_obj, "realism_sprite_count", None)
        if isinstance(val_spr, (int, float)):
            sprites = str(val_spr)
        elif val_spr is not None and not hasattr(val_spr, "_mock_return_value"):
            sprites = str(val_spr)

        val_bt = getattr(hud_obj, "bitmap_build_ms", None)
        if isinstance(val_bt, (int, float)) and val_bt > 0:
            bitmap_time = f"{val_bt:.1f} ms"
        elif val_bt is not None and not hasattr(val_bt, "_mock_return_value"):
            bitmap_time = str(val_bt)

        fb = getattr(hud_obj, "realism_fallback", False)
        if not hasattr(fb, "_mock_return_value"):
            fallback_val = fb or False

    sections.append(
        "=== HUD ===\n"
        f"UI Style: {ui_style}\n"
        f"UI Theme: {ui_theme}\n"
        f"UI Motion: {ui_motion}\n"
        f"UI Realism: {ui_realism}\n"
        f"HUD FPS: {hud_fps}\n"
        f"Current Tier: {current_tier}\n"
        f"Average Frame Time: {avg_frame_time}\n"
        f"Governor Downgrades: {downgrades}\n"
        f"Items Touched (Last): {items_last}\n"
        f"Items Touched (Avg): {items_avg}\n"
        f"Compute Time (Avg): {compute_avg}\n"
        f"Canvas Time (Avg): {canvas_avg}\n"
        f"Realism Sprites: {sprites}\n"
        f"Bitmap Build Time: {bitmap_time}\n"
        f"Realism Fallback: {fallback_val}"
    )

    # 2f. Interaction Telemetry (flags and counters only, no text)
    ui_interactive = getattr(settings, "UI_INTERACTIVE", 1)
    drag_count = 0
    zoom_factor = 1.0
    radial_opens = 0
    palette_opens = getattr(app, "palette_opens", 0) if app is not None and isinstance(getattr(app, "palette_opens", 0), int) else 0
    notices_count = 0

    if hud_obj is not None:
        v_dc = getattr(hud_obj, "drag_count", None)
        if isinstance(v_dc, int):
            drag_count = v_dc
        v_zf = getattr(hud_obj, "zoom_factor", None)
        if isinstance(v_zf, (int, float)):
            zoom_factor = float(v_zf)
        v_ro = getattr(hud_obj, "radial_menu_opens", None)
        if isinstance(v_ro, int):
            radial_opens = v_ro
        v_po = getattr(hud_obj, "palette_opens", None)
        if isinstance(v_po, int):
            palette_opens = v_po
        v_nc = getattr(hud_obj, "notices_count", None)
        if isinstance(v_nc, int):
            notices_count = v_nc

    sections.append(
        "=== INTERACTION ===\n"
        f"UI_INTERACTIVE: {ui_interactive}\n"
        f"Orb Drag Count: {drag_count}\n"
        f"Orb Zoom Factor: {zoom_factor:.2f}\n"
        f"Radial Menu Opens: {radial_opens}\n"
        f"Palette Opens: {palette_opens}\n"
        f"Notices Shown: {notices_count}"
    )

    # 2g. UI Tabs (names, sampler active states, counts only)
    tab_lines = [
        "=== UI TABS ===",
        "Tabs: Today, Activity, Context, Memories, Documents, Reminders, Routines, Contacts, Files, System, Insights, Verify, Last run",
    ]
    if app is not None and hasattr(app, "tab_rail"):
        active_t = app.tab_rail.get_active_tab() if hasattr(app.tab_rail, "get_active_tab") else "unknown"
        today_active = bool(getattr(getattr(app, "today_view", None), "_is_active", False))
        sys_active = bool(getattr(getattr(app, "system_view", None), "_is_active", False))
        tab_lines.append(f"Active Tab: {active_t}")
        tab_lines.append(f"Samplers: today={'active' if today_active else 'inactive'}, system={'active' if sys_active else 'inactive'}")
    sections.append("\n".join(tab_lines))

    # Hands-Free Telemetry
    hf_enabled = getattr(settings, "HANDS_FREE", 0)
    wake_phrase = getattr(settings, "WAKE_PHRASE", "hey orbit")
    wake_sensitivity = getattr(settings, "WAKE_SENSITIVITY", 0.80)
    wake_model = getattr(settings, "WAKE_STT_MODEL", "tiny.en")
    hf_home = os.getenv("HF_HOME")
    hub_dir = (
        Path(hf_home) / "hub"
        if hf_home
        else Path.home() / ".cache" / "huggingface" / "hub"
    )
    wake_patterns = [
        hub_dir / f"models--Systran--faster-whisper-{wake_model}",
        hub_dir / f"models--guillaumekln--faster-whisper-{wake_model}",
        hub_dir / f"models--{wake_model}",
    ]
    wake_cached = any(p.is_dir() for p in wake_patterns)
    mic_ok = False
    try:
        from voice.audio import list_input_devices
        mic_ok = len(list_input_devices()) > 0
    except Exception:
        pass

    hfc = getattr(app, "handsfree_controller", None) or getattr(chat_controller, "handsfree_controller", None)
    counters = getattr(hfc, "counters", {}) if hfc else {}
    wake_events = counters.get("wake_events", 0)
    drops = counters.get("drops_low_confidence", 0)
    cooldowns = counters.get("cooldowns", 0)
    auto_offs = counters.get("auto_offs", 0)

    sections.append(
        "=== HANDS-FREE ===\n"
        f"Hands-Free Enabled: {'yes' if hf_enabled else 'no'}\n"
        f"Wake Phrase: {wake_phrase}\n"
        f"Wake Sensitivity: {wake_sensitivity}\n"
        f"Wake STT Model: {wake_model}\n"
        f"Wake Model Cached: {'yes' if wake_cached else 'no'}\n"
        f"Microphone Available: {'yes' if mic_ok else 'no'}\n"
        f"Wake Events: {wake_events}\n"
        f"Drops for Low Confidence: {drops}\n"
        f"Cooldown Events: {cooldowns}\n"
        f"Auto-Off Events: {auto_offs}"
    )

    # 3. Settings That Matter
    settings_lines = ["=== SETTINGS ==="]
    # BROWSER_* settings
    for key in sorted(dir(settings)):
        if key.startswith("BROWSER_"):
            val = getattr(settings, key)
            settings_lines.append(f"{key}: {val}")
    # Specific requested settings
    for key in (
        "RAG_AUTO",
        "COMMAND_SHORTCUTS",
        "CHAINS_ENABLED",
        "PLAN_PREVIEW",
        "PLAN_MAX_STEPS",
        "PLAN_MAX_SECONDS",
        "SPEAK_REPLIES",
        "VOICE_IN_UI",
        "SESSION_CONTEXT",
        "OLLAMA_NUM_CTX",
        "REMINDERS_ENABLED",
        "TOOL_ROUTING",
        "ROUTINES_ENABLED",
        "ROUTINE_PREVIEW",
        "RAG_EMBEDDINGS",
        "RAG_AUTO_SCORING",
        "RAG_EMBED_MODEL",
        "RAG_EMBED_THRESHOLD",
        "RAG_EMBED_MARGIN",
        "RAG_EMBED_TOOL",
        "SYSTEM_CONTROLS_ENABLED",
        "UI_STYLE",
        "UI_THEME",
        "UI_MOTION",
        "UI_REALISM",
        "UI_INTERACTIVE",
        "HUD_FPS",
    ):
        val = getattr(settings, key, "not_configured")
        settings_lines.append(f"{key}: {val}")
    settings_lines.append(f"ORBIT_HOME: {orbit_home_name}")
    settings_lines.append(f"DEMO_MODE: {demo_status}")

    # Settings store items whose source is environment or value differs from default
    try:
        from config.settings_store import is_secret_like, read_effective

        effective = read_effective()
        for key, eff in sorted(effective.items()):
            if is_secret_like(key):
                continue
            if eff.source == "environment" or eff.value != eff.spec.default:
                source_tag = f" (source: {eff.source})"
                override_tag = " [overridden]" if eff.overridden_by_env else ""
                entry_line = f"{key}: {eff.value}{source_tag}{override_tag}"
                if entry_line not in settings_lines:
                    settings_lines.append(entry_line)
    except Exception:
        pass

    sections.append("\n".join(settings_lines))

    # Reminders Status
    rem_store = reminder_store
    if rem_store is None and app is not None and hasattr(app, "reminder_store"):
        rem_store = getattr(app, "reminder_store")
    if rem_store is None and agent is not None and hasattr(agent, "reminder_store"):
        rem_store = getattr(agent, "reminder_store")
    if rem_store is None and chat_controller is not None:
        rem_store = getattr(chat_controller, "reminder_store", None)
        if rem_store is None and hasattr(chat_controller, "agent"):
            rem_store = getattr(chat_controller.agent, "reminder_store", None)

    rem_enabled = getattr(settings, "REMINDERS_ENABLED", True)
    env_rem = os.getenv("REMINDERS_ENABLED")
    if env_rem is not None:
        rem_enabled = env_rem.strip().lower() not in ("0", "false", "no", "off")

    rem_lines = ["=== REMINDERS ==="]
    rem_lines.append(f"REMINDERS_ENABLED: {rem_enabled}")
    if rem_store is not None:
        try:
            counts = rem_store.count_by_status()
            for st in ("active", "fired", "missed", "dismissed", "cancelled"):
                rem_lines.append(f"{st.capitalize()}: {counts.get(st, 0)}")
            active_items = rem_store.list_active()
            if active_items:
                sorted_items = sorted(active_items, key=lambda x: x.due_at)
                next_due_dt = sorted_items[0].due_at
                due_val = next_due_dt if isinstance(next_due_dt, str) else next_due_dt.isoformat()
                rem_lines.append(f"Next Due: {due_val}")
            else:
                rem_lines.append("Next Due: none")
        except Exception as exc:
            rem_lines.append(f"Error reading reminder store: {exc}")
    else:
        rem_lines.append("Store: not configured")
    sections.append("\n".join(rem_lines))

    # Routines Telemetry
    r_store = routine_store
    if r_store is None and app is not None and hasattr(app, "routine_store"):
        r_store = getattr(app, "routine_store")
    if r_store is None and agent is not None and hasattr(agent, "routine_store"):
        r_store = getattr(agent, "routine_store")
    if r_store is None and chat_controller is not None:
        r_store = getattr(chat_controller, "routine_store", None)
        if r_store is None and hasattr(chat_controller, "agent"):
            r_store = getattr(chat_controller.agent, "routine_store", None)

    routines_on = getattr(settings, "ROUTINES_ENABLED", True)
    env_routines = os.getenv("ROUTINES_ENABLED")
    if env_routines is not None:
        routines_on = env_routines.strip().lower() not in ("0", "false", "no", "off")

    routine_count = 0
    if r_store is not None:
        try:
            items = r_store.list()
            routine_count = len(items)
        except Exception:
            pass

    routine_lines = ["=== ROUTINES ==="]
    routine_lines.append(f"ROUTINES_ENABLED: {routines_on}")
    routine_lines.append(f"Count: {routine_count}")
    sections.append("\n".join(routine_lines))

    # Tool Routing Status
    routing_lines = ["=== TOOL ROUTING ==="]
    routing_on = getattr(settings, "TOOL_ROUTING", False)
    eff_agent = agent
    if eff_agent is None and app is not None and hasattr(app, "agent"):
        eff_agent = app.agent
    if eff_agent is None and chat_controller is not None and hasattr(chat_controller, "agent"):
        eff_agent = chat_controller.agent

    if eff_agent is not None and hasattr(eff_agent, "tool_routing_enabled"):
        try:
            routing_on = eff_agent.tool_routing_enabled()
        except Exception:
            pass
    routing_lines.append(f"TOOL_ROUTING: {routing_on}")

    last_run = getattr(eff_agent, "last_run", None) if eff_agent else None
    if last_run is not None:
        routing_lines.append(f"Last Turn Routing Active: {last_run.routing_on}")
        routing_lines.append(f"Tools Offered: {last_run.tools_offered_count}")
        routing_lines.append(f"Tools Registered: {last_run.tools_registered_count}")
        routing_lines.append(f"Groups: {', '.join(sorted(last_run.routing_groups)) if last_run.routing_groups else 'none'}")
        routing_lines.append(f"Off-Route Calls: {last_run.off_route_call}")
        routing_lines.append(f"Routing Retry: {last_run.routing_retry}")
        prompt_tokens_str = str(last_run.prompt_tokens) if last_run.prompt_tokens is not None else "none"
        routing_lines.append(f"Prompt Tokens: {prompt_tokens_str}")
    else:
        routing_lines.append("Last Turn: none")
    sections.append("\n".join(routing_lines))

    # Doctor Summary and Failing Check Names
    doc_lines = ["=== DOCTOR ==="]
    try:
        from core.doctor import run_checks

        d_checks = doctor_checks
        if d_checks is None:
            d_checks = run_checks(probes=doctor_probes, fast=True)
        ok_count = sum(1 for c in d_checks if getattr(c, "status", "").upper() == "OK")
        warn_count = sum(1 for c in d_checks if getattr(c, "status", "").upper() == "WARN")
        fail_count = sum(1 for c in d_checks if getattr(c, "status", "").upper() == "FAIL")
        doc_lines.append(f"Summary: {ok_count} OK, {warn_count} warnings, {fail_count} failures")
        failing_names = [getattr(c, "name", "") for c in d_checks if getattr(c, "status", "").upper() == "FAIL"]
        if failing_names:
            doc_lines.append(f"Failing Checks: {', '.join(failing_names)}")
        else:
            doc_lines.append("Failing Checks: none")
    except Exception as exc:
        doc_lines.append(f"Summary: unavailable ({exc.__class__.__name__})")
        doc_lines.append("Failing Checks: none")
    sections.append("\n".join(doc_lines))

    # 3b. Verification Results Summary (privacy protected)
    verify_lines = ["=== VERIFICATION RESULTS ==="]
    try:
        from verify.store import VerificationStore
        vstore = VerificationStore()
        latest_run = vstore.get_latest_run()
        if latest_run is not None:
            v_sum = latest_run.summary()
            verify_lines.append(f"Latest Run: {latest_run.finished_at}")
            verify_lines.append(f"App Version: v{latest_run.app_version}")
            verify_lines.append(f"Git Commit: {latest_run.git_commit or 'unknown'} (dirty: {latest_run.dirty})")
            verify_lines.append(f"Pass: {v_sum.get('pass', 0)}")
            verify_lines.append(f"Fail: {v_sum.get('fail', 0)}")
            verify_lines.append(f"Skip: {v_sum.get('skip', 0)}")
            verify_lines.append(f"Blocked: {v_sum.get('blocked', 0)}")
            verify_lines.append(f"Total Items: {v_sum.get('total', 0)}")
        else:
            verify_lines.append("Latest Run: none")
            verify_lines.append("Status: No verification runs recorded yet")
    except Exception as exc:
        verify_lines.append(f"Status: unavailable ({exc.__class__.__name__})")
    sections.append("\n".join(verify_lines))

    # 4. Browser Status (host, page type, title sanitized)
    browser_ctrl = getattr(app, "browser_controller", None) if app else None
    if browser_ctrl is None and agent:
        browser_ctrl = getattr(agent, "browser_controller", None)

    browser_lines = ["=== BROWSER STATUS ==="]
    if browser_ctrl is not None:
        try:
            stat = browser_ctrl.status()
            if stat and stat.get("alive"):
                raw_url = stat.get("url", "")
                parsed = urlparse(raw_url)
                host = parsed.netloc or raw_url or "unknown"
                allow_lb = getattr(getattr(browser_ctrl, "_backend", None), "allow_loopback", False)
                pt = page_type(raw_url, allow_loopback=allow_lb)
                raw_title = stat.get("title", "")
                title = " ".join(str(raw_title).strip().split())
                for ch in ("<", ">", "`"):
                    title = title.replace(ch, "")
                browser_lines.append("Status: open")
                browser_lines.append(f"Host: {host}")
                browser_lines.append(f"Page Type: {pt}")
                browser_lines.append(f"Title: {title[:120]}")
            else:
                browser_lines.append("Status: closed / not running")

            b_controlled = stat.get("browser_controlled") if (stat and "browser_controlled" in stat) else getattr(browser_ctrl, "browser_controlled", True)
            fb_count = stat.get("fallback_count") if (stat and "fallback_count" in stat) else getattr(browser_ctrl, "fallback_count", 0)
            rst_count = stat.get("restart_count") if (stat and "restart_count" in stat) else getattr(browser_ctrl, "restart_count", 0)
            l_err = stat.get("last_error") if (stat and "last_error" in stat) else getattr(browser_ctrl, "last_error", None)

            is_blocked = False
            if stat and "google_blocked" in stat:
                is_blocked = bool(stat["google_blocked"])
            elif hasattr(browser_ctrl, "is_google_blocked"):
                is_blocked = bool(browser_ctrl.is_google_blocked())

            if is_blocked:
                rem = browser_ctrl.cooldown_remaining_s() if hasattr(browser_ctrl, "cooldown_remaining_s") else 0
                cooldown_str = f"active ({rem}s remaining)" if rem > 0 else "active"
            else:
                cooldown_str = "inactive"

            browser_lines.append(f"Browser Controlled: {b_controlled}")
            browser_lines.append(f"Fallback Count: {fb_count}")
            browser_lines.append(f"Restart Count: {rst_count}")
            browser_lines.append(f"Cooldown Status: {cooldown_str}")
            browser_lines.append(f"Last Error: {l_err or 'none'}")
        except Exception as exc:
            browser_lines.append(f"Status: error inspecting browser: {exc}")
    else:
        browser_lines.append("Status: not configured")
    sections.append("\n".join(browser_lines))

    # 4b. Last Browser Failure
    last_fail_lines = ["=== LAST BROWSER FAILURE ==="]
    last_fail = getattr(browser_ctrl, "last_failure", None) if browser_ctrl else None
    if not last_fail and browser_ctrl and getattr(browser_ctrl, "_backend", None):
        last_fail = getattr(browser_ctrl._backend, "last_failure", None)

    if last_fail and isinstance(last_fail, dict):
        pt = last_fail.get("page_type", "unknown")
        host = last_fail.get("url_host", "unknown")
        path = last_fail.get("url_path", "")
        vp = last_fail.get("viewport_size", {})
        vp_str = f"{vp.get('width', '?')}x{vp.get('height', '?')}" if isinstance(vp, dict) else str(vp)
        last_fail_lines.append(f"Page Type: {pt}")
        last_fail_lines.append(f"Host: {host}")
        last_fail_lines.append(f"Path: {path}")
        last_fail_lines.append(f"Viewport: {vp_str}")

        per_sel = last_fail.get("anchors_per_selector", {})
        last_fail_lines.append("Anchors per selector:")
        if per_sel and isinstance(per_sel, dict):
            for sel, cnt in per_sel.items():
                last_fail_lines.append(f"  {sel}: {cnt}")
        else:
            last_fail_lines.append("  none")

        excl = last_fail.get("exclusions", {})
        last_fail_lines.append("Exclusions:")
        if excl and isinstance(excl, dict):
            for k, cnt in excl.items():
                last_fail_lines.append(f"  {k}: {cnt}")
        else:
            last_fail_lines.append("  none")
    else:
        last_fail_lines.append("No browser failure recorded.")
    sections.append("\n".join(last_fail_lines))

    # 4c. Browser Selectors
    selectors_lines = ["=== BROWSER SELECTORS ==="]
    try:
        from config.browser_sites import (
            SELECTORS,
            YOUTUBE_CONTAINER_SELECTORS,
            YOUTUBE_WATCH_ANCHOR_SELECTORS,
            YOUTUBE_RENDERER_ANCHOR_SELECTORS,
            YOUTUBE_SHORTS_ANCHOR_SELECTORS,
            YOUTUBE_EXCLUSION_SELECTORS,
        )
        selectors_lines.append("General Selectors:")
        for site, sels in SELECTORS.items():
            selectors_lines.append(f"  {site}: {', '.join(sels)}")
        selectors_lines.append("YouTube Containers:")
        for c_key, c_sel in YOUTUBE_CONTAINER_SELECTORS.items():
            selectors_lines.append(f"  {c_key}: {c_sel}")
        selectors_lines.append("YouTube Watch Anchors:")
        for w_sel in YOUTUBE_WATCH_ANCHOR_SELECTORS:
            selectors_lines.append(f"  {w_sel}")
        selectors_lines.append("YouTube Renderer Anchors:")
        for r_sel in YOUTUBE_RENDERER_ANCHOR_SELECTORS:
            selectors_lines.append(f"  {r_sel}")
        selectors_lines.append("YouTube Shorts Anchors:")
        for s_sel in YOUTUBE_SHORTS_ANCHOR_SELECTORS:
            selectors_lines.append(f"  {s_sel}")
        selectors_lines.append("YouTube Exclusions:")
        for e_key, e_sel in YOUTUBE_EXCLUSION_SELECTORS.items():
            selectors_lines.append(f"  {e_key}: {e_sel}")
    except Exception as exc:
        selectors_lines.append(f"Error loading browser selectors: {exc}")
    sections.append("\n".join(selectors_lines))

    # 5. Session Context Block
    sess_state = getattr(agent, "session_state", None) if agent else None
    ctx_lines = ["=== SESSION CONTEXT ==="]
    if sess_state is not None and hasattr(sess_state, "context_block"):
        try:
            block = sess_state.context_block()
            ctx_lines.append(block if block else "No active session context.")
        except Exception as exc:
            ctx_lines.append(f"Error reading session context: {exc}")
    else:
        ctx_lines.append("No active session context.")
    sections.append("\n".join(ctx_lines))

    # 5b. Last Plan
    plan_lines = ["=== LAST PLAN ==="]
    lp = last_plan
    if lp is None and agent and getattr(agent, "last_run", None):
        lp = getattr(agent.last_run, "plan", None)
    if lp is None and app and getattr(app, "agent", None) and getattr(app.agent, "last_run", None):
        lp = getattr(app.agent.last_run, "plan", None)
    if lp is None:
        lp = _LAST_RECORDED_PLAN

    if lp and isinstance(lp, dict):
        p_id = lp.get("id", "—")
        p_source = lp.get("source", "—")
        p_outcome = lp.get("outcome", "—")
        plan_lines.append(f"Plan ID: {p_id}")
        plan_lines.append(f"Source: {p_source}")
        plan_lines.append(f"Outcome: {p_outcome}")
        p_steps = lp.get("steps", [])
        if p_steps:
            plan_lines.append("Steps:")
            for s_idx, s in enumerate(p_steps, 1):
                s_lbl = s.get("label", s.get("tool", f"step {s_idx}"))
                s_status = s.get("status", "unknown")
                plan_lines.append(f"  {s_idx}. {s_lbl}: {s_status}")
        else:
            plan_lines.append("Steps: none")
    else:
        plan_lines.append("No plan recorded.")
    sections.append("\n".join(plan_lines))

    # 6. Recent Activity Events
    act_lines = ["=== RECENT ACTIVITY EVENTS ==="]
    events_to_show: list[dict] = []
    if activity_events is not None:
        events_to_show = list(activity_events)
    elif agent and getattr(agent, "last_run", None) and getattr(agent.last_run, "steps", None):
        for s in agent.last_run.steps:
            events_to_show.append({
                "task": 1,
                "step": s.index,
                "tool": s.tool,
                "status": s.status,
                "detail": s.result,
            })

    if events_to_show:
        for idx, ev in enumerate(events_to_show, start=1):
            task_id = ev.get("task", "—")
            step_id = ev.get("step", "—")
            tool_name = ev.get("tool", "—")
            status_val = ev.get("status", "—")
            raw_detail = str(ev.get("detail", ev.get("info", "")))[:120].strip()
            clean_detail = " ".join(raw_detail.split())
            act_lines.append(
                f"[{idx}] Task: {task_id} | Step: {step_id} | Tool: {tool_name} | "
                f"Status: {status_val} | Detail: {clean_detail}"
            )
    else:
        act_lines.append("No activity events recorded.")
    sections.append("\n".join(act_lines))

    # 7. Recent Chat Lines (each message cut to 160 characters)
    chat_section_lines = ["=== RECENT CHAT ==="]
    chat_lines_to_show = list(recent_chat_lines or [])
    if chat_lines_to_show:
        for line in chat_lines_to_show:
            clean_line = " ".join(str(line).strip().split())
            chat_section_lines.append(clean_line[:160])
    else:
        chat_section_lines.append("No chat lines recorded.")
    sections.append("\n".join(chat_section_lines))

    # 7b. Messaging & Contacts (counts and flags only, NO PII, no names, no numbers, no emails, no message texts)
    msg_lines = ["=== MESSAGING & CONTACTS ==="]
    messaging_enabled = bool(getattr(settings, "MESSAGING_ENABLED", True))
    send_mode = getattr(settings, "WHATSAPP_SEND_MODE", "draft")
    c_count = 0
    try:
        c_store = getattr(agent, "contact_store", None)
        if c_store is None:
            from contacts.store import ContactStore
            c_store = ContactStore()
        c_count = c_store.count()
    except Exception:
        c_count = 0
    msg_lines.append(f"Messaging Enabled: {messaging_enabled}")
    msg_lines.append(f"WhatsApp Send Mode: {send_mode}")
    msg_lines.append(f"Total Contacts: {c_count}")
    sections.append("\n".join(msg_lines))

    # 8. Last 30 Audit Entries (redacted)
    audit = audit_logger or (getattr(app, "audit", None) if app else None) or (getattr(agent, "audit", None) if agent else None) or AuditLogger()
    audit_section_lines = ["=== RECENT AUDIT ENTRIES (Last 30) ==="]
    try:
        all_records = audit.read_all() if hasattr(audit, "read_all") else []
        records_to_show = all_records[-30:]
        if records_to_show:
            for r in records_to_show:
                ts = r.get("timestamp", "")
                t_name = r.get("tool", "")
                dec = r.get("decision", "")
                out = r.get("outcome", "")
                args_redacted = redact_data(r.get("args", {}))
                audit_section_lines.append(
                    f"{ts} | {t_name} | decision={dec} outcome={out} | args={json.dumps(args_redacted)}"
                )
        else:
            audit_section_lines.append("No audit entries recorded.")
    except Exception as exc:
        audit_section_lines.append(f"Error reading audit logs: {exc}")
    sections.append("\n".join(audit_section_lines))

    raw_text = "\n\n".join(sections) + "\n"

    # Redact secret patterns
    sanitized_text = redact_secrets_text(raw_text)

    # Size cap at 20 KB (20,480 bytes UTF-8)
    encoded = sanitized_text.encode("utf-8")
    if len(encoded) > MAX_DIAG_BYTES:
        notice = "\n[Truncated to 20 KB size cap]"
        notice_bytes = notice.encode("utf-8")
        allowed = MAX_DIAG_BYTES - len(notice_bytes)
        lines = sanitized_text.split("\n")
        start_idx = 0
        cand = sanitized_text
        while start_idx < len(lines):
            cand = "\n".join(lines[start_idx:])
            if len(cand.encode("utf-8")) <= allowed:
                sanitized_text = cand + notice
                break
            start_idx += 1
        else:
            sanitized_text = cand.encode("utf-8")[:allowed].decode("utf-8", errors="ignore") + notice

    return sanitized_text


def generate_diagnostics(
    app: Optional[Any] = None,
    agent: Optional[Any] = None,
    chat_controller: Optional[Any] = None,
    voice_controller: Optional[Any] = None,
    provider_label: Optional[str] = None,
    model_name: Optional[str] = None,
    recent_chat_lines: Optional[list[str]] = None,
    activity_events: Optional[list[dict]] = None,
    audit_logger: Optional[AuditLogger] = None,
    output_dir: Optional[Union[str, Path]] = None,
    git_commit_override: Optional[str] = None,
    last_plan: Optional[Any] = None,
    reminder_store: Optional[Any] = None,
    doctor_checks: Optional[list[Any]] = None,
    doctor_probes: Optional[Any] = None,
    routine_store: Optional[Any] = None,
) -> tuple[Path, str]:
    """Generate and write sanitized diagnostics report to logs/diag-<timestamp>.txt.

    Returns (file_path, report_text).
    """
    report_text = build_diagnostics_text(
        app=app,
        agent=agent,
        chat_controller=chat_controller,
        voice_controller=voice_controller,
        provider_label=provider_label,
        model_name=model_name,
        recent_chat_lines=recent_chat_lines,
        activity_events=activity_events,
        audit_logger=audit_logger,
        git_commit_override=git_commit_override,
        last_plan=last_plan,
        reminder_store=reminder_store,
        doctor_checks=doctor_checks,
        doctor_probes=doctor_probes,
        routine_store=routine_store,
    )

    out_dir = Path(output_dir if output_dir is not None else get_diag_dir())
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    file_path = out_dir / f"diag-{timestamp}.txt"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    return file_path, report_text


# Backward-compatible alias
build_diagnostics = build_diagnostics_text
