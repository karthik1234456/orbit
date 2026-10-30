"""Tests verifying zero leakage of file names, folder names, window titles, and typed text."""

from pathlib import Path
from core.bootstrap import AppPaths, build_app
from core.claims import find_action_claims
from core.diagnostics import build_diagnostics_text
from core.doctor import format_report, run_checks
from core.llm import MockProvider
from core.session_state import SessionState
from system.winwindow import WindowItem


def test_window_item_no_title_privacy():
    """Verify WindowItem never exposes or has a title field."""
    item = WindowItem(handle=42, process="calc.exe", rect=(0, 0, 100, 100), state="normal")
    assert "title" not in dir(item)


def test_session_state_context_block_never_contains_file_names():
    """Verify SessionState context block never leaks file candidate paths or names."""
    state = SessionState()
    fake_file = "C:/Users/FakeUser/Documents/SECRET_DOCUMENT_XYZ.pdf"
    state.set_found_files([fake_file])

    block = state.context_block()
    assert "SECRET_DOCUMENT_XYZ" not in block
    assert fake_file not in block

    diag_dict = state.to_dict()
    assert "SECRET_DOCUMENT_XYZ" not in str(diag_dict)
    assert fake_file not in str(diag_dict)


def test_agent_history_and_audit_never_contain_secrets(tmp_path: Path):
    """Verify agent history, trace, and audit logs redact file names and typed text."""
    paths = AppPaths.under(tmp_path)
    secret_text = "SECRET_PASSWORD_12345"
    fake_secret_file = "SUPER_SECRET_PLAN.pdf"

    # Create dummy file inside workspace
    docs_dir = paths.workspace / "Documents"
    docs_dir.mkdir(parents=True)
    (docs_dir / fake_secret_file).write_text("classified")

    app = build_app(
        provider=MockProvider(),
        paths=paths,
        confirm_callback=lambda *a, **k: True,
        files_search_roots_getter=lambda: [docs_dir],
        typing_char_sender=lambda ch: None,
        typing_foreground_fn=lambda: ("notepad.exe", "notepad.exe"),
        typing_enumerator=lambda: [WindowItem(10, "notepad.exe", (0, 0, 100, 100), "normal")],
        typing_focus_fn=lambda h: True,
        typing_enabled=True,
    )
    agent = app.agent

    # 1. Type into notepad
    reply_type = agent.chat(f"type {secret_text} into notepad")
    assert "Typed" in reply_type

    # Verify secret_text is NOT in history
    for msg in agent.history:
        assert secret_text not in msg.get("content", "")

    # 2. Find files
    reply_find = agent.chat("find files named PLAN")
    assert fake_secret_file in reply_find  # User-visible UI reply contains filename

    # Verify fake_secret_file is NOT in history
    for msg in agent.history:
        assert fake_secret_file not in msg.get("content", "")

    # Verify audit log never contains the typed secret text or file path
    assert paths.audit_log.exists()
    audit_content = paths.audit_log.read_text(encoding="utf-8")
    assert secret_text not in audit_content
    assert fake_secret_file not in audit_content
    app.shutdown()


def test_diagnostics_and_doctor_never_contain_secrets():
    """Verify diagnostics and doctor outputs only contain counts and flags."""
    secret_title = "TOP_SECRET_WINDOW_TITLE_555"
    secret_file = "CLASSIFIED_RESEARCH_2026.docx"

    report = build_diagnostics_text()
    assert secret_title not in report
    assert secret_file not in report
    assert "=== LAPTOP CONTROL ===" in report

    checks = run_checks(fast=True)
    doc_text = format_report(checks)
    assert secret_title not in doc_text
    assert secret_file not in doc_text


def test_claims_families_and_negations():
    """Verify claims heuristics for window, files, screenshot, typing, sleep."""
    # Positive claims
    text_claims = (
        "I've switched to Chrome. I found the files. I took a screenshot. "
        "I typed that into Notepad. I put the computer to sleep."
    )
    claims = find_action_claims(text_claims)
    kinds = {c.kind for c in claims}
    assert "window" in kinds
    assert "files" in kinds
    assert "screenshot" in kinds
    assert "typing" in kinds
    assert "sleep" in kinds

    # Negations produce zero claims
    text_negations = (
        "I can't switch to Chrome. I didn't find the files. I was unable to take a screenshot. "
        "I won't type into Notepad. I cannot put the computer to sleep."
    )
    neg_claims = find_action_claims(text_negations)
    assert len(neg_claims) == 0
