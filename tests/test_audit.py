"""Tests for AuditLogger functionality: JSON validation, secret redaction, truncation, and error resilience."""

import json
from pathlib import Path
import pytest
from security.audit import AuditLogger
from security.permissions import Decision


def test_audit_log_valid_json(tmp_path: Path):
    """Verify logged lines are valid JSON and all expected metadata fields are stored."""
    log_file = tmp_path / "logs" / "audit.jsonl"
    logger = AuditLogger(path=str(log_file))

    logger.log(
        tool="calculate",
        args={"expression": "2 + 2"},
        decision=Decision.ALLOW,
        outcome="success",
        result="4",
        duration_ms=5.4,
    )

    # Verify line can be read and parsed by the helper
    records = logger.read_all()
    assert len(records) == 1
    record = records[0]

    assert "timestamp" in record
    assert record["tool"] == "calculate"
    assert record["args"] == {"expression": "2 + 2"}
    assert record["decision"] == "allow"
    assert record["outcome"] == "success"
    assert record["result"] == "4"
    assert record["duration_ms"] == 5.4

    # Verify directly from raw file content
    with open(log_file, "r", encoding="utf-8") as f:
        raw_lines = [line.strip() for line in f if line.strip()]
    assert len(raw_lines) == 1
    parsed_raw = json.loads(raw_lines[0])
    assert parsed_raw["tool"] == "calculate"


def test_secrets_are_redacted(tmp_path: Path):
    """Verify keys containing password, token, secret, key, or credential are redacted."""
    log_file = tmp_path / "audit.jsonl"
    logger = AuditLogger(path=str(log_file))

    sensitive_args = {
        "api_key": "sk-12345abcdef",
        "USER_PASSWORD": "supersecretpassword",
        "auth_token": "bearer-xyz",
        "client_secret": "my-client-secret",
        "userCredential": "id-and-hash",
        "safe_query": "hello world",
    }

    logger.log(
        tool="authenticate",
        args=sensitive_args,
        decision="allow",
        outcome="success",
        result="authenticated",
    )

    records = logger.read_all()
    assert len(records) == 1
    logged_args = records[0]["args"]

    assert logged_args["api_key"] == "***"
    assert logged_args["USER_PASSWORD"] == "***"
    assert logged_args["auth_token"] == "***"
    assert logged_args["client_secret"] == "***"
    assert logged_args["userCredential"] == "***"
    assert logged_args["safe_query"] == "hello world"


def test_long_strings_are_truncated(tmp_path: Path):
    """Verify that string arguments and results longer than 200 characters are truncated."""
    log_file = tmp_path / "audit.jsonl"
    logger = AuditLogger(path=str(log_file))

    long_input = "A" * 300
    long_result = "B" * 500

    logger.log(
        tool="text_processor",
        args={"input_text": long_input},
        decision="allow",
        outcome="success",
        result=long_result,
    )

    records = logger.read_all()
    assert len(records) == 1
    record = records[0]

    # Verify input argument truncation
    assert len(record["args"]["input_text"]) == 200
    assert record["args"]["input_text"] == "A" * 200

    # Verify result truncation
    assert len(record["result"]) == 200
    assert record["result"] == "B" * 200


def test_logging_to_unwritable_path_does_not_raise(tmp_path: Path):
    """Verify that logging failures (e.g. unwritable or invalid paths) never raise exceptions."""
    # Create a file and attempt to treat it as a directory to force an OS path failure
    blocker_file = tmp_path / "not_a_directory"
    blocker_file.write_text("blocker", encoding="utf-8")

    unwritable_path = blocker_file / "audit.jsonl"
    logger = AuditLogger(path=str(unwritable_path))

    # Should safely catch the filesystem error and not raise
    try:
        logger.log(
            tool="test_tool",
            args={"key": "value"},
            decision="allow",
            outcome="success",
        )
    except Exception as exc:
        pytest.fail(f"AuditLogger raised an unexpected exception on invalid path: {exc}")

    # read_all should also fail safely without crashing
    assert logger.read_all() == []
