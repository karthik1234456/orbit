"""Tests for task tracking, StepRecord, TaskRun serialization, redaction, and RunLogger."""

import json
from pathlib import Path
import pytest

from core.task import RunLogger, RunStatus, TaskRun


def test_to_dict_is_json_serializable(tmp_path: Path):
    """Verify TaskRun.to_dict produces valid JSON with all metadata and steps."""
    run = TaskRun(
        goal="Calculate sum and save note",
        llm_calls=2,
    )
    run.add_step(
        tool="calculate",
        args={"expression": "10 + 20"},
        status="success",
        result="30",
        duration_ms=12,
    )
    run.finish(status=RunStatus.COMPLETED, text="The sum is 30.")

    data = run.to_dict()

    # Must serialize with standard json.dumps without error
    json_str = json.dumps(data)
    assert isinstance(json_str, str)

    deserialized = json.loads(json_str)
    assert deserialized["goal"] == "Calculate sum and save note"
    assert deserialized["status"] == "COMPLETED"
    assert deserialized["final_text"] == "The sum is 30."
    assert deserialized["llm_calls"] == 2
    assert len(deserialized["steps"]) == 1

    step_data = deserialized["steps"][0]
    assert step_data["index"] == 1
    assert step_data["tool"] == "calculate"
    assert step_data["args"] == {"expression": "10 + 20"}
    assert step_data["status"] == "success"
    assert step_data["result"] == "30"
    assert step_data["duration_ms"] == 12


def test_secrets_are_redacted_in_to_dict():
    """Verify sensitive keys in step arguments are masked as '***' in to_dict()."""
    run = TaskRun(goal="Authenticate and query API")
    sensitive_args = {
        "api_key": "secret-token-12345",
        "user_password": "PlaintextPassword",
        "auth_token": "bearer-xyz",
        "client_secret": "oauth-client-secret",
        "userCredential": "hash-credential",
        "normal_param": "safe-value",
    }
    run.add_step(
        tool="login",
        args=sensitive_args,
        status="success",
        result="logged_in",
        duration_ms=45,
    )

    data = run.to_dict()
    step_args = data["steps"][0]["args"]

    assert step_args["api_key"] == "***"
    assert step_args["user_password"] == "***"
    assert step_args["auth_token"] == "***"
    assert step_args["client_secret"] == "***"
    assert step_args["userCredential"] == "***"
    assert step_args["normal_param"] == "safe-value"


def test_long_results_are_truncated():
    """Verify tool results longer than 200 characters are truncated in StepRecord and to_dict()."""
    long_result = "Z" * 500
    run = TaskRun(goal="Fetch massive output")
    step = run.add_step(
        tool="large_fetch",
        args={},
        status="success",
        result=long_result,
        duration_ms=100,
    )

    # StepRecord truncation
    assert len(step.result) == 200
    assert step.result == "Z" * 200

    # to_dict truncation
    data = run.to_dict()
    assert len(data["steps"][0]["result"]) == 200
    assert data["steps"][0]["result"] == "Z" * 200


def test_summary_marks_success_and_failed_steps():
    """Verify summary() outputs a checkmark (✓) for success and cross (✗) for failures."""
    run = TaskRun(goal="Multi-step workflow")
    run.add_step(tool="get_current_time", args={}, status="success", result="2:00 PM")
    run.add_step(tool="write_note", args={"text": "hello"}, status="error", result="ERROR: write failed")
    run.add_step(tool="open_app", args={"name": "cmd"}, status="denied", result="ERROR: denied")
    run.add_step(tool="restricted_tool", args={}, status="blocked", result="ERROR: blocked")

    summary_text = run.summary()
    lines = summary_text.splitlines()

    assert len(lines) == 4
    # Step 1 succeeded
    assert lines[0].startswith("✓")
    assert "get_current_time" in lines[0]

    # Steps 2, 3, 4 failed or were blocked
    assert lines[1].startswith("✗")
    assert "write_note" in lines[1]

    assert lines[2].startswith("✗")
    assert "open_app" in lines[2]

    assert lines[3].startswith("✗")
    assert "restricted_tool" in lines[3]


def test_run_logger_records_and_resists_unwritable_path(tmp_path: Path):
    """Verify RunLogger appends JSON lines and never raises on filesystem errors."""
    log_file = tmp_path / "logs" / "runs.jsonl"
    logger = RunLogger(path=str(log_file))

    run = TaskRun(goal="Sample task")
    run.add_step(tool="calculate", args={"expression": "1+1"}, status="success", result="2")
    run.finish(RunStatus.COMPLETED, "Done")

    logger.log(run)

    records = logger.read_all()
    assert len(records) == 1
    assert records[0]["goal"] == "Sample task"
    assert records[0]["status"] == "COMPLETED"

    # Test error resilience on an unwritable path
    blocker_file = tmp_path / "blocker_file"
    blocker_file.write_text("blocking directory creation", encoding="utf-8")

    unwritable_logger = RunLogger(path=str(blocker_file / "sub" / "runs.jsonl"))

    try:
        # Logging to an invalid path must not raise exceptions
        unwritable_logger.log(run)
    except Exception as exc:
        pytest.fail(f"RunLogger.log raised an unexpected exception: {exc}")

    assert unwritable_logger.read_all() == []
