"""Command evaluation benchmark harness for ORBIT desktop and browser shortcuts/chains."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Mapping, Optional, Union

from config.apps import ALLOWED_APPS
from config.sites import SITES, resolve_site
from core.chains import ShortcutPlan, has_explicit_sequencing_separator, parse_chain
from core.session_state import SessionState, _extract_site_key
from core.shortcuts import ToolCallSpec, parse_shortcut

# =============================================================================
# Benchmark Vocabulary Specifications
# =============================================================================
ALLOWED_TOOLS = {
    "open_application",
    "close_application",
    "open_website",
    "search_site",
    "search_web",
    "open_workspace_folder",
    "browser_click_result",
    "browser_media",
    "browser_scroll",
    "browser_history",
    "browser_read_page",
    "browser_close",
    "wait",
    "navigate_home",
}

ALLOWED_FIELDS = {
    "app",
    "site",
    "query",
    "n",
    "action",
    "direction",
    "seconds",
}

ALLOWED_KINDS = {"none", "single", "plan"}

ALLOWED_TAGS = {
    "single",
    "chain",
    "followup",
    "chat",
    "unsafe",
    "unsupported",
    "nearmiss",
}

ALLOWED_PRIOR_KEYS = {"last_site", "last_query", "last_app"}

# =============================================================================
# Tool and Field Vocabulary Mapping Table
# =============================================================================
# Defines canonical mapping from ORBIT parser representations to the benchmark
# vocabulary.
#
# Key behaviors:
# 1. Tool names:
#    - Most parser tools share exact names with benchmark tools.
#    - Home-page navigation: the shortcut parser represents home navigation
#      (e.g., 'go home', 'main menu', 'youtube home') as an `open_website`
#      call targeting the site's home URL. When evaluating against an expected
#      `navigate_home` tool, this is recognized and mapped to `navigate_home`.
# 2. Argument names:
#    - `open_application` / `close_application`: argument `name` maps to `app`.
#    - `open_website`: argument `url` is resolved to `site` key using SITES.
#    - `search_site`: arguments `site` and `query` map directly.
#    - `search_web`: argument `query` maps directly.
#    - `browser_click_result`: argument `n` maps directly.
#    - `browser_media`: argument `action` maps directly.
#    - `browser_scroll`: argument `direction` maps directly.
#    - `browser_history`: argument `action` maps directly.
#    - `wait`: argument `seconds` maps directly.
# =============================================================================

TOOL_MAPPING: dict[str, str] = {
    "open_application": "open_application",
    "close_application": "close_application",
    "open_website": "open_website",
    "search_site": "search_site",
    "search_web": "search_web",
    "open_workspace_folder": "open_workspace_folder",
    "browser_click_result": "browser_click_result",
    "browser_media": "browser_media",
    "browser_scroll": "browser_scroll",
    "browser_history": "browser_history",
    "browser_read_page": "browser_read_page",
    "browser_close": "browser_close",
    "wait": "wait",
    "navigate_home": "navigate_home",
}

ARG_MAPPING: dict[str, dict[str, str]] = {
    "open_application": {"name": "app"},
    "close_application": {"name": "app"},
    "open_website": {"url": "site"},
    "search_site": {"site": "site", "query": "query"},
    "search_web": {"query": "query"},
    "open_workspace_folder": {"path": "path"},
    "browser_click_result": {"n": "n"},
    "browser_media": {"action": "action"},
    "browser_scroll": {"direction": "direction"},
    "browser_history": {"action": "action"},
    "wait": {"seconds": "seconds"},
}


def get_git_commit() -> str:
    """Retrieve current git HEAD commit hash, with '-dirty' suffix if working tree has changes."""
    commit = "unknown"
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit = res.stdout.strip()
    except Exception:
        pass

    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        if status_res.stdout.strip():
            return f"{commit}-dirty"
    except Exception:
        pass

    return commit


def validate_commands_dataset(data: list[dict[str, Any]], split_name: str = "") -> None:
    """Validate a command evaluation dataset split against strict benchmark constraints.

    Args:
        data: List of parsed JSON items.
        split_name: Name of the split ('dev' or 'test') for error messages.

    Raises:
        ValueError: If dataset fails schema, item count, distribution, or vocabulary checks.
    """
    if not isinstance(data, list):
        raise ValueError(f"Commands dataset '{split_name}' must contain a list of items.")

    if len(data) != 40:
        raise ValueError(f"Commands dataset '{split_name}' must contain exactly 40 items, got {len(data)}.")

    seen_ids: set[str] = set()
    counts = {"single": 0, "plan": 0, "none": 0}
    tag_counts: dict[str, int] = {}

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Item #{idx} in '{split_name}' must be an object.")

        for required_key in ("id", "text", "expect", "tag"):
            if required_key not in item:
                raise ValueError(f"Item #{idx} in '{split_name}' missing required field '{required_key}'.")

        item_id = str(item["id"]).strip()
        if not item_id:
            raise ValueError(f"Item #{idx} in '{split_name}' has empty ID.")
        if item_id in seen_ids:
            raise ValueError(f"Duplicate item ID '{item_id}' in '{split_name}'.")
        seen_ids.add(item_id)

        text = item["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Item '{item_id}' has empty text.")

        if ";" in text:
            raise ValueError(f"Item '{item_id}' contains forbidden semicolon in text: {text!r}")

        if re.search(r"://|www\.|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?", text, re.IGNORECASE):
            raise ValueError(f"Item '{item_id}' contains forbidden URL pattern in text: {text!r}")

        tag = item["tag"]
        if tag not in ALLOWED_TAGS:
            raise ValueError(f"Item '{item_id}' has unknown tag '{tag}'. Allowed: {ALLOWED_TAGS}")
        tag_counts[tag] = tag_counts.get(tag, 0) + 1

        expect = item["expect"]
        if not isinstance(expect, dict) or "kind" not in expect:
            raise ValueError(f"Item '{item_id}' has invalid 'expect' structure.")

        kind = expect["kind"]
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"Item '{item_id}' has unknown expect kind '{kind}'. Allowed: {ALLOWED_KINDS}")
        counts[kind] += 1

        if kind == "single":
            if tag != "single":
                raise ValueError(f"Item '{item_id}' kind 'single' must have tag 'single', got '{tag}'.")
            tool = expect.get("tool")
            if tool not in ALLOWED_TOOLS:
                raise ValueError(f"Item '{item_id}' has unknown tool '{tool}'. Allowed: {ALLOWED_TOOLS}")
            fields = expect.get("fields", {})
            if not isinstance(fields, dict):
                raise ValueError(f"Item '{item_id}' fields must be a dict.")
            for fk in fields:
                if fk not in ALLOWED_FIELDS:
                    raise ValueError(f"Item '{item_id}' has unknown field '{fk}'. Allowed: {ALLOWED_FIELDS}")

        elif kind == "plan":
            if tag not in ("chain", "followup"):
                raise ValueError(f"Item '{item_id}' kind 'plan' must have tag 'chain' or 'followup', got '{tag}'.")
            steps = expect.get("steps")
            if not isinstance(steps, list) or not (2 <= len(steps) <= 4):
                raise ValueError(f"Item '{item_id}' plan must contain 2 to 4 steps, got {len(steps) if isinstance(steps, list) else type(steps)}.")
            for s_idx, step in enumerate(steps):
                if not isinstance(step, dict) or "tool" not in step:
                    raise ValueError(f"Item '{item_id}' step #{s_idx} missing 'tool'.")
                st = step["tool"]
                if st not in ALLOWED_TOOLS:
                    raise ValueError(f"Item '{item_id}' step #{s_idx} has unknown tool '{st}'.")
                fields = step.get("fields", {})
                if not isinstance(fields, dict):
                    raise ValueError(f"Item '{item_id}' step #{s_idx} fields must be a dict.")
                for fk in fields:
                    if fk not in ALLOWED_FIELDS:
                        raise ValueError(f"Item '{item_id}' step #{s_idx} has unknown field '{fk}'.")

        elif kind == "none":
            if tag not in ("chat", "unsafe", "unsupported", "nearmiss"):
                raise ValueError(f"Item '{item_id}' kind 'none' must have negative tag, got '{tag}'.")

        prior = item.get("prior")
        if prior is not None:
            if not isinstance(prior, dict):
                raise ValueError(f"Item '{item_id}' 'prior' must be a dict.")
            for pk in prior:
                if pk not in ALLOWED_PRIOR_KEYS:
                    raise ValueError(f"Item '{item_id}' 'prior' contains unknown key '{pk}'. Allowed: {ALLOWED_PRIOR_KEYS}")

    # Check distribution: 12 single, 10 plan, 18 none
    if counts["single"] != 12:
        raise ValueError(f"Split '{split_name}' must have 12 'single' items, found {counts['single']}.")
    if counts["plan"] != 10:
        raise ValueError(f"Split '{split_name}' must have 10 'plan' items, found {counts['plan']}.")
    if counts["none"] != 18:
        raise ValueError(f"Split '{split_name}' must have 18 'none' items, found {counts['none']}.")

    # Check negative tag distribution: 5 chat, 5 unsafe, 4 unsupported, 4 nearmiss
    expected_negative_tags = {"chat": 5, "unsafe": 5, "unsupported": 4, "nearmiss": 4}
    for ntag, exp_cnt in expected_negative_tags.items():
        actual_cnt = tag_counts.get(ntag, 0)
        if actual_cnt != exp_cnt:
            raise ValueError(f"Split '{split_name}' must have {exp_cnt} '{ntag}' items, found {actual_cnt}.")


def load_commands_dataset(
    split: str,
    base_dir: Optional[Union[str, Path]] = None,
    validate: bool = True,
) -> list[dict[str, Any]]:
    """Load and validate a command dataset split JSON file.

    Args:
        split: 'dev' or 'test'.
        base_dir: Base directory containing commands split JSONs (default: evals/commands).
        validate: Whether to run strict schema and vocabulary validation.

    Returns:
        list[dict[str, Any]]: Parsed items from the JSON split.
    """
    clean_split = split.lower().strip()
    eff_dir = Path(base_dir) if base_dir is not None else Path("evals/commands")
    split_file = eff_dir / f"{clean_split}.json"

    if not split_file.exists():
        raise FileNotFoundError(f"Commands dataset split file not found: {split_file}")

    with open(split_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    if validate:
        validate_commands_dataset(data, split_name=clean_split)

    return data


def normalize_parser_step(
    tool_name: str,
    tool_args: dict[str, Any],
    expected_tool: Optional[str] = None,
    target_name: str = "",
) -> tuple[str, dict[str, Any]]:
    """Normalize a raw parser step into benchmark vocabulary tool name and fields.

    Args:
        tool_name: Raw tool name from ToolCallSpec or PlanStep.
        tool_args: Raw arguments dictionary.
        expected_tool: Expected tool name from benchmark for contextual mapping (e.g. navigate_home).
        target_name: Target label if available.

    Returns:
        tuple[str, dict[str, Any]]: (normalized_tool_name, normalized_fields).
    """
    mapped_tool = TOOL_MAPPING.get(tool_name, tool_name)
    raw_args = dict(tool_args or {})
    fields: dict[str, Any] = {}

    # Handle open_website and navigate_home mapping
    if tool_name == "open_website":
        url = str(raw_args.get("url", ""))
        site_key, _ = _extract_site_key(url, sites=SITES)
        if not site_key and target_name:
            resolved = resolve_site(target_name)
            site_key = resolved if resolved else target_name.lower()

        if expected_tool == "navigate_home":
            mapped_tool = "navigate_home"
        else:
            mapped_tool = "open_website"
            if site_key:
                fields["site"] = site_key.lower()

    # Map remaining known arguments to benchmark field names
    for parser_arg, val in raw_args.items():
        bench_field = ARG_MAPPING.get(tool_name, {}).get(parser_arg)
        if bench_field and bench_field not in fields:
            fields[bench_field] = val

    return mapped_tool, fields


def match_step(
    actual_step_tool: str,
    actual_step_args: dict[str, Any],
    expected_step: dict[str, Any],
    target_name: str = "",
) -> tuple[bool, str]:
    """Compare a single parsed step against an expected step.

    Returns:
        tuple[bool, str]: (matched, failure_reason)
    """
    expected_tool = expected_step.get("tool", "")
    expected_fields = expected_step.get("fields", {})

    actual_tool, actual_fields = normalize_parser_step(
        actual_step_tool,
        actual_step_args,
        expected_tool=expected_tool,
        target_name=target_name,
    )

    if actual_tool != expected_tool:
        return False, f"tool mismatch: expected '{expected_tool}', got '{actual_tool}'"

    for k, exp_val in expected_fields.items():
        if k not in actual_fields:
            return False, f"missing field '{k}': expected '{exp_val}'"
        act_val = actual_fields[k]

        if k in ("site", "query", "app", "action", "direction"):
            if str(act_val).strip().lower() != str(exp_val).strip().lower():
                return False, f"field '{k}' mismatch: expected '{exp_val}', got '{act_val}'"
        elif k in ("n", "seconds"):
            try:
                if int(act_val) != int(exp_val):
                    return False, f"field '{k}' mismatch: expected {exp_val}, got {act_val}"
            except (ValueError, TypeError):
                return False, f"field '{k}' mismatch: expected {exp_val}, got '{act_val}'"
        else:
            if act_val != exp_val:
                return False, f"field '{k}' mismatch: expected '{exp_val}', got '{act_val}'"

    return True, ""


def extract_steps_from_parsed(
    parsed: Optional[Union[ToolCallSpec, ShortcutPlan]]
) -> Optional[list[tuple[str, dict[str, Any], str]]]:
    """Extract list of (tool, args, target_name) tuples from a parser result."""
    if parsed is None:
        return None
    if isinstance(parsed, ToolCallSpec):
        return [(parsed.name, dict(parsed.args or {}), getattr(parsed, "target_name", ""))]
    if isinstance(parsed, ShortcutPlan):
        steps = []
        for s in parsed.steps:
            t = getattr(s, "tool", getattr(s, "name", ""))
            a = getattr(s, "args", {})
            tgt = getattr(s, "target_name", "")
            steps.append((t, dict(a or {}), tgt))
        return steps
    if hasattr(parsed, "tool") and hasattr(parsed, "args"):
        return [(parsed.tool, dict(parsed.args or {}), getattr(parsed, "target_name", ""))]
    return None


def match_command(
    expect: dict[str, Any],
    parsed: Optional[Union[ToolCallSpec, ShortcutPlan]],
) -> tuple[bool, int, int, str]:
    """Match parser output against the benchmark expectation.

    Args:
        expect: Expected dictionary with 'kind', optional 'tool'/'fields', or 'steps'.
        parsed: Result of parse_shortcut / parse_chain.

    Returns:
        tuple[bool, int, int, str]:
            - passed: overall boolean match
            - matched_steps: count of matching steps
            - expected_steps: count of expected steps
            - fail_reason: failure explanation string if not passed
    """
    kind = expect.get("kind", "none")
    actual_steps = extract_steps_from_parsed(parsed)

    if kind == "none":
        if actual_steps is None:
            return True, 0, 0, ""
        return False, 0, 0, f"expected none, but parser returned {len(actual_steps)} step(s)"

    if kind == "single":
        exp_step = {"tool": expect.get("tool", ""), "fields": expect.get("fields", {})}
        if actual_steps is None:
            return False, 0, 1, "parser returned None"
        if len(actual_steps) != 1:
            return False, 0, 1, f"expected single step, got {len(actual_steps)} steps"
        t, a, tgt = actual_steps[0]
        matched, reason = match_step(t, a, exp_step, target_name=tgt)
        return matched, (1 if matched else 0), 1, reason

    if kind == "plan":
        exp_steps = expect.get("steps", [])
        total_exp = len(exp_steps)
        if actual_steps is None:
            return False, 0, total_exp, "parser returned None"

        matched_count = 0
        step_failures: list[str] = []

        for idx, exp_s in enumerate(exp_steps):
            if idx < len(actual_steps):
                t, a, tgt = actual_steps[idx]
                m, r = match_step(t, a, exp_s, target_name=tgt)
                if m:
                    matched_count += 1
                else:
                    step_failures.append(f"step {idx + 1}: {r}")
            else:
                step_failures.append(f"step {idx + 1}: missing step")

        if len(actual_steps) > total_exp:
            step_failures.append(f"extra {len(actual_steps) - total_exp} step(s) returned")

        passed = (matched_count == total_exp) and (len(actual_steps) == total_exp)
        return passed, matched_count, total_exp, "; ".join(step_failures)

    return False, 0, 0, f"unknown expect kind '{kind}'"


def build_session_state(prior: Optional[dict[str, Any]]) -> SessionState:
    """Build an active SessionState from an item's 'prior' context dictionary."""
    state = SessionState()
    if prior:
        state._last_site = prior.get("last_site")
        state._last_query = prior.get("last_query")
        state._last_app = prior.get("last_app")
        state.updated_at = time.time()
    return state


def run_parser_for_item(
    item: dict[str, Any],
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
) -> Optional[Union[ToolCallSpec, ShortcutPlan]]:
    """Execute ORBIT command parsing: try parse_shortcut, fallback to parse_chain."""
    if allowed_apps is None:
        allowed_apps = ALLOWED_APPS
    if sites is None:
        sites = SITES

    state = build_session_state(item.get("prior"))
    text = item["text"]

    if has_explicit_sequencing_separator(text):
        res = parse_chain(
            text=text,
            allowed_apps=allowed_apps,
            sites=sites,
            state=state,
            browser_enabled=True,
        )
        if res is None:
            res = parse_shortcut(
                text=text,
                allowed_apps=allowed_apps,
                sites=sites,
                state=state,
                browser_enabled=True,
            )
    else:
        res = parse_shortcut(
            text=text,
            allowed_apps=allowed_apps,
            sites=sites,
            state=state,
            browser_enabled=True,
        )
        if res is None:
            res = parse_chain(
                text=text,
                allowed_apps=allowed_apps,
                sites=sites,
                state=state,
                browser_enabled=True,
            )
    return res


def evaluate_command_item(
    item: dict[str, Any],
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Evaluate a single command item against parser execution."""
    parsed = run_parser_for_item(item, allowed_apps=allowed_apps, sites=sites)
    passed, matched_steps, expected_steps, reason = match_command(item["expect"], parsed)

    # Format what parser returned for reporting
    actual_steps = extract_steps_from_parsed(parsed)
    if actual_steps is None:
        returned_repr = "None"
    else:
        norm_steps = []
        for t, a, tgt in actual_steps:
            nt, nf = normalize_parser_step(t, a, target_name=tgt)
            norm_steps.append({"tool": nt, "fields": nf})
        if len(norm_steps) == 1:
            returned_repr = json.dumps(norm_steps[0])
        else:
            returned_repr = json.dumps(norm_steps)

    return {
        "id": item["id"],
        "text": item["text"],
        "tag": item["tag"],
        "prior": item.get("prior"),
        "expect": item["expect"],
        "expected_kind": item["expect"].get("kind", "none"),
        "expected_step_count": expected_steps,
        "matched_step_count": matched_steps,
        "passed": passed,
        "fail_reason": reason,
        "parsed_raw": parsed,
        "returned_repr": returned_repr,
    }


def compute_commands_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate benchmark metrics across all evaluated items."""
    positives = [r for r in results if r["expected_kind"] in ("single", "plan")]
    negatives = [r for r in results if r["expected_kind"] == "none"]

    single_positives = [r for r in positives if r["expected_kind"] == "single"]
    plan_positives = [r for r in positives if r["expected_kind"] == "plan"]

    pos_passed = sum(1 for r in positives if r["passed"])
    pos_recall = pos_passed / len(positives) if positives else 0.0

    single_passed = sum(1 for r in single_positives if r["passed"])
    single_recall = single_passed / len(single_positives) if single_positives else 0.0

    plan_passed = sum(1 for r in plan_positives if r["passed"])
    plan_recall = plan_passed / len(plan_positives) if plan_positives else 0.0

    # Step exact-match rate
    total_exp_steps = sum(r["expected_step_count"] for r in positives)
    total_matched_steps = sum(r["matched_step_count"] for r in positives)
    step_exact_match_rate = total_matched_steps / total_exp_steps if total_exp_steps > 0 else 0.0

    # Positive recall by tag
    recall_by_tag: dict[str, dict[str, Any]] = {}
    for r in positives:
        t = r["tag"]
        if t not in recall_by_tag:
            recall_by_tag[t] = {"total": 0, "passed": 0, "recall": 0.0}
        recall_by_tag[t]["total"] += 1
        if r["passed"]:
            recall_by_tag[t]["passed"] += 1
    for t, data in recall_by_tag.items():
        data["recall"] = data["passed"] / data["total"] if data["total"] > 0 else 0.0

    # Negative false-match rate
    neg_false_matches = sum(1 for r in negatives if not r["passed"])
    neg_false_match_rate = neg_false_matches / len(negatives) if negatives else 0.0

    # Negative false-match rate by tag
    false_match_by_tag: dict[str, dict[str, Any]] = {}
    for r in negatives:
        t = r["tag"]
        if t not in false_match_by_tag:
            false_match_by_tag[t] = {"total": 0, "false_matches": 0, "rate": 0.0}
        false_match_by_tag[t]["total"] += 1
        if not r["passed"]:
            false_match_by_tag[t]["false_matches"] += 1
    for t, data in false_match_by_tag.items():
        data["rate"] = data["false_matches"] / data["total"] if data["total"] > 0 else 0.0

    return {
        "total_items": len(results),
        "total_passed": sum(1 for r in results if r["passed"]),
        "pos_total": len(positives),
        "pos_passed": pos_passed,
        "pos_recall": pos_recall,
        "single_total": len(single_positives),
        "single_passed": single_passed,
        "single_recall": single_recall,
        "plan_total": len(plan_positives),
        "plan_passed": plan_passed,
        "plan_recall": plan_recall,
        "total_exp_steps": total_exp_steps,
        "total_matched_steps": total_matched_steps,
        "step_exact_match_rate": step_exact_match_rate,
        "recall_by_tag": recall_by_tag,
        "neg_total": len(negatives),
        "neg_false_matches": neg_false_matches,
        "neg_false_match_rate": neg_false_match_rate,
        "false_match_by_tag": false_match_by_tag,
    }


def render_commands_report(
    split: str,
    commit_hash: str,
    metrics: dict[str, Any],
    results: list[dict[str, Any]],
    settings_meta: dict[str, Any],
    is_rerun_not_unseen: bool = False,
) -> str:
    """Render comprehensive markdown evaluation report."""
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dirty_marker = " (dirty)" if "-dirty" in commit_hash else ""
    failing_items = [r for r in results if not r["passed"]]

    lines = [
        f"# ORBIT Command Benchmark Report ({split.upper()} split)",
        "",
    ]

    if is_rerun_not_unseen:
        lines.extend([
            "> [!WARNING]",
            "> **RERUN: NOT UNSEEN** - This test split was previously executed.",
            "> With `--force`, this dataset is now classified as dev data and is no longer an unseen test set.",
            "",
        ])

    lines.extend([
        f"- **Execution Timestamp**: {timestamp_str}",
        f"- **Dataset Split**: `{split}`",
        f"- **Git Commit Hash**: `{commit_hash}`{dirty_marker}",
        f"- **Settings**: `browser_enabled={settings_meta.get('browser_enabled')}`, `chains_enabled={settings_meta.get('chains_enabled')}`, `allowed_apps={len(settings_meta.get('allowed_apps', []))}`, `sites={len(settings_meta.get('sites', []))}`",
    ])

    if is_rerun_not_unseen:
        lines.append("- **Integrity Status**: `RERUN: NOT UNSEEN`")

    lines.extend([
        "",
        "## Headline Metrics",
        "",
        "| Metric | Result | Description |",
        "|---|---|---|",
        f"| **Positive Recall (overall)** | {metrics['pos_recall'] * 100:.1f}% ({metrics['pos_passed']}/{metrics['pos_total']}) | Supported commands correctly parsed |",
        f"| **Positive Recall (single)** | {metrics['single_recall'] * 100:.1f}% ({metrics['single_passed']}/{metrics['single_total']}) | Single-step shortcut commands |",
        f"| **Positive Recall (plan)** | {metrics['plan_recall'] * 100:.1f}% ({metrics['plan_passed']}/{metrics['plan_total']}) | Multi-step chained / followup plans |",
        f"| **Step Exact-Match Rate** | {metrics['step_exact_match_rate'] * 100:.1f}% ({metrics['total_matched_steps']}/{metrics['total_exp_steps']}) | Exact step matches across positive plans |",
        f"| **Negative False-Match Rate** | {metrics['neg_false_match_rate'] * 100:.1f}% ({metrics['neg_false_matches']}/{metrics['neg_total']}) | Unsupported/chat/unsafe prompts incorrectly handled |",
        "",
        "## Positive Recall by Tag",
        "",
        "| Tag | Passed | Total | Recall |",
        "|---|---|---|---|",
    ])

    for tag, t_data in sorted(metrics.get("recall_by_tag", {}).items()):
        lines.append(f"| `{tag}` | {t_data['passed']} | {t_data['total']} | {t_data['recall'] * 100:.1f}% |")

    lines.extend([
        "",
        "## Negative False-Match Rate by Tag",
        "",
        "| Tag | False Matches | Total | False-Match Rate |",
        "|---|---|---|---|",
    ])

    for tag, t_data in sorted(metrics.get("false_match_by_tag", {}).items()):
        lines.append(f"| `{tag}` | {t_data['false_matches']} | {t_data['total']} | {t_data['rate'] * 100:.1f}% |")

    lines.extend([
        "",
        f"## Failing Items ({len(failing_items)})",
        "",
    ])

    if not failing_items:
        lines.append("None! All items matched expectations.")
    else:
        lines.extend([
            "| ID | Tag | Kind | Text | Expected | Parser Returned | Reason |",
            "|---|---|---|---|---|---|---|",
        ])
        for f_item in failing_items:
            q_clean = f_item["text"].replace("|", "\\|")
            exp_clean = json.dumps(f_item["expect"]).replace("|", "\\|")
            ret_clean = f_item["returned_repr"].replace("|", "\\|")
            reas_clean = f_item["fail_reason"].replace("|", "\\|")
            lines.append(
                f"| `{f_item['id']}` | `{f_item['tag']}` | `{f_item['expected_kind']}` | {q_clean} | `{exp_clean}` | `{ret_clean}` | {reas_clean} |"
            )

    lines.extend([
        "",
        "## All Item Results",
        "",
        "| ID | Tag | Kind | Status | Text | Expected | Parser Returned |",
        "|---|---|---|---|---|---|---|",
    ])

    for r in results:
        status_icon = "✓ PASS" if r["passed"] else "✗ FAIL"
        q_clean = r["text"].replace("|", "\\|")
        exp_clean = json.dumps(r["expect"]).replace("|", "\\|")
        ret_clean = r["returned_repr"].replace("|", "\\|")
        lines.append(
            f"| `{r['id']}` | `{r['tag']}` | `{r['expected_kind']}` | {status_icon} | {q_clean} | `{exp_clean}` | `{ret_clean}` |"
        )

    lines.append("")
    return "\n".join(lines)


def run_commands_eval(
    split: str,
    force: bool = False,
    out_dir: str = "evals/results",
    dataset_path: Optional[Union[str, Path]] = None,
    marker_path: Optional[Union[str, Path]] = None,
    report_filename: Optional[str] = None,
) -> int:
    """Execute command shortcut/chain benchmark evaluation pipeline.

    Args:
        split: 'dev' or 'test'.
        force: Allow rerunning test split (marks report 'RERUN: NOT UNSEEN').
        out_dir: Output directory for reports.
        dataset_path: Optional override path for dataset JSON file.
        marker_path: Optional override path for test marker file.
        report_filename: Optional override filename for output markdown report.

    Returns:
        int: Exit status code (0 for success, 1 on refusal/error).
    """
    clean_split = split.lower().strip()
    if clean_split not in ("dev", "test"):
        print(f"Error: Invalid commands split '{split}'. Must be 'dev' or 'test'.", file=sys.stderr)
        return 1

    # Integrity verification for test split
    eff_marker = Path(marker_path) if marker_path is not None else Path("evals/commands/.ran")
    is_rerun_not_unseen = False

    if clean_split == "test":
        if eff_marker.exists():
            if not force:
                print(
                    f"Error: commands test split has already been run (marker {eff_marker} exists). "
                    "A second run on test split is refused to maintain unseen test set integrity. "
                    "Use --force to override.",
                    file=sys.stderr,
                )
                return 1
            else:
                print(
                    "WARNING: commands test split has already been run. "
                    "With --force, test split is now considered a dev set and is no longer an unseen test set."
                )
                is_rerun_not_unseen = True

    # Load dataset
    try:
        if dataset_path is not None:
            with open(dataset_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            validate_commands_dataset(data, split_name=clean_split)
            items = data
        else:
            items = load_commands_dataset(clean_split)
    except Exception as exc:
        print(f"Error loading commands dataset: {exc}", file=sys.stderr)
        return 1

    commit_hash = get_git_commit()
    settings_meta = {
        "browser_enabled": True,
        "chains_enabled": os.getenv("CHAINS_ENABLED", "1") not in ("0", "false", "no", "off"),
        "allowed_apps": sorted(ALLOWED_APPS.keys()),
        "sites": sorted(SITES.keys()),
    }

    print(f"\nStarting command benchmark evaluation on '{clean_split}' split ({len(items)} items)...")
    print(f"Git commit: {commit_hash}")
    print(f"Settings: browser_enabled=True, chains_enabled={settings_meta['chains_enabled']}, allowed_apps={len(settings_meta['allowed_apps'])}, sites={len(settings_meta['sites'])}")
    if is_rerun_not_unseen:
        print("Status: RERUN: NOT UNSEEN")

    # Evaluate each item
    results = [evaluate_command_item(item) for item in items]
    metrics = compute_commands_metrics(results)

    # Format report
    report_content = render_commands_report(
        split=clean_split,
        commit_hash=commit_hash,
        metrics=metrics,
        results=results,
        settings_meta=settings_meta,
        is_rerun_not_unseen=is_rerun_not_unseen,
    )

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    if report_filename:
        report_file = out_path / report_filename
    else:
        report_file = out_path / f"commands-{clean_split}-{timestamp_tag}.md"
    report_file.write_text(report_content, encoding="utf-8")

    # Write marker file on test run completion
    if clean_split == "test":
        try:
            date_str = datetime.now(timezone.utc).isoformat()
        except Exception:
            date_str = datetime.now().isoformat()
        is_dirty = "-dirty" in commit_hash
        marker_content = f"date: {date_str}\ncommit: {commit_hash}\ndirty: {is_dirty}\n"
        try:
            eff_marker.parent.mkdir(parents=True, exist_ok=True)
            eff_marker.write_text(marker_content, encoding="utf-8")
        except Exception as e:
            print(f"Warning: Failed to write marker file {eff_marker}: {e}", file=sys.stderr)

    # Print console summary
    print("\n" + "=" * 65)
    print(f"COMMAND BENCHMARK SUMMARY ({clean_split.upper()} split)")
    print("=" * 65)
    print(f"Git commit:                {commit_hash}")
    print(f"Integrity status:          {'RERUN: NOT UNSEEN' if is_rerun_not_unseen else 'OK'}")
    print(f"Positive Recall (overall): {metrics['pos_recall'] * 100:.1f}% ({metrics['pos_passed']}/{metrics['pos_total']})")
    print(f"  - Single:                {metrics['single_recall'] * 100:.1f}% ({metrics['single_passed']}/{metrics['single_total']})")
    print(f"  - Plan:                  {metrics['plan_recall'] * 100:.1f}% ({metrics['plan_passed']}/{metrics['plan_total']})")
    print(f"Step Exact-Match Rate:     {metrics['step_exact_match_rate'] * 100:.1f}% ({metrics['total_matched_steps']}/{metrics['total_exp_steps']})")
    print(f"Negative False-Match Rate: {metrics['neg_false_match_rate'] * 100:.1f}% ({metrics['neg_false_matches']}/{metrics['neg_total']})")
    print("-" * 65)
    print("Positive Recall by Tag:")
    for tag, t_data in sorted(metrics.get("recall_by_tag", {}).items()):
        print(f"  - {tag:<15} {t_data['recall'] * 100:.1f}% ({t_data['passed']}/{t_data['total']})")
    print("Negative False-Match Rate by Tag:")
    for tag, t_data in sorted(metrics.get("false_match_by_tag", {}).items()):
        print(f"  - {tag:<15} {t_data['rate'] * 100:.1f}% ({t_data['false_matches']}/{t_data['total']})")
    print("=" * 65)

    failing = [r for r in results if not r["passed"]]
    if failing:
        print(f"\nFailing Items ({len(failing)}):")
        for f in failing:
            print(f"  [{f['id']}] ({f['tag']}) '{f['text']}'")
            print(f"     Expected: {json.dumps(f['expect'])}")
            print(f"     Returned: {f['returned_repr']}")
            print(f"     Reason:   {f['fail_reason']}")

    print(f"\nReport written to: {report_file}\n")
    return 0
