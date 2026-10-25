"""Tests for evaluation suite v5: NOT_FOUND_WORDS, not_found_language check, and unanswerable RAG cases."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import pytest

from core.llm import LLMProvider, LLMResponse
from evals.checks import (
    CHECK_TYPES,
    LANGUAGE_CHECK_TYPES,
    NOT_FOUND_WORDS,
    RunObservation,
    ToolEvent,
    classify_check,
    classify_check_failure,
    run_checks,
)
from evals.providers import GoldenProvider, NoopProvider
from evals.runner import run_case, run_suite
from evals.schema import CHECK_ALLOWED_KEYS, CaseError, _validate_and_parse_case, load_cases


def test_schema_validates_not_found_language():
    """not_found_language is registered in checks and schema validation."""
    assert "not_found_language" in CHECK_TYPES
    assert "not_found_language" in LANGUAGE_CHECK_TYPES
    assert "not_found_language" in CHECK_ALLOWED_KEYS
    assert classify_check({"type": "not_found_language"}) == "language"

    # Valid check definition
    valid_raw = {
        "id": "test_not_found_case",
        "category": "rag",
        "turns": ["Where is the secret base?"],
        "checks": [{"type": "not_found_language"}],
    }
    case = _validate_and_parse_case(valid_raw, set())
    assert case.checks == [{"type": "not_found_language"}]

    # Unknown extra key rejected
    invalid_raw = {
        "id": "test_not_found_invalid",
        "category": "rag",
        "turns": ["Where is the secret base?"],
        "checks": [{"type": "not_found_language", "bad_key": 123}],
    }
    with pytest.raises(CaseError, match="unknown extra key"):
        _validate_and_parse_case(invalid_raw, set())


def test_not_found_language_passes_for_each_phrase(tmp_path: Path):
    """not_found_language passes for every phrase in NOT_FOUND_WORDS (case-insensitive)."""
    assert len(NOT_FOUND_WORDS) == 20
    case = SimpleNamespace(checks=[{"type": "not_found_language"}])

    for phrase in NOT_FOUND_WORDS:
        # Lowercase in sentence
        obs_lower = RunObservation(
            final_texts=[f"I checked the documentation and it {phrase} regarding that."],
            events=[],
            launches=[],
            llm_calls=1,
            latency_s=0.1,
            workspace=tmp_path,
        )
        failures = run_checks(case, obs_lower)
        assert failures == [], f"Expected phrase '{phrase}' to pass, got failures: {failures}"

        # Uppercase / mixed case
        obs_upper = RunObservation(
            final_texts=[f"THE TOPIC IS {phrase.upper()} IN YOUR DOCUMENTATION."],
            events=[],
            launches=[],
            llm_calls=1,
            latency_s=0.1,
            workspace=tmp_path,
        )
        failures_upper = run_checks(case, obs_upper)
        assert failures_upper == [], f"Expected uppercase phrase '{phrase.upper()}' to pass, got: {failures_upper}"


def test_not_found_language_fails_for_invented_answer(tmp_path: Path):
    """not_found_language fails when the response invents an answer without absence phrasing."""
    case = SimpleNamespace(checks=[{"type": "not_found_language"}])

    invented_answers = [
        "The lead mobile designer is Bob Smith.",
        "The travel and lodging budget is $5,000 for the conference.",
        "Engineering workstations use Intel Xeon processors.",
        "Here is the secret document: classified payload.",
        "Done.",
    ]

    for answer in invented_answers:
        obs = RunObservation(
            final_texts=[answer],
            events=[],
            launches=[],
            llm_calls=1,
            latency_s=0.1,
            workspace=tmp_path,
        )
        failures = run_checks(case, obs)
        assert len(failures) == 1
        assert "not_found_language" in failures[0]
        assert classify_check_failure(failures[0]) == "language"


def test_documents_consulted_semantics_unchanged(tmp_path: Path):
    """documents_consulted continues to pass via tool call or auto-retrieval, and fail on neither."""
    case = SimpleNamespace(checks=[{"type": "documents_consulted"}])

    # 1. Passes with successful tool call
    obs_tool = RunObservation(
        final_texts=["Document content returned."],
        events=[
            ToolEvent(tool="search_documents", args={"query": "test"}, status="success", turn=0),
        ],
        launches=[],
        llm_calls=1,
        latency_s=0.2,
        workspace=tmp_path,
    )
    assert run_checks(case, obs_tool) == []

    # 2. Passes with auto-retrieval injected > 0
    obs_ar_injected = RunObservation(
        final_texts=["Document content returned."],
        events=[],
        launches=[],
        llm_calls=1,
        latency_s=0.2,
        workspace=tmp_path,
        auto_retrievals=[{"searched": True, "injected": 2, "note": ""}],
    )
    assert run_checks(case, obs_ar_injected) == []

    # 3. Passes with auto-retrieval note
    obs_ar_note = RunObservation(
        final_texts=["Document content returned."],
        events=[],
        launches=[],
        llm_calls=1,
        latency_s=0.2,
        workspace=tmp_path,
        auto_retrievals=[{"searched": True, "injected": 0, "note": "Not found in documents."}],
    )
    assert run_checks(case, obs_ar_note) == []

    # 4. Fails when neither occurred
    obs_none = RunObservation(
        final_texts=["Hallucinated answer."],
        events=[],
        launches=[],
        llm_calls=1,
        latency_s=0.2,
        workspace=tmp_path,
        auto_retrievals=[],
    )
    failures_none = run_checks(case, obs_none)
    assert len(failures_none) == 1
    assert "documents_consulted" in failures_none[0]


class UnsafeInventingProvider(LLMProvider):
    """Provider that invents answers instead of acknowledging document absence."""

    def generate(self, messages: list[dict], tools: Optional[list[Any]] = None, **kwargs) -> LLMResponse:
        return LLMResponse(
            text="The lead mobile designer is Bob and Sarah, and our conference budget is $5,000."
        )


UNANSWERABLE_CASE_PATHS = [
    ("evals/cases/rag.json", "rag_unanswered_question"),
    ("evals/holdout/rag.json", "holdout_rag_unindexed_topic_fallback"),
    ("evals/holdout2/cases.json", "holdout2_rag_unindexed_topic"),
    ("evals/holdout3/cases.json", "holdout3_rag_unanswerable_conference_budget"),
    ("evals/holdout3/cases.json", "holdout3_rag_unanswerable_lead_architect"),
]


def test_unsafe_provider_invents_answer_fails_every_unanswerable_case():
    """An unsafe provider that invents answers fails every unanswerable case across all suites."""
    for file_path, case_id in UNANSWERABLE_CASE_PATHS:
        cases = load_cases(file_path)
        matching = [c for c in cases if c.id == case_id]
        assert len(matching) == 1, f"Case {case_id} not found in {file_path}"
        case = matching[0]

        result = run_case(
            case=case,
            provider_factory=lambda c: UnsafeInventingProvider(),
            repeat=0,
        )
        assert result.status == "failed", f"Case {case_id} was expected to fail with UnsafeInventingProvider"
        assert len(result.failed_checks) > 0


def test_noop_provider_fails_every_unanswerable_case():
    """NoopProvider returns 'Done.' without tool calls, failing every unanswerable case."""
    for file_path, case_id in UNANSWERABLE_CASE_PATHS:
        cases = load_cases(file_path)
        matching = [c for c in cases if c.id == case_id]
        assert len(matching) == 1, f"Case {case_id} not found in {file_path}"
        case = matching[0]

        result = run_case(
            case=case,
            provider_factory=lambda c: NoopProvider(),
            repeat=0,
        )
        assert result.status == "failed", f"Case {case_id} was expected to fail with NoopProvider"
        assert len(result.failed_checks) > 0


@pytest.mark.parametrize(
    "dataset_dir",
    [
        "evals/cases",
        "evals/holdout",
        "evals/holdout2",
        "evals/holdout3",
    ],
)
def test_mock_eval_100_percent_for_all_directories(dataset_dir: str, tmp_path: Path):
    """Verify that mock evaluations with GoldenProvider achieve 100% pass rate in all four directories."""
    cases = load_cases(dataset_dir)
    assert len(cases) > 0

    results, quota_hit = run_suite(
        cases=cases,
        provider_factory=lambda c: GoldenProvider(c),
        out_path=tmp_path / "mock_eval_results.json",
        repeats=1,
    )
    assert not quota_hit
    for r in results:
        assert r.status == "passed", f"Case {r.id} failed in {dataset_dir}: {r.failed_checks}"
