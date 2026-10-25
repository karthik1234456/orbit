"""Evaluation metrics calculation, Wilson confidence intervals, and result aggregation."""

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Mapping, Optional, Sequence, Union


@dataclass
class CaseResult:
    """Outcome and execution metrics for a single evaluation case attempt."""

    id: str
    category: str
    repeat: int
    status: str  # "passed", "failed", "error", or "not_run"
    failed_checks: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    tools_executed: list[str] = field(default_factory=list)
    llm_calls: int = 0
    latency_s: float = 0.0
    run_status: Optional[str] = None
    final_text: str = ""
    error: Optional[str] = None
    unverified_claims: int = 0

    def __post_init__(self) -> None:
        if self.final_text:
            self.final_text = str(self.final_text)[:500]

    def to_dict(self) -> dict[str, Any]:
        """Convert result to a JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseResult":
        """Instantiate CaseResult from a dictionary, ensuring string truncation."""
        d = dict(data)
        if "final_text" in d and d["final_text"] is not None:
            d["final_text"] = str(d["final_text"])[:500]
        return cls(**d)


def wilson_interval(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Compute the Wilson score confidence interval for a binomial proportion.

    Returns (0.0, 0.0) when total is 0.
    """
    if total <= 0:
        return (0.0, 0.0)

    p = passed / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    radicand = (p * (1.0 - p)) / total + z2 / (4.0 * total * total)
    margin = z * math.sqrt(max(0.0, radicand))

    low = max(0.0, (center - margin) / denom)
    high = min(1.0, (center + margin) / denom)
    return (low, high)


def percentile(values: Sequence[float], p: float) -> float:
    """Calculate the p-th percentile of values using linear interpolation.

    Accepts p either as a percentage [0, 100] or fraction [0, 1].
    Returns 0.0 for an empty sequence.
    """
    valid_vals = [v for v in values if v is not None]
    if not valid_vals:
        return 0.0

    sorted_vals = sorted(valid_vals)
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])

    frac = p / 100.0 if p > 1.0 else p
    frac = max(0.0, min(1.0, frac))

    idx = (n - 1) * frac
    low = int(math.floor(idx))
    high = int(math.ceil(idx))

    if low == high:
        return float(sorted_vals[low])

    weight = idx - low
    return float(sorted_vals[low] + weight * (sorted_vals[high] - sorted_vals[low]))


def _compute_group_stats(results: list[CaseResult]) -> dict[str, Any]:
    """Compute summary statistics for a collection of evaluated results."""
    n = len(results)
    if n == 0:
        return {
            "n": 0,
            "passed": 0,
            "pass_rate": 0.0,
            "wilson_low": 0.0,
            "wilson_high": 0.0,
            "errors": 0,
            "avg_tool_calls": 0.0,
            "avg_llm_calls": 0.0,
            "p50_latency": 0.0,
            "p95_latency": 0.0,
        }

    passed_count = sum(1 for r in results if r.status == "passed")
    error_count = sum(1 for r in results if r.status == "error")
    pass_rate = passed_count / n
    w_low, w_high = wilson_interval(passed_count, n)

    total_tool_calls = sum(len(r.tools_called) for r in results if r.tools_called is not None)
    total_llm_calls = sum(r.llm_calls for r in results if r.llm_calls is not None)
    latencies = [r.latency_s for r in results if r.latency_s is not None]

    return {
        "n": n,
        "passed": passed_count,
        "pass_rate": pass_rate,
        "wilson_low": w_low,
        "wilson_high": w_high,
        "errors": error_count,
        "avg_tool_calls": total_tool_calls / n,
        "avg_llm_calls": total_llm_calls / n,
        "p50_latency": percentile(latencies, 50),
        "p95_latency": percentile(latencies, 95),
    }


def aggregate(
    results: Sequence[CaseResult],
    cases: Union[Sequence[Any], Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate evaluation run results into overall and by-category metrics.

    Excludes results with status 'not_run' from all metric denominators.
    """
    if isinstance(cases, Mapping):
        case_map = dict(cases)
    else:
        case_map = {getattr(c, "id", str(idx)): c for idx, c in enumerate(cases)}

    evaluated_results: list[CaseResult] = []
    not_run_count = 0

    for r in results:
        if r.status == "not_run":
            not_run_count += 1
        else:
            evaluated_results.append(r)

    # 1. Overall base statistics
    overall_stats = _compute_group_stats(evaluated_results)

    # 2. Tool selection accuracy (among cases with non-empty expect_tools)
    expect_tools_evaluated = 0
    tools_selected_correctly = 0

    for r in evaluated_results:
        case_obj = case_map.get(r.id)
        if case_obj is not None:
            expected = getattr(case_obj, "expect_tools", [])
            if expected:
                expect_tools_evaluated += 1
                called_set = {t.lower() for t in r.tools_called}
                if all(exp.lower() in called_set for exp in expected):
                    tools_selected_correctly += 1

    tool_selection_accuracy = (
        (tools_selected_correctly / expect_tools_evaluated)
        if expect_tools_evaluated > 0
        else None
    )

    # 3. Unnecessary tool rate (among chat-category cases)
    chat_results = [r for r in evaluated_results if r.category == "chat"]
    if chat_results:
        chat_with_tools = sum(1 for r in chat_results if len(r.tools_called) > 0)
        unnecessary_tool_rate = chat_with_tools / len(chat_results)
    else:
        unnecessary_tool_rate = None

    # 4. Safety pass rate (pass rate of the safety category)
    safety_results = [r for r in evaluated_results if r.category == "safety"]
    if safety_results:
        safety_passed = sum(1 for r in safety_results if r.status == "passed")
        safety_pass_rate = safety_passed / len(safety_results)
    else:
        safety_pass_rate = None

    # 5. Unverified action claims
    if evaluated_results:
        claims_run_count = sum(
            1 for r in evaluated_results if getattr(r, "unverified_claims", 0) > 0
        )
        total_eval_runs = len(evaluated_results)
        unverified_claims_headline = f"{claims_run_count} of {total_eval_runs} runs"
    else:
        unverified_claims_headline = "n/a"

    # 6. Outcome success and refusals clearly worded
    from evals.checks import classify_check_failure
    from evals.schema import lookup_case_kind

    outcome_passed_count = sum(
        1
        for r in evaluated_results
        if r.status != "error"
        and not any(classify_check_failure(fc) == "outcome" for fc in r.failed_checks)
    )
    outcome_pass_rate = outcome_passed_count / len(evaluated_results) if evaluated_results else 0.0
    outcome_low, outcome_high = wilson_interval(outcome_passed_count, len(evaluated_results))

    refusal_results = [
        r
        for r in evaluated_results
        if lookup_case_kind(r.id, case_map.get(r.id)) in ("refusal", "denial")
    ]
    refusal_cases_count = len(refusal_results)
    refusals_clearly_worded_count = sum(
        1
        for r in refusal_results
        if r.status != "error"
        and not any(
            fc.startswith("refusal_language")
            or classify_check_failure(fc) == "language"
            for fc in r.failed_checks
        )
    )

    overall_stats["outcome_passed"] = outcome_passed_count
    overall_stats["outcome_pass_rate"] = outcome_pass_rate
    overall_stats["outcome_wilson_low"] = outcome_low
    overall_stats["outcome_wilson_high"] = outcome_high
    overall_stats["refusals_clearly_worded_count"] = refusals_clearly_worded_count
    overall_stats["refusal_cases_count"] = refusal_cases_count
    overall_stats["refusals_clearly_worded_headline"] = (
        f"{refusals_clearly_worded_count} of {refusal_cases_count}"
        if refusal_cases_count > 0
        else "n/a"
    )

    overall_stats["tool_selection_accuracy"] = tool_selection_accuracy
    overall_stats["unnecessary_tool_rate"] = unnecessary_tool_rate
    overall_stats["safety_pass_rate"] = safety_pass_rate
    overall_stats["unverified_claims_headline"] = unverified_claims_headline
    overall_stats["not_run"] = not_run_count

    # 5. Group by category
    by_category: dict[str, dict[str, Any]] = {}
    distinct_categories = sorted({r.category for r in evaluated_results})

    for cat in distinct_categories:
        cat_results = [r for r in evaluated_results if r.category == cat]
        by_category[cat] = _compute_group_stats(cat_results)

    return {
        "overall": overall_stats,
        "by_category": by_category,
    }
