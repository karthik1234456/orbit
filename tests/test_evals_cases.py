"""Tests verifying the evaluation case dataset and running benchmark suites."""

from pathlib import Path
import pytest

from evals.providers import GoldenProvider, NoopProvider
from evals.runner import run_case
from evals.schema import EvalCase, load_cases

POSITIVE_CHECK_TYPES = {
    "final_contains_any",
    "refusal_language",
    "tool_called",
    "tool_executed",
    "file_exists",
    "file_contains",
    "launched",
    "memory_contains",
}

MIN_CATEGORY_COUNTS = {
    "chat": 3,
    "utility": 4,
    "files": 6,
    "apps": 4,
    "memory": 4,
    "rag": 3,
    "safety": 5,
    "multistep": 4,
    "recovery": 2,
}


@pytest.fixture(scope="module")
def all_cases() -> list[EvalCase]:
    """Load all evaluation cases from the evals/cases directory."""
    cases_dir = Path("evals/cases")
    assert cases_dir.is_dir(), f"Expected directory '{cases_dir}' to exist"
    return load_cases(cases_dir)


def test_cases_load_and_meet_counts(all_cases: list[EvalCase]):
    """Verify that all cases load cleanly, meet minimum counts, and have unique IDs."""
    assert len(all_cases) >= 40, f"Expected at least 40 cases, got {len(all_cases)}"

    # Check unique IDs
    ids = [c.id for c in all_cases]
    assert len(ids) == len(set(ids)), f"Duplicate case IDs found: {ids}"

    # Verify per-category minimum counts
    counts: dict[str, int] = {}
    for c in all_cases:
        counts[c.category] = counts.get(c.category, 0) + 1

    for cat, min_count in MIN_CATEGORY_COUNTS.items():
        actual = counts.get(cat, 0)
        assert actual >= min_count, f"Category '{cat}' has {actual} cases, expected at least {min_count}"

    # Verify every case has at least one check
    for c in all_cases:
        assert len(c.checks) >= 1, f"Case '{c.id}' has no checks configured"

    # Verify every safety case has at least one positive check
    safety_cases = [c for c in all_cases if c.category == "safety"]
    assert len(safety_cases) >= MIN_CATEGORY_COUNTS["safety"]

    for sc in safety_cases:
        has_positive = any(chk.get("type") in POSITIVE_CHECK_TYPES for chk in sc.checks)
        assert has_positive, f"Safety case '{sc.id}' does not have any positive checks"


def test_all_cases_pass_with_golden_provider(all_cases: list[EvalCase]):
    """Verify that running every case with GoldenProvider passes 100%."""
    failures: list[tuple[str, list[str], str | None]] = []

    for case in all_cases:
        result = run_case(case, lambda c: GoldenProvider(c))
        if result.status != "passed":
            failures.append((case.id, result.failed_checks, result.error))

    assert len(failures) == 0, (
        f"{len(failures)} case(s) failed with GoldenProvider:\n"
        + "\n".join(f"  - {fid}: {fchecks} (error: {err})" for fid, fchecks, err in failures)
    )


def test_noop_provider_fails_at_least_60_percent(all_cases: list[EvalCase]):
    """Verify that NoopProvider fails at least 60% of evaluation cases."""
    non_passed_count = 0

    for case in all_cases:
        result = run_case(case, lambda c: NoopProvider())
        if result.status != "passed":
            non_passed_count += 1

    fail_rate = non_passed_count / len(all_cases)
    assert fail_rate >= 0.60, f"Expected NoopProvider to fail >= 60% of cases, failed {fail_rate * 100:.1f}% ({non_passed_count}/{len(all_cases)})"


def test_memory_refusal_cases_pass_golden_and_fail_noop(all_cases: list[EvalCase]):
    """Verify memory_refuse_password and memory_refuse_instruction pass with GoldenProvider and fail with NoopProvider."""
    target_ids = {"memory_refuse_password", "memory_refuse_instruction"}
    cases = [c for c in all_cases if c.id in target_ids]
    assert len(cases) == 2, f"Expected 2 cases, found: {[c.id for c in cases]}"

    for case in cases:
        # GoldenProvider (mock mode) passes
        golden_res = run_case(case, lambda c: GoldenProvider(c))
        assert golden_res.status == "passed", (
            f"Case {case.id} failed with GoldenProvider: {golden_res.failed_checks}"
        )

        # NoopProvider fails
        noop_res = run_case(case, lambda c: NoopProvider())
        assert noop_res.status != "passed", (
            f"Case {case.id} unexpectedly passed with NoopProvider"
        )


HOLDOUT_CATEGORY_COUNTS = {
    "memory": 3,
    "rag": 2,
    "files": 2,
    "safety": 2,
    "multistep": 2,
    "utility": 1,
}


@pytest.fixture(scope="module")
def holdout_cases() -> list[EvalCase]:
    """Load all evaluation cases from the evals/holdout directory."""
    holdout_dir = Path("evals/holdout")
    assert holdout_dir.is_dir(), f"Expected directory '{holdout_dir}' to exist"
    return load_cases(holdout_dir)


def test_holdout_cases_load_and_meet_counts(holdout_cases: list[EvalCase]):
    """Verify that all 12 holdout cases load cleanly, meet category counts, and have unique IDs."""
    assert len(holdout_cases) == 12, f"Expected exactly 12 holdout cases, got {len(holdout_cases)}"

    ids = [c.id for c in holdout_cases]
    assert len(ids) == len(set(ids)), f"Duplicate holdout case IDs found: {ids}"

    counts: dict[str, int] = {}
    for c in holdout_cases:
        counts[c.category] = counts.get(c.category, 0) + 1

    for cat, expected in HOLDOUT_CATEGORY_COUNTS.items():
        actual = counts.get(cat, 0)
        assert actual == expected, f"Holdout category '{cat}' has {actual} cases, expected {expected}"

    for c in holdout_cases:
        assert len(c.checks) >= 1, f"Holdout case '{c.id}' has no checks configured"


def test_holdout_safety_cases_check_outcomes(holdout_cases: list[EvalCase]):
    """Verify safety holdout cases check safe outcomes, not merely dangerous tool calls."""
    safety_cases = [c for c in holdout_cases if c.category == "safety"]
    assert len(safety_cases) == 2

    outcome_types = {
        "no_escape",
        "nothing_launched",
        "file_absent",
        "tool_not_executed",
        "confirm_requests_warned",
        "final_contains_any",
    }
    for sc in safety_cases:
        types_used = {chk["type"] for chk in sc.checks}
        assert types_used.intersection(outcome_types), (
            f"Holdout safety case '{sc.id}' must check safe outcomes"
        )
        assert "tool_called" not in types_used, (
            f"Holdout safety case '{sc.id}' should not merely check tool_called for dangerous tools"
        )


def test_all_holdout_cases_pass_with_golden_provider(holdout_cases: list[EvalCase]):
    """Verify that every holdout case passes 100% with GoldenProvider."""
    failures: list[tuple[str, list[str], str | None]] = []

    for case in holdout_cases:
        result = run_case(case, lambda c: GoldenProvider(c))
        if result.status != "passed":
            failures.append((case.id, result.failed_checks, result.error))

    assert len(failures) == 0, (
        f"{len(failures)} holdout case(s) failed with GoldenProvider:\n"
        + "\n".join(f"  - {fid}: {fchecks} (error: {err})" for fid, fchecks, err in failures)
    )


def test_all_holdout_cases_fail_with_noop_provider(holdout_cases: list[EvalCase]):
    """Verify that every holdout case fails when evaluated with NoopProvider."""
    passed_cases: list[str] = []

    for case in holdout_cases:
        result = run_case(case, lambda c: NoopProvider())
        if result.status == "passed":
            passed_cases.append(case.id)

    assert len(passed_cases) == 0, (
        f"Expected NoopProvider to fail all holdout cases, but passed: {passed_cases}"
    )
