"""Evaluation markdown report generation."""

from datetime import datetime, timezone
import subprocess
import sys
from typing import Any, Mapping, Optional, Sequence, Union

from core.version import VERSION
from evals.checks import classify_check_failure
from evals.metrics import CaseResult, aggregate
from evals.schema import EvalCase


def _get_git_commit() -> str:
    """Retrieve the current git short commit hash, safely falling back to 'unknown'."""
    try:
        res = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return res.strip() or "unknown"
    except Exception:
        return "unknown"


def _format_metric_pct(val: Optional[float]) -> str:
    """Format a floating proportion as percentage or return 'n/a (no cases in this run)' if None."""
    if val is None:
        return "n/a (no cases in this run)"
    return f"{val * 100:.1f}%"


def render_markdown(
    results_dict: dict[str, Any],
    cases: Union[Sequence[EvalCase], Mapping[str, EvalCase]],
) -> str:
    """Render a comprehensive evaluation report in GitHub-flavored markdown."""
    meta = results_dict.get("meta", {})
    results_data = results_dict.get("results", [])
    results = [CaseResult.from_dict(r) for r in results_data]

    mode = str(meta.get("mode", "mock")).lower()
    provider = str(meta.get("provider", "")).lower()
    model_name = str(meta.get("model", "mock-provider"))
    timestamp = meta.get("timestamp", datetime.now(timezone.utc).isoformat())
    repeats = int(meta.get("repeats", 1))

    gpu_name = meta.get("gpu_name")
    total_vram = meta.get("total_vram") or meta.get("gpu_vram")
    if meta.get("gpu"):
        gpu_str = str(meta.get("gpu"))
    elif gpu_name and total_vram and gpu_name != "unknown":
        gpu_str = f"{gpu_name} ({total_vram})"
    elif gpu_name:
        gpu_str = str(gpu_name)
    else:
        gpu_str = "unknown"

    num_ctx = meta.get("num_ctx")
    temperature = meta.get("temperature")
    seed = meta.get("seed")

    case_list = list(cases.values()) if isinstance(cases, Mapping) else list(cases)
    total_planned = len(case_list) * repeats
    cases_run = sum(1 for r in results if r.status != "not_run")
    git_commit = _get_git_commit()
    py_version = sys.version.split()[0]

    agg = aggregate(results, case_list)
    overall = agg["overall"]
    by_category = agg["by_category"]

    lines: list[str] = []

    is_holdout = bool(meta.get("holdout", False))
    total_cases = meta.get("total_cases")
    is_partial = bool(
        meta.get("is_partial")
        or meta.get("partial")
        or (total_cases is not None and total_cases > len(case_list))
    )

    think = meta.get("think")
    case_count = len(case_list) or meta.get("case_count") or meta.get("total_cases") or len({r.id for r in results})

    repeat_mode = meta.get("repeat_mode")
    base_seed = meta.get("base_seed")

    # Title & Metadata Header
    lines.append(f"# ORBIT v{VERSION} Evaluation Report")
    lines.append("")
    lines.append(f"- **Version**: {VERSION}")
    lines.append(f"- **Mode**: {mode.capitalize()}")
    rag_auto_val = meta.get("RAG_AUTO")
    if rag_auto_val is not None:
        if isinstance(rag_auto_val, str):
            rag_auto_str = "on" if rag_auto_val.strip().lower() in ("1", "true", "yes", "on") else "off"
        else:
            rag_auto_str = "on" if bool(rag_auto_val) else "off"
    else:
        from config import settings
        rag_auto_str = "on" if getattr(settings, "RAG_AUTO", True) else "off"
    lines.append(f"- **RAG_AUTO**: {rag_auto_str}")
    if is_holdout:
        lines.append("- **Dataset**: Holdout")
    if provider == "ollama":
        lines.append("- **Provider**: local (no data left this machine)")
    elif provider:
        lines.append(f"- **Provider**: {provider.capitalize()}")
    lines.append(f"- **Model**: `{model_name}`")
    lines.append(f"- **GPU**: `{gpu_str}`")
    if think is not None:
        lines.append(f"- **Think**: `{think}`")
    if num_ctx is not None:
        lines.append(f"- **Context Size**: `{num_ctx}`")
    if repeat_mode is not None:
        lines.append(f"- **Repeat Mode**: `{repeat_mode}`")
    if temperature is not None:
        lines.append(f"- **Temperature**: `{temperature}`")
    if base_seed is not None:
        lines.append(f"- **Base Seed**: `{base_seed}`")
    if seed is not None:
        lines.append(f"- **Seed**: `{seed}`")
    lines.append(f"- **Repeats**: {repeats}")
    lines.append(f"- **Case Count**: {case_count}")
    lines.append(f"- **Date**: {timestamp}")
    lines.append(f"- **Commit**: `{git_commit}`")
    lines.append(f"- **Python**: `{py_version}`")
    lines.append(f"- **Runs Evaluated**: {cases_run} / {total_planned} (Repeats: {repeats})")
    lines.append("")

    # Holdout / Partial / Mock Run Banners
    if is_holdout:
        lines.append("> **HOLDOUT RUN: unobserved evaluation suite, do not optimize for these cases**")
        lines.append("")
    if is_partial:
        total_str = total_cases if total_cases is not None else len(case_list)
        cats_str = ", ".join(sorted({c.category for c in case_list}))
        lines.append(f"> **PARTIAL RUN: {len(case_list)} of {total_str} cases, categories: {cats_str}**")
        lines.append("")
    if mode == "mock":
        lines.append("> **MOCK RUN: harness self-test, NOT model performance**")
        lines.append("")

    # Note if repeats run at temperature 0 with a fixed seed
    eff_repeat_mode = repeat_mode or "same"
    eff_temp = float(temperature) if temperature is not None else 0.0
    if repeats > 1 and eff_temp == 0.0 and eff_repeat_mode == "same":
        lines.append("> Note: repeats are identical; use --repeat-mode seeds to measure variation")
        lines.append("")

    # Headline Metrics
    lines.append("## Headline Metrics")
    lines.append("")
    pass_pct = overall["pass_rate"] * 100
    low_pct = overall["wilson_low"] * 100
    high_pct = overall["wilson_high"] * 100
    outcome_pct = overall.get("outcome_pass_rate", overall["pass_rate"]) * 100
    outcome_low_pct = overall.get("outcome_wilson_low", overall["wilson_low"]) * 100
    outcome_high_pct = overall.get("outcome_wilson_high", overall["wilson_high"]) * 100
    lines.append(f"- **Task success (all checks)**: {pass_pct:.1f}% [{low_pct:.1f}% - {high_pct:.1f}%]")
    lines.append(f"- **Outcome success (language checks not counted)**: {outcome_pct:.1f}% [{outcome_low_pct:.1f}% - {outcome_high_pct:.1f}%]")
    lines.append(f"- **Refusals clearly worded**: {overall.get('refusals_clearly_worded_headline', 'n/a')}")
    lines.append(f"- **Auto-retrieval**: {overall.get('auto_retrieval_headline', '0 of 0 turns searched, 0 injected, 0 not-found notes')}")
    lines.append(f"- **Tool Selection Accuracy**: {_format_metric_pct(overall.get('tool_selection_accuracy'))}")
    lines.append(f"- **Unnecessary Tool Use Rate**: {_format_metric_pct(overall.get('unnecessary_tool_rate'))}")
    lines.append(f"- **Safety Pass Rate**: {_format_metric_pct(overall.get('safety_pass_rate'))}")
    lines.append(f"- **Unverified action claims**: {overall.get('unverified_claims_headline', 'n/a')}")
    lines.append(f"- **Average Tool Calls / Task**: {overall['avg_tool_calls']:.2f}")
    lines.append(f"- **Average LLM Calls / Task**: {overall['avg_llm_calls']:.2f}")
    lines.append(f"- **p50 Latency**: {overall['p50_latency']:.2f}s")
    lines.append(f"- **p95 Latency**: {overall['p95_latency']:.2f}s")
    if overall["not_run"] > 0:
        lines.append(f"- **Not Run**: {overall['not_run']}")
    lines.append("")

    # Category Table
    lines.append("## Results by Category")
    lines.append("")
    lines.append("| Category | Cases | Passed | Rate | 95% Wilson Interval |")
    lines.append("|---|---|---|---|---|")

    for cat, c_stats in sorted(by_category.items()):
        c_rate = c_stats["pass_rate"] * 100
        c_low = c_stats["wilson_low"] * 100
        c_high = c_stats["wilson_high"] * 100
        interval_str = f"[{c_low:.1f}% - {c_high:.1f}%]"
        lines.append(
            f"| `{cat}` | {c_stats['n']} | {c_stats['passed']} | {c_rate:.1f}% | {interval_str} |"
        )
    lines.append("")

    # Latency Notes
    p50 = overall.get("p50_latency", 0.0)
    slow_cases = [r for r in results if r.status != "not_run" and p50 > 0 and r.latency_s > 3.0 * p50]
    if slow_cases:
        lines.append("## Latency Notes")
        lines.append("")
        for r in slow_cases:
            repeat_tag = f" (repeat {r.repeat})" if repeats > 1 else ""
            lines.append(
                f"- Case `{r.id}`{repeat_tag}: {r.latency_s:.2f}s is slower than 3x the median ({p50:.2f}s)"
            )
        lines.append("")

    # Failures Section
    lines.append("## Failures")
    lines.append("")
    failures = [r for r in results if r.status not in ("passed", "not_run")]
    if not failures:
        lines.append("No failures recorded. All evaluated cases passed!")
    else:
        # Refusal cases summary: count outcome vs language-only failures among refusal/denial cases
        from evals.schema import lookup_case_kind

        case_lookup = {c.id: c for c in case_list}

        refusal_outcome_failures = 0
        refusal_language_failures = 0

        for r in failures:
            case_kind = lookup_case_kind(r.id, case_lookup.get(r.id))
            if case_kind in ("refusal", "denial"):
                check_classes = [
                    classify_check_failure(fc) for fc in r.failed_checks
                ]
                if any(c == "outcome" for c in check_classes):
                    refusal_outcome_failures += 1
                elif any(c == "language" for c in check_classes):
                    refusal_language_failures += 1

        refusal_failures_headline = (
            f"Refusal-case failures: {refusal_outcome_failures} outcome, "
            f"{refusal_language_failures} language-only"
        )
        lines.append(f"**{refusal_failures_headline}**")
        lines.append("")

        for r in failures:
            repeat_tag = f" (repeat {r.repeat})" if repeats > 1 else ""
            status_display = (r.status or "unknown").upper()
            lines.append(f"### `{r.id}`{repeat_tag} - `{status_display}`")
            lines.append(f"- **Category**: `{r.category or 'unknown'}`")
            if r.tools_called:
                tool_seq = ", ".join(f"`{t}`" for t in r.tools_called)
                lines.append(f"- **Tool Sequence**: {tool_seq}")
            else:
                lines.append("- **Tool Sequence**: *(none)*")

            if r.failed_checks:
                lines.append("- **Failed Checks**:")
                for fc in r.failed_checks:
                    kind = classify_check_failure(fc)
                    lines.append(f"  - [{kind}] {fc}")

            if r.error:
                lines.append(f"- **Error Message**: `{r.error}`")
            lines.append("")

    return "\n".join(lines)
