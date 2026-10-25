"""Diagnostics reporting module for ORBIT.

Generates a sanitized diagnostics file (logs/diag-<timestamp>.txt) containing
version, git commit, runtime environment, settings, browser status, context,
recent activity events, recent chat lines, and audit entries.
Redacts credentials, secrets, tokens, and caps file size at 20 KB.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from browser.guards import page_type
from config import settings
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
    provider_label: Optional[str] = None,
    model_name: Optional[str] = None,
    recent_chat_lines: Optional[list[str]] = None,
    activity_events: Optional[list[dict]] = None,
    audit_logger: Optional[AuditLogger] = None,
    git_commit_override: Optional[str] = None,
    last_plan: Optional[dict] = None,
) -> str:
    """Gather diagnostic info and construct the sanitized report string."""
    sections: list[str] = []

    # 1. Header and Versions
    git_commit = git_commit_override if git_commit_override is not None else _get_git_commit()
    py_ver = sys.version.split()[0]
    os_ver = f"{platform.system()} {platform.release()} ({platform.version()})"
    now_utc = datetime.now(timezone.utc).isoformat()

    sections.append(
        "=== ORBIT SYSTEM DIAGNOSTICS ===\n"
        f"Timestamp: {now_utc}\n"
        f"ORBIT Version: {VERSION}\n"
        f"Git Commit: {git_commit}\n"
        f"Python Version: {py_ver}\n"
        f"OS Version: {os_ver}"
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
        "SESSION_CONTEXT",
        "OLLAMA_NUM_CTX",
    ):
        val = getattr(settings, key, "not_configured")
        settings_lines.append(f"{key}: {val}")

    sections.append("\n".join(settings_lines))

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
                browser_lines.append(f"Status: open")
                browser_lines.append(f"Host: {host}")
                browser_lines.append(f"Page Type: {pt}")
                browser_lines.append(f"Title: {title[:120]}")
            else:
                browser_lines.append("Status: closed / not running")
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
    provider_label: Optional[str] = None,
    model_name: Optional[str] = None,
    recent_chat_lines: Optional[list[str]] = None,
    activity_events: Optional[list[dict]] = None,
    audit_logger: Optional[AuditLogger] = None,
    output_dir: str = "logs",
    git_commit_override: Optional[str] = None,
    last_plan: Optional[dict] = None,
) -> tuple[Path, str]:
    """Generate and write sanitized diagnostics report to logs/diag-<timestamp>.txt.

    Returns (file_path, report_text).
    """
    report_text = build_diagnostics_text(
        app=app,
        agent=agent,
        chat_controller=chat_controller,
        provider_label=provider_label,
        model_name=model_name,
        recent_chat_lines=recent_chat_lines,
        activity_events=activity_events,
        audit_logger=audit_logger,
        git_commit_override=git_commit_override,
        last_plan=last_plan,
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    file_path = out_dir / f"diag-{timestamp}.txt"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    return file_path, report_text
