"""Pure aggregation functions and data structures for ORBIT Insights analytics."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from typing import Any, Optional, Union

from config.paths import get_audit_log_path, get_runs_log_path


@dataclass
class InsightsSummary:
    """Aggregated insights metrics over an evaluation period."""
    total_events: int = 0
    tool_success_rate: float = 0.0
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    confirmations_approved: int = 0
    confirmations_denied: int = 0
    blocked_by_type: dict[str, int] = field(default_factory=dict)
    top_tools: list[tuple[str, int]] = field(default_factory=list)
    median_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    voice_reviewed: int = 0
    voice_direct: int = 0
    daily_activity: list[tuple[str, int]] = field(default_factory=list)
    period: str = "all"
    has_data: bool = False


def _parse_iso_ts(ts_val: Any) -> Optional[datetime]:
    """Parse an ISO timestamp string into a UTC datetime."""
    if not ts_val or not isinstance(ts_val, str):
        return None
    try:
        clean = ts_val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def read_jsonl_tail(path: Union[str, Path], max_records: int = 5000) -> list[dict]:
    """Read at most the newest max_records from a JSON-lines file without raising."""
    p = Path(path)
    if not p.is_file():
        return []
    records: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []

    if len(records) > max_records:
        records = records[-max_records:]
    return records


def aggregate_insights(
    audit_records: list[dict],
    run_records: Optional[list[dict]] = None,
    period: str = "all",
    now: Optional[datetime] = None,
    max_records: int = 5000,
) -> InsightsSummary:
    """Aggregate audit and task run records into an InsightsSummary.

    Args:
        audit_records: List of audit record dicts.
        run_records: Optional list of task run dicts.
        period: One of '24h', '7d', 'all'.
        now: Reference UTC datetime for cutoff. Defaults to datetime.now(timezone.utc).
        max_records: Cap on records processed.

    Returns:
        InsightsSummary dataclass with counts, percentages, and sparkline data.
    """
    if audit_records is None:
        audit_records = []
    if run_records is None:
        run_records = []

    # Slice to newest max_records
    if len(audit_records) > max_records:
        audit_records = audit_records[-max_records:]
    if len(run_records) > max_records:
        run_records = run_records[-max_records:]

    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    cutoff: Optional[datetime] = None
    if period == "24h":
        cutoff = ref_now - timedelta(hours=24)
    elif period == "7d":
        cutoff = ref_now - timedelta(days=7)

    # Filter audit records by period
    filtered_audits: list[dict] = []
    for r in audit_records:
        if not isinstance(r, dict):
            continue
        ts = _parse_iso_ts(r.get("timestamp"))
        if cutoff and ts and ts < cutoff:
            continue
        filtered_audits.append(r)

    # Filter run records by period
    filtered_runs: list[dict] = []
    for r in run_records:
        if not isinstance(r, dict):
            continue
        ts = _parse_iso_ts(r.get("started_at"))
        if cutoff and ts and ts < cutoff:
            continue
        filtered_runs.append(r)

    total_events = len(filtered_audits) + len(filtered_runs)
    if total_events == 0:
        return InsightsSummary(period=period, has_data=False)

    total_tool_calls = 0
    successful_tool_calls = 0
    confirmations_approved = 0
    confirmations_denied = 0
    blocked_by_type: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    latencies: list[float] = []
    voice_reviewed = 0
    voice_direct = 0
    daily_buckets: dict[str, int] = {}

    for r in filtered_audits:
        # Tool call stats
        tool = r.get("tool")
        if tool:
            total_tool_calls += 1
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            outcome = str(r.get("outcome", "")).lower()
            if outcome == "success":
                successful_tool_calls += 1

        # Confirmations
        decision = str(r.get("decision", "")).lower()
        if decision in ("confirmed", "approved", "user_approved", "allow"):
            confirmations_approved += 1
        elif decision in ("denied", "rejected", "cancelled", "user_denied"):
            confirmations_denied += 1

        # Blocked actions
        outcome = str(r.get("outcome", "")).lower()
        if outcome == "blocked" or decision in ("blocked", "tainted", "precheck", "rate_limit", "not_model_visible"):
            if r.get("tainted"):
                block_key = "tainted"
            elif decision and decision not in ("blocked", ""):
                block_key = decision
            else:
                block_key = "blocked"
            blocked_by_type[block_key] = blocked_by_type.get(block_key, 0) + 1

        # Latencies
        dur = r.get("duration_ms")
        if isinstance(dur, (int, float)) and dur > 0:
            latencies.append(float(dur))

        # Voice usage
        src = str(r.get("source", "")).lower()
        if src in ("voice_reviewed", "voice-reviewed", "reviewed"):
            voice_reviewed += 1
        elif src in ("voice", "voice_direct", "voice-direct", "voice-handsfree"):
            voice_direct += 1

        # Daily activity bucket
        ts = _parse_iso_ts(r.get("timestamp"))
        if ts:
            day_str = ts.strftime("%Y-%m-%d")
            daily_buckets[day_str] = daily_buckets.get(day_str, 0) + 1

    # Also extract latencies and voice stats from run records
    for run in filtered_runs:
        src = str(run.get("source", "")).lower()
        if src in ("voice_reviewed", "voice-reviewed", "reviewed"):
            voice_reviewed += 1
        elif src in ("voice", "voice_direct", "voice-direct", "voice-handsfree"):
            voice_direct += 1

        # Steps in run
        steps = run.get("steps") or []
        for s in steps:
            if isinstance(s, dict):
                dur = s.get("duration_ms")
                if isinstance(dur, (int, float)) and dur > 0:
                    latencies.append(float(dur))

        ts = _parse_iso_ts(run.get("started_at"))
        if ts:
            day_str = ts.strftime("%Y-%m-%d")
            daily_buckets[day_str] = daily_buckets.get(day_str, 0) + 1

    # Calculations
    tool_success_rate = (
        round((successful_tool_calls / total_tool_calls) * 100.0, 1)
        if total_tool_calls > 0
        else 0.0
    )

    # Top 8 tools
    sorted_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:8]

    # Latency percentiles
    median_lat = 0.0
    p95_lat = 0.0
    if latencies:
        latencies.sort()
        n = len(latencies)
        median_lat = round(latencies[n // 2], 1)
        p95_idx = min(n - 1, int(round(n * 0.95)))
        p95_lat = round(latencies[p95_idx], 1)

    # Daily activity sorted chronologically
    sorted_daily = sorted(daily_buckets.items(), key=lambda x: x[0])

    return InsightsSummary(
        total_events=total_events,
        tool_success_rate=tool_success_rate,
        total_tool_calls=total_tool_calls,
        successful_tool_calls=successful_tool_calls,
        confirmations_approved=confirmations_approved,
        confirmations_denied=confirmations_denied,
        blocked_by_type=blocked_by_type,
        top_tools=sorted_tools,
        median_latency_ms=median_lat,
        p95_latency_ms=p95_lat,
        voice_reviewed=voice_reviewed,
        voice_direct=voice_direct,
        daily_activity=sorted_daily,
        period=period,
        has_data=total_events > 0,
    )


def load_insights_summary(
    audit_path: Optional[Union[str, Path]] = None,
    runs_path: Optional[Union[str, Path]] = None,
    period: str = "all",
    now: Optional[datetime] = None,
    max_records: int = 5000,
) -> InsightsSummary:
    """Safely load and aggregate insights summary from log files."""
    a_path = Path(audit_path if audit_path is not None else get_audit_log_path())
    r_path = Path(runs_path if runs_path is not None else get_runs_log_path())

    audits = read_jsonl_tail(a_path, max_records=max_records)
    runs = read_jsonl_tail(r_path, max_records=max_records)

    return aggregate_insights(audits, runs, period=period, now=now, max_records=max_records)

