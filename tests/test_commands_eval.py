"""Unit tests for command benchmark dataset loader, matcher, integrity, and reporting."""

import copy
import json
from pathlib import Path
import pytest

from core.shortcuts import ShortcutPlan, ToolCallSpec
from evals.commands_eval import (
    ALLOWED_FIELDS,
    ALLOWED_TOOLS,
    load_commands_dataset,
    match_command,
    match_step,
    render_commands_report,
    run_commands_eval,
    validate_commands_dataset,
)


# -----------------------------------------------------------------------------
# 1. Loader & Dataset Validation Tests
# -----------------------------------------------------------------------------

def test_loader_validates_both_real_files():
    """Verify that both evals/commands/dev.json and test.json pass all validation rules."""
    dev_data = load_commands_dataset("dev")
    test_data = load_commands_dataset("test")

    assert len(dev_data) == 40
    assert len(test_data) == 40

    # IDs unique within each file and across files
    dev_ids = {item["id"] for item in dev_data}
    test_ids = {item["id"] for item in test_data}
    assert len(dev_ids) == 40
    assert len(test_ids) == 40
    assert len(dev_ids.intersection(test_ids)) == 0

    # 12 / 10 / 18 distribution and tag checks
    for split_name, dataset in (("dev", dev_data), ("test", test_data)):
        singles = [d for d in dataset if d["expect"]["kind"] == "single"]
        plans = [d for d in dataset if d["expect"]["kind"] == "plan"]
        nones = [d for d in dataset if d["expect"]["kind"] == "none"]

        assert len(singles) == 12, f"Failed single count on {split_name}"
        assert len(plans) == 10, f"Failed plan count on {split_name}"
        assert len(nones) == 18, f"Failed none count on {split_name}"

        # Single tags
        assert all(d["tag"] == "single" for d in singles)

        # Plan tags (chain or followup)
        assert all(d["tag"] in ("chain", "followup") for d in plans)
        for p in plans:
            assert 2 <= len(p["expect"]["steps"]) <= 4

        # None negative tags: 5 chat, 5 unsafe, 4 unsupported, 4 nearmiss
        chat = [d for d in nones if d["tag"] == "chat"]
        unsafe = [d for d in nones if d["tag"] == "unsafe"]
        unsupported = [d for d in nones if d["tag"] == "unsupported"]
        nearmiss = [d for d in nones if d["tag"] == "nearmiss"]

        assert len(chat) == 5
        assert len(unsafe) == 5
        assert len(unsupported) == 4
        assert len(nearmiss) == 4

        # Tool and field names within allowed vocabulary
        for item in dataset:
            text = item["text"]
            assert ";" not in text, f"Found semicolon in text: {text}"
            assert "://" not in text and "www." not in text, f"Found URL in text: {text}"

            expect = item["expect"]
            if expect["kind"] == "single":
                assert expect["tool"] in ALLOWED_TOOLS
                assert set(expect.get("fields", {}).keys()).issubset(ALLOWED_FIELDS)
            elif expect["kind"] == "plan":
                for s in expect["steps"]:
                    assert s["tool"] in ALLOWED_TOOLS
                    assert set(s.get("fields", {}).keys()).issubset(ALLOWED_FIELDS)


def test_loader_rejects_duplicate_ids():
    """Verify loader raises ValueError when duplicate item IDs exist."""
    data = load_commands_dataset("dev")
    corrupt = copy.deepcopy(data)
    corrupt[1]["id"] = corrupt[0]["id"]

    with pytest.raises(ValueError, match="Duplicate item ID"):
        validate_commands_dataset(corrupt, split_name="corrupt")


def test_loader_rejects_wrong_item_count():
    """Verify loader raises ValueError if item count is not exactly 40."""
    data = load_commands_dataset("dev")
    with pytest.raises(ValueError, match="must contain exactly 40 items"):
        validate_commands_dataset(data[:39], split_name="short")

    corrupt = copy.deepcopy(data)
    extra = copy.deepcopy(corrupt[0])
    extra["id"] = "cmd-dev-999"
    corrupt.append(extra)
    with pytest.raises(ValueError, match="must contain exactly 40 items"):
        validate_commands_dataset(corrupt, split_name="long")


def test_loader_rejects_wrong_distribution():
    """Verify loader raises ValueError if kind distribution deviates from 12/10/18."""
    data = load_commands_dataset("dev")
    corrupt = copy.deepcopy(data)
    # Turn a single into none
    corrupt[0]["expect"] = {"kind": "none"}
    corrupt[0]["tag"] = "chat"
    with pytest.raises(ValueError, match="must have 12 'single' items"):
        validate_commands_dataset(corrupt, split_name="dist")


def test_loader_rejects_disallowed_vocabulary():
    """Verify loader rejects tools or fields outside the benchmark vocabulary."""
    data = load_commands_dataset("dev")

    # Invalid tool
    corrupt_tool = copy.deepcopy(data)
    corrupt_tool[0]["expect"]["tool"] = "delete_hard_drive"
    with pytest.raises(ValueError, match="unknown tool 'delete_hard_drive'"):
        validate_commands_dataset(corrupt_tool, split_name="bad_tool")

    # Invalid field
    corrupt_field = copy.deepcopy(data)
    corrupt_field[0]["expect"]["fields"]["forbidden_arg"] = "value"
    with pytest.raises(ValueError, match="unknown field 'forbidden_arg'"):
        validate_commands_dataset(corrupt_field, split_name="bad_field")


def test_loader_rejects_semicolons_and_urls():
    """Verify loader rejects texts containing semicolons or URLs."""
    data = load_commands_dataset("dev")

    # Semicolon
    corrupt_semi = copy.deepcopy(data)
    corrupt_semi[0]["text"] = "open notepad; rm -rf"
    with pytest.raises(ValueError, match="forbidden semicolon"):
        validate_commands_dataset(corrupt_semi, split_name="semi")

    # URL
    corrupt_url = copy.deepcopy(data)
    corrupt_url[0]["text"] = "open https://example.com"
    with pytest.raises(ValueError, match="forbidden URL pattern"):
        validate_commands_dataset(corrupt_url, split_name="url")


# -----------------------------------------------------------------------------
# 2. Matcher Unit Tests
# -----------------------------------------------------------------------------

def test_matcher_passes_correct_single():
    """Verify matcher passes a valid single tool execution."""
    expect = {
        "kind": "single",
        "tool": "open_application",
        "fields": {"app": "notepad"},
    }
    parsed = ToolCallSpec(name="open_application", args={"name": "notepad"})
    passed, matched, total, reason = match_command(expect, parsed)

    assert passed is True
    assert matched == 1
    assert total == 1
    assert reason == ""


def test_matcher_passes_correct_plan():
    """Verify matcher passes a valid multi-step plan."""
    expect = {
        "kind": "plan",
        "steps": [
            {"tool": "open_website", "fields": {"site": "youtube"}},
            {"tool": "search_site", "fields": {"site": "youtube", "query": "jazz music"}},
            {"tool": "browser_click_result", "fields": {"n": 1}},
        ],
    }
    parsed = ShortcutPlan(
        steps=[
            ToolCallSpec(name="open_website", args={"url": "https://www.youtube.com"}),
            ToolCallSpec(name="search_site", args={"site": "youtube", "query": "jazz music"}),
            ToolCallSpec(name="browser_click_result", args={"n": 1}),
        ]
    )
    passed, matched, total, reason = match_command(expect, parsed)

    assert passed is True
    assert matched == 3
    assert total == 3
    assert reason == ""


def test_matcher_fails_wrong_tool():
    """Verify matcher fails when parser returns a different tool than expected."""
    expect = {
        "kind": "single",
        "tool": "open_application",
        "fields": {"app": "notepad"},
    }
    parsed = ToolCallSpec(name="close_application", args={"name": "notepad"})
    passed, matched, total, reason = match_command(expect, parsed)

    assert passed is False
    assert matched == 0
    assert total == 1
    assert "tool mismatch" in reason


def test_matcher_fails_wrong_field():
    """Verify matcher fails when field argument differs from expectation."""
    expect = {
        "kind": "single",
        "tool": "search_site",
        "fields": {"site": "wikipedia", "query": "quantum computing"},
    }
    parsed = ToolCallSpec(name="search_site", args={"site": "wikipedia", "query": "classical physics"})
    passed, matched, total, reason = match_command(expect, parsed)

    assert passed is False
    assert matched == 0
    assert total == 1
    assert "field 'query' mismatch" in reason


def test_matcher_fails_missing_step():
    """Verify matcher fails when plan has fewer steps than expected."""
    expect = {
        "kind": "plan",
        "steps": [
            {"tool": "open_website", "fields": {"site": "youtube"}},
            {"tool": "browser_click_result", "fields": {"n": 1}},
        ],
    }
    # Parser only returned step 1
    parsed = ShortcutPlan(
        steps=[
            ToolCallSpec(name="open_website", args={"url": "https://www.youtube.com"}),
        ]
    )
    passed, matched, total, reason = match_command(expect, parsed)

    assert passed is False
    assert matched == 1
    assert total == 2
    assert "missing step" in reason


def test_matcher_fails_extra_step():
    """Verify matcher fails when plan has more steps than expected."""
    expect = {
        "kind": "plan",
        "steps": [
            {"tool": "open_website", "fields": {"site": "youtube"}},
        ],
    }
    # Parser returned 2 steps
    parsed = ShortcutPlan(
        steps=[
            ToolCallSpec(name="open_website", args={"url": "https://www.youtube.com"}),
            ToolCallSpec(name="browser_click_result", args={"n": 1}),
        ]
    )
    passed, matched, total, reason = match_command(expect, parsed)

    assert passed is False
    assert matched == 1
    assert total == 1
    assert "extra 1 step(s)" in reason


def test_matcher_none_fails_when_parser_returns_anything():
    """Verify expectation 'none' fails if parser returns a single or plan, and passes if None."""
    expect = {"kind": "none"}

    # Fails on single tool
    parsed_single = ToolCallSpec(name="open_application", args={"name": "notepad"})
    passed, matched, total, reason = match_command(expect, parsed_single)
    assert passed is False
    assert "expected none, but parser returned 1 step(s)" in reason

    # Fails on plan
    parsed_plan = ShortcutPlan(
        steps=[
            ToolCallSpec(name="open_application", args={"name": "notepad"}),
            ToolCallSpec(name="wait", args={"seconds": 2}),
        ]
    )
    passed, matched, total, reason = match_command(expect, parsed_plan)
    assert passed is False
    assert "expected none, but parser returned 2 step(s)" in reason

    # Passes on None
    passed, matched, total, reason = match_command(expect, None)
    assert passed is True
    assert matched == 0
    assert total == 0
    assert reason == ""


def test_matcher_case_insensitive_site_and_query():
    """Verify site and query matching is case-insensitive."""
    expect_step = {
        "tool": "search_site",
        "fields": {"site": "github", "query": "React Hooks"},
    }
    matched, reason = match_step("search_site", {"site": "GitHub", "query": "react hooks"}, expect_step)
    assert matched is True
    assert reason == ""


# -----------------------------------------------------------------------------
# 3. Integrity and Marker Tests
# -----------------------------------------------------------------------------

def test_marker_file_blocks_second_test_run(tmp_path: Path):
    """Verify that a completed test run writes .ran and refuses a second run without --force."""
    # Use real dev dataset as mock payload
    real_dev_file = Path("evals/commands/dev.json")
    marker_file = tmp_path / ".ran"
    out_dir = tmp_path / "results"

    assert not marker_file.exists()

    # First run on test split: should succeed and create marker
    ret1 = run_commands_eval(
        split="test",
        force=False,
        dataset_path=real_dev_file,
        marker_path=marker_file,
        out_dir=str(out_dir),
    )
    assert ret1 == 0
    assert marker_file.exists()

    marker_text = marker_file.read_text(encoding="utf-8")
    assert "date:" in marker_text
    assert "commit:" in marker_text
    assert "dirty:" in marker_text

    # Second run without force: must be refused (exit code 1)
    ret2 = run_commands_eval(
        split="test",
        force=False,
        dataset_path=real_dev_file,
        marker_path=marker_file,
        out_dir=str(out_dir),
    )
    assert ret2 == 1

    # Third run with force=True: permitted with warning
    ret3 = run_commands_eval(
        split="test",
        force=True,
        dataset_path=real_dev_file,
        marker_path=marker_file,
        out_dir=str(out_dir),
    )
    assert ret3 == 0


def test_dev_runs_are_unlimited(tmp_path: Path):
    """Verify that dev runs do not check or create the marker file and run unlimited times."""
    real_dev_file = Path("evals/commands/dev.json")
    marker_file = tmp_path / ".ran"
    out_dir = tmp_path / "results"

    # Run twice
    ret1 = run_commands_eval(
        split="dev",
        force=False,
        dataset_path=real_dev_file,
        marker_path=marker_file,
        out_dir=str(out_dir),
    )
    ret2 = run_commands_eval(
        split="dev",
        force=False,
        dataset_path=real_dev_file,
        marker_path=marker_file,
        out_dir=str(out_dir),
    )
    assert ret1 == 0
    assert ret2 == 0
    assert not marker_file.exists()


# -----------------------------------------------------------------------------
# 4. Report Rendering Tests
# -----------------------------------------------------------------------------

def test_report_states_commit_and_dirty_flag():
    """Verify rendered markdown report includes commit hash, dirty marker, and settings."""
    metrics = {
        "pos_recall": 1.0,
        "pos_passed": 22,
        "pos_total": 22,
        "single_recall": 1.0,
        "single_passed": 12,
        "single_total": 12,
        "plan_recall": 1.0,
        "plan_passed": 10,
        "plan_total": 10,
        "step_exact_match_rate": 1.0,
        "total_matched_steps": 30,
        "total_exp_steps": 30,
        "neg_false_match_rate": 0.0,
        "neg_false_matches": 0,
        "neg_total": 18,
        "recall_by_tag": {"single": {"total": 12, "passed": 12, "recall": 1.0}},
        "false_match_by_tag": {"chat": {"total": 5, "false_matches": 0, "rate": 0.0}},
    }
    settings_meta = {
        "browser_enabled": True,
        "chains_enabled": True,
        "allowed_apps": ["notepad", "calculator"],
        "sites": ["youtube", "google"],
    }
    results = [
        {
            "id": "cmd-dev-001",
            "text": "open notepad",
            "tag": "single",
            "expected_kind": "single",
            "passed": True,
            "expect": {"kind": "single", "tool": "open_application", "fields": {"app": "notepad"}},
            "returned_repr": '{"tool": "open_application", "fields": {"app": "notepad"}}',
            "fail_reason": "",
        }
    ]

    report = render_commands_report(
        split="dev",
        commit_hash="deadbeef1234-dirty",
        metrics=metrics,
        results=results,
        settings_meta=settings_meta,
        is_rerun_not_unseen=False,
    )

    assert "deadbeef1234-dirty" in report
    assert "(dirty)" in report
    assert "browser_enabled=True" in report
    assert "Positive Recall (overall)" in report

    # Also test rerun marker in report
    rerun_report = render_commands_report(
        split="test",
        commit_hash="deadbeef1234",
        metrics=metrics,
        results=results,
        settings_meta=settings_meta,
        is_rerun_not_unseen=True,
    )
    assert "RERUN: NOT UNSEEN" in rerun_report
