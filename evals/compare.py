"""Comparison module for evaluating differences between two ORBIT evaluation runs."""

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Optional

from evals.metrics import wilson_interval


@dataclass
class RunSummary:
    """Holds parsed summary data for a single evaluation run."""

    stem: str
    meta: dict[str, Any]
    results: list[dict[str, Any]]
    case_ids: set[str]
    repeats: int
    categories: set[str]


def parse_run_data(data: dict[str, Any], stem: str) -> RunSummary:
    """Extract metadata, results, and case IDs from run dictionary."""
    meta = data.get("meta", {})
    results = data.get("results", [])
    case_ids = {r["id"] for r in results if "id" in r}
    repeats = int(meta.get("repeats", 1))
    categories = {r.get("category", "unknown") for r in results}
    return RunSummary(
        stem=stem,
        meta=meta,
        results=results,
        case_ids=case_ids,
        repeats=repeats,
        categories=categories,
    )


def check_identical_repeats(results: list[dict[str, Any]], repeats: int) -> bool:
    """Check if all repeated executions produced identical statuses for every case."""
    if repeats <= 1:
        return False

    grouped: dict[str, list[str]] = {}
    for r in results:
        cid = r.get("id")
        if cid:
            grouped.setdefault(cid, []).append(r.get("status", "unknown"))

    if not grouped:
        return False

    for statuses in grouped.values():
        if len(statuses) <= 1 or len(set(statuses)) > 1:
            return False

    return True


def _get_meta(meta: dict[str, Any], key: str, default: Any = "unknown") -> Any:
    """Retrieve metadata key defaulting to fallback if missing or None."""
    val = meta.get(key)
    return val if val is not None else default


def render_comparison_markdown(
    run_a: RunSummary,
    run_b: RunSummary,
) -> str:
    """Generate Markdown comparing metadata, per-case outcomes, and category totals."""
    lines: list[str] = []

    lines.append(f"# ORBIT Evaluation Comparison: `{run_a.stem}` vs `{run_b.stem}`")
    lines.append("")

    # 1. Metadata Comparison Table
    lines.append("## Metadata Comparison")
    lines.append("")
    lines.append(f"| Parameter | Run A (`{run_a.stem}`) | Run B (`{run_b.stem}`) | Diff |")
    lines.append("|---|---|---|---|")

    meta_params = [
        ("Provider", _get_meta(run_a.meta, "provider"), _get_meta(run_b.meta, "provider")),
        ("Model", _get_meta(run_a.meta, "model"), _get_meta(run_b.meta, "model")),
        ("Think", _get_meta(run_a.meta, "think"), _get_meta(run_b.meta, "think")),
        ("num_ctx", _get_meta(run_a.meta, "num_ctx"), _get_meta(run_b.meta, "num_ctx")),
        ("Temperature", _get_meta(run_a.meta, "temperature"), _get_meta(run_b.meta, "temperature")),
        ("Seed", _get_meta(run_a.meta, "seed"), _get_meta(run_b.meta, "seed")),
        ("Repeats", run_a.repeats, run_b.repeats),
        ("Case Count", len(run_a.case_ids), len(run_b.case_ids)),
        (
            "Date",
            run_a.meta.get("timestamp") or run_a.meta.get("date") or "unknown",
            run_b.meta.get("timestamp") or run_b.meta.get("date") or "unknown",
        ),
        (
            "cases_version",
            _get_meta(run_a.meta, "cases_version"),
            _get_meta(run_b.meta, "cases_version"),
        ),
        (
            "cases_sha256",
            _get_meta(run_a.meta, "cases_sha256"),
            _get_meta(run_b.meta, "cases_sha256"),
        ),
    ]

    for param, val_a, val_b in meta_params:
        diff_marker = "* (different)" if str(val_a) != str(val_b) else ""
        lines.append(f"| {param} | `{val_a}` | `{val_b}` | {diff_marker} |")
    lines.append("")

    # Warnings
    warnings: list[str] = []
    ver_a = run_a.meta.get("cases_version")
    ver_b = run_b.meta.get("cases_version")
    if str(ver_a) != str(ver_b) and (ver_a is not None or ver_b is not None):
        warnings.append(
            f"Warning: Case schema/definition version mismatch (cases_version differs: `{ver_a or 'missing'}` vs `{ver_b or 'missing'}`)."
        )

    sha_a = run_a.meta.get("cases_sha256")
    sha_b = run_b.meta.get("cases_sha256")
    if sha_a != sha_b and (sha_a or sha_b):
        warnings.append(
            f"Warning: Case definition hash mismatch (cases_sha256 differs: `{sha_a or 'missing'}` vs `{sha_b or 'missing'}`). Case files may differ."
        )

    if check_identical_repeats(run_a.results, run_a.repeats):
        warnings.append(
            f"Warning: Repeats were identical for every case in Run A (`{run_a.stem}`, repeats={run_a.repeats}) (then repeats add no information)."
        )
    if check_identical_repeats(run_b.results, run_b.repeats):
        warnings.append(
            f"Warning: Repeats were identical for every case in Run B (`{run_b.stem}`, repeats={run_b.repeats}) (then repeats add no information)."
        )

    if warnings:
        for w in warnings:
            lines.append(f"> **{w}**")
            lines.append("")

    # 2. Category & Overall Summary Table
    lines.append("## Summary by Category & Overall")
    lines.append("")
    lines.append(f"| Category | Run A (`{run_a.stem}`) | Run B (`{run_b.stem}`) |")
    lines.append("|---|---|---|")

    all_cats = sorted(run_a.categories | run_b.categories)
    for cat in all_cats:
        res_cat_a = [r for r in run_a.results if r.get("category") == cat and r.get("status") != "not_run"]
        res_cat_b = [r for r in run_b.results if r.get("category") == cat and r.get("status") != "not_run"]

        n_a = len(res_cat_a)
        p_a = sum(1 for r in res_cat_a if r.get("status") == "passed")
        rate_a = (p_a / n_a * 100.0) if n_a > 0 else 0.0
        low_a, high_a = wilson_interval(p_a, n_a)

        n_b = len(res_cat_b)
        p_b = sum(1 for r in res_cat_b if r.get("status") == "passed")
        rate_b = (p_b / n_b * 100.0) if n_b > 0 else 0.0
        low_b, high_b = wilson_interval(p_b, n_b)

        cell_a = f"{p_a}/{n_a} ({rate_a:.1f}% [{low_a*100:.1f}% - {high_a*100:.1f}%])"
        cell_b = f"{p_b}/{n_b} ({rate_b:.1f}% [{low_b*100:.1f}% - {high_b*100:.1f}%])"
        lines.append(f"| `{cat}` | {cell_a} | {cell_b} |")

    # Overall Row
    res_ov_a = [r for r in run_a.results if r.get("status") != "not_run"]
    res_ov_b = [r for r in run_b.results if r.get("status") != "not_run"]

    ov_n_a = len(res_ov_a)
    ov_p_a = sum(1 for r in res_ov_a if r.get("status") == "passed")
    ov_rate_a = (ov_p_a / ov_n_a * 100.0) if ov_n_a > 0 else 0.0
    ov_low_a, ov_high_a = wilson_interval(ov_p_a, ov_n_a)

    ov_n_b = len(res_ov_b)
    ov_p_b = sum(1 for r in res_ov_b if r.get("status") == "passed")
    ov_rate_b = (ov_p_b / ov_n_b * 100.0) if ov_n_b > 0 else 0.0
    ov_low_b, ov_high_b = wilson_interval(ov_p_b, ov_n_b)

    cell_ov_a = f"{ov_p_a}/{ov_n_a} ({ov_rate_a:.1f}% [{ov_low_a*100:.1f}% - {ov_high_a*100:.1f}%])"
    cell_ov_b = f"{ov_p_b}/{ov_n_b} ({ov_rate_b:.1f}% [{ov_low_b*100:.1f}% - {ov_high_b*100:.1f}%])"
    lines.append(f"| **Overall** | **{cell_ov_a}** | **{cell_ov_b}** |")
    lines.append("")
    lines.append("> Note: differences smaller than the intervals are not conclusive.")
    lines.append("")

    # 3. Per-case Table
    lines.append("## Per-Case Comparison")
    lines.append("")
    lines.append(f"| Case ID | Status in Run A (`{run_a.stem}`) | Status in Run B (`{run_b.stem}`) | Comparison |")
    lines.append("|---|---|---|---|")

    sorted_case_ids = sorted(run_a.case_ids)
    for cid in sorted_case_ids:
        c_res_a = [r for r in run_a.results if r.get("id") == cid]
        c_res_b = [r for r in run_b.results if r.get("id") == cid]

        n_a = len(c_res_a)
        p_a = sum(1 for r in c_res_a if r.get("status") == "passed")
        if n_a == 1 and run_a.repeats <= 1:
            maj_a = c_res_a[0].get("status", "failed")
            status_str_a = maj_a
        else:
            maj_a = "passed" if p_a > n_a / 2 else "failed"
            status_str_a = f"{maj_a} ({p_a}/{n_a})"

        n_b = len(c_res_b)
        p_b = sum(1 for r in c_res_b if r.get("status") == "passed")
        if n_b == 1 and run_b.repeats <= 1:
            maj_b = c_res_b[0].get("status", "failed")
            status_str_b = maj_b
        else:
            maj_b = "passed" if p_b > n_b / 2 else "failed"
            status_str_b = f"{maj_b} ({p_b}/{n_b})"

        if maj_a != "passed" and maj_b == "passed":
            change = "IMPROVED"
        elif maj_a == "passed" and maj_b != "passed":
            change = "REGRESSED"
        else:
            change = "SAME"

        lines.append(f"| `{cid}` | {status_str_a} | {status_str_b} | {change} |")

    lines.append("")
    return "\n".join(lines)


def compare_files(
    path_a: str | Path,
    path_b: str | Path,
    out_dir: Optional[str | Path] = None,
) -> tuple[int, str]:
    """Load two evaluation result files, compare them, print and save comparison report."""
    p_a = Path(path_a)
    p_b = Path(path_b)

    if not p_a.exists() and (Path("evals/results") / path_a).exists():
        p_a = Path("evals/results") / path_a

    if not p_b.exists() and (Path("evals/results") / path_b).exists():
        p_b = Path("evals/results") / path_b

    if not p_a.exists():
        msg = f"Error: File A not found: {p_a}"
        print(msg, file=sys.stderr)
        return 1, msg

    if not p_b.exists():
        msg = f"Error: File B not found: {p_b}"
        print(msg, file=sys.stderr)
        return 1, msg

    try:
        data_a = json.loads(p_a.read_text(encoding="utf-8"))
    except Exception as exc:
        msg = f"Error reading {p_a}: {exc}"
        print(msg, file=sys.stderr)
        return 1, msg

    try:
        data_b = json.loads(p_b.read_text(encoding="utf-8"))
    except Exception as exc:
        msg = f"Error reading {p_b}: {exc}"
        print(msg, file=sys.stderr)
        return 1, msg

    run_a = parse_run_data(data_a, p_a.stem)
    run_b = parse_run_data(data_b, p_b.stem)

    # Refusal check: same set of case IDs
    if run_a.case_ids != run_b.case_ids:
        only_in_a = sorted(run_a.case_ids - run_b.case_ids)
        only_in_b = sorted(run_b.case_ids - run_a.case_ids)
        err_lines = [
            f"Cannot compare runs: runs do not cover the same set of case IDs ({len(run_a.case_ids)} vs {len(run_b.case_ids)})."
        ]
        if only_in_a:
            err_lines.append(f"  Only in Run A: {only_in_a}")
        if only_in_b:
            err_lines.append(f"  Only in Run B: {only_in_b}")
        err_msg = "\n".join(err_lines)
        print(err_msg, file=sys.stderr)
        return 1, err_msg

    ver_a = run_a.meta.get("cases_version")
    ver_b = run_b.meta.get("cases_version")
    if str(ver_a) != str(ver_b) and (ver_a is not None or ver_b is not None):
        print(
            f"Warning: Case schema/definition version mismatch (cases_version differs: {ver_a or 'missing'} vs {ver_b or 'missing'}).",
            file=sys.stderr,
        )

    sha_a = run_a.meta.get("cases_sha256")
    sha_b = run_b.meta.get("cases_sha256")
    if sha_a != sha_b and (sha_a or sha_b):
        print(
            f"Warning: Case definition hash mismatch (cases_sha256 differs: {sha_a or 'missing'} vs {sha_b or 'missing'}).",
            file=sys.stderr,
        )

    report_md = render_comparison_markdown(run_a, run_b)

    # Determine output path
    dest_dir = Path(out_dir) if out_dir else Path("evals/results")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_file = dest_dir / f"compare-{run_a.stem}-vs-{run_b.stem}.md"
    out_file.write_text(report_md, encoding="utf-8")

    # Print to stdout
    print(report_md)
    print(f"\nComparison report written to: {out_file}")

    return 0, report_md
