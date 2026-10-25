"""Tests for evaluation suite v4: documents_consulted check, RAG cases, and report metrics."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import pytest

from core.llm import LLMProvider, LLMResponse
from evals.checks import (
    CHECK_TYPES,
    OUTCOME_CHECK_TYPES,
    RunObservation,
    ToolEvent,
    run_checks,
)
from evals.metrics import CaseResult, aggregate
from evals.report import render_markdown
from evals.runner import run_case
from evals.schema import CHECK_ALLOWED_KEYS, CaseError, EvalCase, _validate_and_parse_case


def test_schema_validates_documents_consulted():
    """documents_consulted is recognized in schema and rejects unknown keys."""
    assert "documents_consulted" in CHECK_TYPES
    assert "documents_consulted" in OUTCOME_CHECK_TYPES
    assert "documents_consulted" in CHECK_ALLOWED_KEYS
    assert CHECK_ALLOWED_KEYS["documents_consulted"] == {"type"}

    # Valid check definition
    valid_raw = {
        "id": "test_rag_doc",
        "category": "rag",
        "turns": ["What is the deadline?"],
        "checks": [{"type": "documents_consulted"}],
    }
    case = _validate_and_parse_case(valid_raw, set())
    assert case.checks == [{"type": "documents_consulted"}]

    # Invalid check with extra key
    invalid_raw = {
        "id": "test_rag_doc_invalid",
        "category": "rag",
        "turns": ["What is the deadline?"],
        "checks": [{"type": "documents_consulted", "invalid_key": True}],
    }
    with pytest.raises(CaseError, match="unknown extra key"):
        _validate_and_parse_case(invalid_raw, set())


def test_documents_consulted_passes_with_tool_call(tmp_path: Path):
    """documents_consulted passes when a search_documents tool call executed successfully."""
    case = SimpleNamespace(checks=[{"type": "documents_consulted"}])

    obs = RunObservation(
        final_texts=["The deadline is November 15."],
        events=[
            ToolEvent(tool="search_documents", args={"query": "deadline"}, status="success", turn=0),
        ],
        launches=[],
        llm_calls=1,
        latency_s=0.5,
        workspace=tmp_path,
        auto_retrievals=[],
    )

    failures = run_checks(case, obs)
    assert failures == []

    # If the tool call resulted in an error and no auto-retrieval happened, it must fail
    obs_error = RunObservation(
        final_texts=["Error searching"],
        events=[
            ToolEvent(tool="search_documents", args={"query": "deadline"}, status="error", turn=0),
        ],
        launches=[],
        llm_calls=1,
        latency_s=0.5,
        workspace=tmp_path,
        auto_retrievals=[],
    )
    failures_error = run_checks(case, obs_error)
    assert len(failures_error) == 1
    assert "documents_consulted" in failures_error[0]


def test_documents_consulted_passes_with_auto_retrieval(tmp_path: Path):
    """documents_consulted passes when auto retrieval ran with searched=True and injected > 0 or a note."""
    case = SimpleNamespace(checks=[{"type": "documents_consulted"}])

    # 1. Injected > 0
    obs_injected = RunObservation(
        final_texts=["The deadline is November 15."],
        events=[],  # No tool calls
        launches=[],
        llm_calls=1,
        latency_s=0.5,
        workspace=tmp_path,
        auto_retrievals=[
            {"searched": True, "injected": 2, "withheld": 0, "terms": ["deadline"], "note": ""}
        ],
    )
    failures_injected = run_checks(case, obs_injected)
    assert failures_injected == []

    # 2. Not-found note issued
    obs_note = RunObservation(
        final_texts=["I checked the indexed documentation, but it does not mention the lead designer."],
        events=[],  # No tool calls
        launches=[],
        llm_calls=1,
        latency_s=0.5,
        workspace=tmp_path,
        auto_retrievals=[
            {
                "searched": True,
                "injected": 0,
                "withheld": 0,
                "terms": ["lead", "designer"],
                "note": "The indexed documentation does not contain information about lead designer.",
            }
        ],
    )
    failures_note = run_checks(case, obs_note)
    assert failures_note == []


def test_documents_consulted_fails_with_neither(tmp_path: Path):
    """documents_consulted fails when neither a tool call nor an auto retrieval occurred."""
    case = SimpleNamespace(checks=[{"type": "documents_consulted"}])

    # Empty auto_retrievals and no events
    obs_neither = RunObservation(
        final_texts=["I think the deadline is next week."],
        events=[],
        launches=[],
        llm_calls=1,
        latency_s=0.5,
        workspace=tmp_path,
        auto_retrievals=[],
    )
    failures = run_checks(case, obs_neither)
    assert len(failures) == 1
    assert "documents_consulted" in failures[0]

    # Auto retrieval ran but was disabled or did not search and had 0 injected and no note
    obs_unsearched = RunObservation(
        final_texts=["I don't know."],
        events=[],
        launches=[],
        llm_calls=1,
        latency_s=0.5,
        workspace=tmp_path,
        auto_retrievals=[
            {"searched": False, "injected": 0, "withheld": 0, "reason": "index_empty", "note": ""}
        ],
    )
    failures_unsearched = run_checks(case, obs_unsearched)
    assert len(failures_unsearched) == 1
    assert "documents_consulted" in failures_unsearched[0]


class UnsafeDocCitingProvider(LLMProvider):
    """Unsafe provider that inappropriately cites an indexed document on a general query."""

    def generate(self, messages: list[dict], tools: Optional[list[Any]] = None, **kwargs) -> LLMResponse:
        return LLMResponse(
            text="According to server_inventory.md, green plants use chlorophyll for photosynthesis."
        )


def test_no_docs_needed_case_with_injected_docs_fails():
    """A no-docs-needed case fails when an unsafe provider cites the indexed document."""
    no_docs_case = EvalCase(
        id="test_no_docs_needed_failure",
        category="chat",
        turns=["What primary pigment do green plants use to absorb light during photosynthesis?"],
        setup={
            "docs": {
                "server_inventory.md": "Cluster node compute-01: IP 192.168.10.15, 128 cores, Ubuntu 22.04 LTS."
            },
            "index_docs": True,
        },
        expect_tools=[],
        checks=[
            {"type": "no_tools_called"},
            {"type": "tool_not_called", "tool": "search_documents"},
            {"type": "final_contains_any", "texts": ["chlorophyll"]},
            {
                "type": "final_not_contains",
                "texts": ["server_inventory", "compute-01", "indexed", "document", "docs"],
            },
        ],
    )

    result = run_case(
        case=no_docs_case,
        provider_factory=lambda c: UnsafeDocCitingProvider(),
        repeat=0,
    )

    assert result.status == "failed"
    assert any("final_not_contains" in f for f in result.failed_checks)


def test_report_line_computed_on_hand_made_result():
    """Verify that 'Auto-retrieval: N of M turns searched, K injected, P not-found notes' is accurately computed."""
    # Create hand-made results spanning 3 turns
    r1 = CaseResult(
        id="case_rag_1",
        category="rag",
        repeat=0,
        status="passed",
        auto_retrievals=[
            {"searched": True, "injected": 2, "note": ""},
            {"searched": False, "injected": 0, "note": ""},
        ],
    )
    r2 = CaseResult(
        id="case_rag_2",
        category="rag",
        repeat=0,
        status="passed",
        auto_retrievals=[
            {
                "searched": True,
                "injected": 1,
                "note": "Document not found guidance",
            },
        ],
    )

    # 1. Test aggregate()
    agg = aggregate([r1, r2], cases=[])
    overall = agg["overall"]
    expected_headline = "2 of 3 turns searched, 3 injected, 1 not-found notes"
    assert overall["auto_retrieval_headline"] == expected_headline
    assert overall["auto_retrieval_searched"] == 2
    assert overall["auto_retrieval_total_turns"] == 3
    assert overall["auto_retrieval_injected"] == 3
    assert overall["auto_retrieval_not_found_notes"] == 1

    # 2. Test render_markdown()
    report_dict_on = {
        "meta": {
            "mode": "mock",
            "provider": "mock-provider",
            "model": "test-model",
            "cases_version": 4,
            "RAG_AUTO": True,
            "RAG_AUTO_MIN_TERMS": 2,
            "RAG_AUTO_MIN_COVERAGE": 0.5,
            "RAG_AUTO_TOP_K": 3,
            "RAG_AUTO_MAX_CHARS": 1500,
        },
        "results": [r1.to_dict(), r2.to_dict()],
    }
    md_on = render_markdown(report_dict_on, cases=[])
    assert "- **RAG_AUTO**: on" in md_on
    assert f"- **Auto-retrieval**: {expected_headline}" in md_on

    report_dict_off = {
        "meta": {
            "mode": "mock",
            "provider": "mock-provider",
            "model": "test-model",
            "cases_version": 4,
            "RAG_AUTO": False,
        },
        "results": [r1.to_dict(), r2.to_dict()],
    }
    md_off = render_markdown(report_dict_off, cases=[])
    assert "- **RAG_AUTO**: off" in md_off
