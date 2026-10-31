"""Tests for core/insights.py analytics and aggregation."""

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

from core.insights import aggregate_insights, load_insights_summary


def test_aggregate_insights_empty():
    summary = aggregate_insights([], [])
    assert summary.has_data is False
    assert summary.total_events == 0
    assert summary.tool_success_rate == 0.0
    assert summary.top_tools == []
    assert summary.daily_activity == []


def test_aggregate_insights_success_rate_and_tools():
    audits = [
        {"tool": "open_app", "outcome": "success", "decision": "allow", "duration_ms": 100},
        {"tool": "open_app", "outcome": "success", "decision": "allow", "duration_ms": 120},
        {"tool": "open_app", "outcome": "error", "decision": "allow", "duration_ms": 150},
        {"tool": "find_files", "outcome": "success", "decision": "allow", "duration_ms": 50},
    ]
    summary = aggregate_insights(audits)
    assert summary.has_data is True
    assert summary.total_events == 4
    assert summary.total_tool_calls == 4
    assert summary.successful_tool_calls == 3
    assert summary.tool_success_rate == 75.0
    assert summary.top_tools[0] == ("open_app", 3)
    assert summary.top_tools[1] == ("find_files", 1)


def test_aggregate_insights_confirmations_and_blocks():
    audits = [
        {"tool": "send_msg", "decision": "confirmed", "outcome": "success"},
        {"tool": "send_msg", "decision": "user_approved", "outcome": "success"},
        {"tool": "delete_file", "decision": "denied", "outcome": "blocked"},
        {"tool": "eval_code", "decision": "tainted", "outcome": "blocked", "tainted": True},
        {"tool": "shell_exec", "decision": "precheck", "outcome": "blocked"},
    ]
    summary = aggregate_insights(audits)
    assert summary.confirmations_approved == 2
    assert summary.confirmations_denied == 1
    assert summary.blocked_by_type.get("tainted") == 1
    assert summary.blocked_by_type.get("precheck") == 1


def test_aggregate_insights_latency_and_voice():
    now_iso = datetime.now(timezone.utc).isoformat()
    audits = [
        {"tool": "t1", "outcome": "success", "duration_ms": 50, "source": "voice_reviewed", "timestamp": now_iso},
        {"tool": "t2", "outcome": "success", "duration_ms": 100, "source": "voice", "timestamp": now_iso},
        {"tool": "t3", "outcome": "success", "duration_ms": 200, "source": "typed", "timestamp": now_iso},
    ]
    runs = [
        {"started_at": now_iso, "source": "voice_reviewed", "steps": [{"duration_ms": 150}]},
    ]
    summary = aggregate_insights(audits, runs)
    assert summary.voice_reviewed == 2
    assert summary.voice_direct == 1
    assert summary.median_latency_ms == 125.0 or summary.median_latency_ms == 150.0
    assert summary.p95_latency_ms >= 150.0


def test_aggregate_insights_period_filtering():
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    recent_ts = (now - timedelta(hours=2)).isoformat()
    week_ts = (now - timedelta(days=3)).isoformat()
    old_ts = (now - timedelta(days=20)).isoformat()

    audits = [
        {"tool": "t1", "outcome": "success", "timestamp": recent_ts},
        {"tool": "t2", "outcome": "success", "timestamp": week_ts},
        {"tool": "t3", "outcome": "success", "timestamp": old_ts},
    ]

    s_24h = aggregate_insights(audits, period="24h", now=now)
    assert s_24h.total_events == 1

    s_7d = aggregate_insights(audits, period="7d", now=now)
    assert s_7d.total_events == 2

    s_all = aggregate_insights(audits, period="all", now=now)
    assert s_all.total_events == 3


def test_load_insights_summary_file_handling(tmp_path: Path):
    audit_file = tmp_path / "audit.jsonl"
    runs_file = tmp_path / "runs.jsonl"

    # Write some lines with one corrupt line
    with open(audit_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({"tool": "t1", "outcome": "success", "duration_ms": 80}) + "\n")
        f.write("NOT_VALID_JSON\n")
        f.write(json.dumps({"tool": "t2", "outcome": "success", "duration_ms": 90}) + "\n")

    summary = load_insights_summary(audit_path=audit_file, runs_path=runs_file)
    assert summary.has_data is True
    assert summary.total_tool_calls == 2
