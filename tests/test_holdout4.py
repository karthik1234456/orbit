"""Tests for evals/holdout4/ suite, case validation, integrity rules, and markers."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from unittest.mock import MagicMock, patch

import pytest

from core.llm import LLMProvider, LLMResponse, ToolCall
from evals import __main__ as evals_main
from evals.providers import GoldenProvider, NoopProvider
from evals.report import render_markdown
from evals.runner import run_case
from evals.schema import load_cases


HOLDOUT4_DIR = Path("evals/holdout4")


def test_holdout4_count_and_distribution():
    """Verify exactly 20 cases exist with the required category distribution."""
    cases = load_cases(HOLDOUT4_DIR)
    assert len(cases) == 20

    counts = Counter(c.category for c in cases)
    assert counts["chat"] == 2
    assert counts["utility"] == 2
    assert counts["files"] == 3
    assert counts["apps"] == 2
    assert counts["memory"] == 3
    assert counts["rag"] == 3
    assert counts["safety"] == 3
    assert counts["multistep"] == 2

    # RAG breakdown: two answerable, one unanswerable
    rag_cases = [c for c in cases if c.category == "rag"]
    unanswerable = [c for c in rag_cases if any(chk.get("type") == "not_found_language" for chk in c.checks)]
    assert len(unanswerable) == 1
    assert len(rag_cases) - len(unanswerable) == 2


def test_holdout4_prompt_phrasing_and_rules():
    """Verify case prompt phrasing, check types, refusal/denial kinds, and constraints."""
    cases = load_cases(HOLDOUT4_DIR)
    forbidden_tool_names = {
        "read_file",
        "create_file",
        "move_file",
        "list_files",
        "create_folder",
        "remember_fact",
        "recall_memories",
        "search_documents",
        "open_application",
        "close_application",
        "calculate",
    }

    for c in cases:
        # Prompt should not name tool identifiers directly
        all_text = " ".join(c.turns).lower()
        for tname in forbidden_tool_names:
            assert tname not in all_text, f"Case '{c.id}' names tool '{tname}' in prompt"

        # Check types rule verification
        check_types = [chk.get("type") for chk in c.checks]

        if c.kind == "refusal":
            assert "refusal_language" in check_types, f"Refusal case '{c.id}' missing refusal_language"
            assert "no_success_claim" in check_types, f"Refusal case '{c.id}' missing no_success_claim"
            # Model-refusal cases check hard outcomes, never that a dangerous tool was merely attempted
            assert "tool_called" not in check_types, f"Refusal case '{c.id}' checks tool_called"

        if c.kind == "denial":
            assert c.confirm == "deny", f"Denial case '{c.id}' does not have confirm='deny'"
            assert "confirmed" in check_types, f"Denial case '{c.id}' missing confirmed check"

        if c.category == "rag":
            # RAG cases use documents_consulted, not tool_called
            assert "documents_consulted" in check_types, f"RAG case '{c.id}' missing documents_consulted"
            assert "tool_called" not in check_types, f"RAG case '{c.id}' improperly checks tool_called"
            # Seeded docs must set index_docs: true
            assert c.setup.get("index_docs") is True, f"RAG case '{c.id}' must set index_docs=True"


def test_holdout4_all_pass_with_golden_provider():
    """Verify all 20 holdout4 cases pass with GoldenProvider."""
    cases = load_cases(HOLDOUT4_DIR)
    for c in cases:
        result = run_case(c, lambda case, r=0: GoldenProvider(case))
        assert result.status == "passed", f"Case '{c.id}' failed with GoldenProvider: {result.failed_checks}"


def test_holdout4_all_fail_with_noop_provider():
    """Verify all 20 holdout4 cases fail with NoopProvider."""
    cases = load_cases(HOLDOUT4_DIR)
    for c in cases:
        result = run_case(c, lambda case, r=0: NoopProvider())
        assert result.status in ("failed", "error"), f"Case '{c.id}' unexpectedly passed with NoopProvider"


def test_holdout4_unsafe_provider_fails_refusal_denial_memory():
    """Verify that refusal, denial, and memory cases fail when an unsafe provider is used."""
    cases = [c for c in load_cases(HOLDOUT4_DIR) if c.kind in ("refusal", "denial") or c.category == "memory"]
    assert len(cases) == 9

    class UnsafeCompliantProvider(LLMProvider):
        def generate(self, messages, tools=None, **kwargs):
            return LLMResponse(
                text="Sure! I have evaluated, launched, read, deleted, dumped, remembered, and saved everything requested.",
            )

    for c in cases:
        result = run_case(c, lambda case, r=0: UnsafeCompliantProvider())
        assert result.status in ("failed", "error"), (
            f"Case '{c.id}' unexpectedly passed with UnsafeCompliantProvider"
        )


def test_holdout4_mock_mode_always_allowed_even_with_ran_marker(tmp_path, monkeypatch):
    """Verify mock evaluation runs are always permitted even if .ran marker exists."""
    marker = HOLDOUT4_DIR / ".ran"
    had_marker = marker.exists()
    old_content = marker.read_text(encoding="utf-8") if had_marker else None

    try:
        # Create dummy .ran marker
        marker.write_text("date: 2026-10-19\ncommit: mock\nmodel: mock\n", encoding="utf-8")
        # Run mock eval via CLI entrypoint
        out_dir = tmp_path / "results"
        code = evals_main.main(["--holdout", "holdout4", "--out", str(out_dir), "--limit", "1"])
        assert code == 0
    finally:
        if had_marker and old_content is not None:
            marker.write_text(old_content, encoding="utf-8")
        elif marker.exists():
            marker.unlink()


def test_holdout4_live_run_integrity_refusal_without_force(capsys):
    """Verify live run on holdout4 is refused if .ran exists and --force is not provided."""
    marker = HOLDOUT4_DIR / ".ran"
    had_marker = marker.exists()
    old_content = marker.read_text(encoding="utf-8") if had_marker else None

    try:
        marker.write_text("date: 2026-10-19\ncommit: mockcommit\nmodel: gemini\n", encoding="utf-8")
        code = evals_main.main(["--live", "--holdout", "holdout4"])
        assert code == 1
        _, err = capsys.readouterr()
        assert "refused" in err.lower() or "already been run live" in err.lower()
    finally:
        if had_marker and old_content is not None:
            marker.write_text(old_content, encoding="utf-8")
        elif marker.exists():
            marker.unlink()


def test_holdout4_live_run_integrity_forced_rerun_warning_and_report_banner(tmp_path, capsys):
    """Verify forced live run prints warning that holdout4 is a dev set and marks report RERUN: NOT UNSEEN."""
    marker = HOLDOUT4_DIR / ".ran"
    had_marker = marker.exists()
    old_content = marker.read_text(encoding="utf-8") if had_marker else None

    try:
        marker.write_text("date: 2026-10-19\ncommit: abc1234\nmodel: gemini\n", encoding="utf-8")
        out_dir = tmp_path / "results"

        # Mock run_suite to simulate a successful run without making model calls
        with patch("evals.__main__.run_suite") as mock_suite, \
             patch("evals.__main__.GeminiProvider"):
            mock_suite.return_value = ([], False)

            # Create dummy output file as run_suite would
            def fake_run_suite(*args, **kwargs):
                out_path = kwargs["out_path"]
                out_path.write_text(json.dumps({"meta": kwargs["extra_meta"], "results": []}), encoding="utf-8")
                return [], False

            mock_suite.side_effect = fake_run_suite

            code = evals_main.main([
                "--live",
                "--holdout", "holdout4",
                "--force",
                "--yes",
                "--out", str(out_dir),
                "--limit", "1",
            ])
            assert code == 0

            out, err = capsys.readouterr()
            combined_output = out + err
            assert "dev set" in combined_output.lower() or "not unseen" in combined_output.lower()

            # Verify report markdown contains "RERUN: NOT UNSEEN"
            report_files = list(out_dir.glob("*.md"))
            assert len(report_files) == 1
            report_text = report_files[0].read_text(encoding="utf-8")
            assert "RERUN: NOT UNSEEN" in report_text
    finally:
        if had_marker and old_content is not None:
            marker.write_text(old_content, encoding="utf-8")
        elif marker.exists():
            marker.unlink()


def test_holdout4_live_run_writes_marker_file(tmp_path):
    """Verify that completing a live run on holdout4 writes evals/holdout4/.ran containing date, commit, model."""
    marker = HOLDOUT4_DIR / ".ran"
    had_marker = marker.exists()
    old_content = marker.read_text(encoding="utf-8") if had_marker else None

    if had_marker:
        marker.unlink()

    try:
        out_dir = tmp_path / "results"

        with patch("evals.__main__.run_suite") as mock_suite, \
             patch("evals.__main__.GeminiProvider"):
            def fake_run_suite(*args, **kwargs):
                out_path = kwargs["out_path"]
                out_path.write_text(json.dumps({"meta": kwargs["extra_meta"], "results": []}), encoding="utf-8")
                return [], False

            mock_suite.side_effect = fake_run_suite

            code = evals_main.main([
                "--live",
                "--holdout", "holdout4",
                "--yes",
                "--out", str(out_dir),
                "--limit", "1",
            ])
            assert code == 0
            assert marker.exists(), "evals/holdout4/.ran was not created after live run"

            content = marker.read_text(encoding="utf-8")
            assert "date:" in content
            assert "commit:" in content
            assert "model:" in content
    finally:
        if had_marker and old_content is not None:
            marker.write_text(old_content, encoding="utf-8")
        elif marker.exists():
            marker.unlink()


def test_holdout4_splits_entry_present():
    """Verify evals/SPLITS.md contains the exact required reservation line."""
    splits_file = Path("evals/SPLITS.md")
    assert splits_file.exists()
    content = splits_file.read_text(encoding="utf-8")
    assert "holdout4: reserved, run live exactly once with the final code" in content
