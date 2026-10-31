"""Evidence evaluation engine for ORBIT guided verification.

Evaluates evidence rules against the audit log, active settings, and the filesystem.

Guarantees:
- Closed set of rules: audit_tool_success, audit_tool_denied, audit_no_tool_success,
  audit_decision, audit_source, setting_equals, file_exists_under_home, all_of.
- Evaluation outcomes: "found", "not_found", or "unavailable".
- Strictly reads only: tool, decision, outcome, tainted, source, timestamp.
- NEVER reads 'args' or 'result' from audit records (privacy protection).
- Evidence only suggests; user always decides Pass/Fail/Skip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from config.paths import REPO_ROOT, get_audit_log_path, get_orbit_home
from config.settings_store import get_effective_setting


@dataclass(frozen=True)
class EvidenceResult:
    """Outcome of an evidence rule evaluation."""

    status: str  # "found", "not_found", "unavailable"
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


def _safe_parse_iso(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        # Standardize 'Z' to UTC offset
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def read_audit_entries_filtered(
    audit_path: Optional[Path] = None,
    since: Optional[datetime | str] = None,
) -> Optional[list[dict[str, Any]]]:
    """Read audit log entries, extracting strictly privacy-safe fields.

    Only reads: tool, decision, outcome, tainted, source, timestamp.
    Explicitly ignores args, result, extra, or custom payload content.
    Returns None if file does not exist or cannot be read.
    """
    path = audit_path if audit_path is not None else get_audit_log_path()
    if not path.exists():
        return None

    since_dt: Optional[datetime] = None
    if isinstance(since, str):
        since_dt = _safe_parse_iso(since)
    elif isinstance(since, datetime):
        since_dt = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)

    sanitized: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue

                ts_str = record.get("timestamp")
                if since_dt is not None and ts_str:
                    rec_dt = _safe_parse_iso(ts_str)
                    if rec_dt is not None and rec_dt < since_dt:
                        continue

                # STRICT privacy projection: only allowlisted metadata fields
                sanitized.append({
                    "timestamp": ts_str,
                    "tool": str(record.get("tool", "")),
                    "decision": str(record.get("decision", "")).lower(),
                    "outcome": str(record.get("outcome", "")).lower(),
                    "tainted": bool(record.get("tainted", False)),
                    "source": str(record.get("source", "")),
                })
        return sanitized
    except Exception:
        return None


class EvidenceEvaluator:
    """Evaluates evidence rules with privacy protection and sandboxed path resolution."""

    def __init__(
        self,
        audit_path: Optional[Path] = None,
        audit_entries: Optional[Sequence[dict[str, Any]]] = None,
        settings_getter: Optional[Callable[[str], Any]] = None,
        orbit_home: Optional[Path] = None,
    ) -> None:
        self.audit_path = audit_path
        self._preset_entries = audit_entries
        self.settings_getter = settings_getter or (lambda k: get_effective_setting(k).effective_value)
        self.orbit_home = orbit_home or get_orbit_home() or REPO_ROOT

    def evaluate(
        self,
        rule: Optional[Mapping[str, Any]],
        since: Optional[datetime | str] = None,
    ) -> EvidenceResult:
        """Evaluate a structured rule against current evidence sources."""
        if not rule:
            return EvidenceResult("unavailable", "No evidence rule defined for this item")

        rtype = rule.get("type")
        if not rtype:
            return EvidenceResult("unavailable", "Rule missing 'type' field")

        if rtype == "all_of":
            return self._eval_all_of(rule, since)
        if rtype == "audit_tool_success":
            return self._eval_audit_tool_success(rule, since)
        if rtype == "audit_tool_denied":
            return self._eval_audit_tool_denied(rule, since)
        if rtype == "audit_no_tool_success":
            return self._eval_audit_no_tool_success(rule, since)
        if rtype == "audit_decision":
            return self._eval_audit_decision(rule, since)
        if rtype == "audit_source":
            return self._eval_audit_source(rule, since)
        if rtype == "setting_equals":
            return self._eval_setting_equals(rule)
        if rtype == "file_exists_under_home":
            return self._eval_file_exists_under_home(rule)

        return EvidenceResult("unavailable", f"Unknown rule type: {rtype}")

    def _get_entries(self, since: Optional[datetime | str]) -> Optional[list[dict[str, Any]]]:
        if self._preset_entries is not None:
            since_dt: Optional[datetime] = None
            if isinstance(since, str):
                since_dt = _safe_parse_iso(since)
            elif isinstance(since, datetime):
                since_dt = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)

            filtered = []
            for e in self._preset_entries:
                if since_dt is not None and e.get("timestamp"):
                    rec_dt = _safe_parse_iso(e["timestamp"])
                    if rec_dt is not None and rec_dt < since_dt:
                        continue
                filtered.append({
                    "timestamp": e.get("timestamp"),
                    "tool": str(e.get("tool", "")),
                    "decision": str(e.get("decision", "")).lower(),
                    "outcome": str(e.get("outcome", "")).lower(),
                    "tainted": bool(e.get("tainted", False)),
                    "source": str(e.get("source", "")),
                })
            return filtered
        return read_audit_entries_filtered(self.audit_path, since=since)

    def _eval_all_of(self, rule: Mapping[str, Any], since: Optional[datetime | str]) -> EvidenceResult:
        sub_rules = rule.get("rules", [])
        if not sub_rules:
            return EvidenceResult("unavailable", "all_of rule has empty rules list")

        reasons = []
        any_unavailable = False
        all_found = True

        for sub in sub_rules:
            res = self.evaluate(sub, since)
            reasons.append(f"[{res.status}: {res.reason}]")
            if res.status == "unavailable":
                any_unavailable = True
            if res.status != "found":
                all_found = False

        if all_found:
            return EvidenceResult("found", "All sub-rules matched: " + "; ".join(reasons))
        if any_unavailable:
            return EvidenceResult("unavailable", "Sub-rules incomplete: " + "; ".join(reasons))
        return EvidenceResult("not_found", "Sub-rules did not all match: " + "; ".join(reasons))

    def _eval_audit_tool_success(self, rule: Mapping[str, Any], since: Optional[datetime | str]) -> EvidenceResult:
        tool_name = rule.get("tool")
        if not tool_name:
            return EvidenceResult("unavailable", "audit_tool_success rule missing 'tool'")

        entries = self._get_entries(since)
        if entries is None:
            return EvidenceResult("unavailable", "Audit log is not available")

        for e in entries:
            if e["tool"] == tool_name:
                dec = e["decision"]
                out = e["outcome"]
                if dec in ("allow", "allowed", "auto") and (out == "success" or out.startswith("success")):
                    return EvidenceResult("found", f"Tool '{tool_name}' executed successfully ({dec}/{out})")

        return EvidenceResult("not_found", f"No successful execution of tool '{tool_name}' in audit log")

    def _eval_audit_tool_denied(self, rule: Mapping[str, Any], since: Optional[datetime | str]) -> EvidenceResult:
        tool_name = rule.get("tool")
        if not tool_name:
            return EvidenceResult("unavailable", "audit_tool_denied rule missing 'tool'")

        entries = self._get_entries(since)
        if entries is None:
            return EvidenceResult("unavailable", "Audit log is not available")

        for e in entries:
            if e["tool"] == tool_name:
                dec = e["decision"]
                out = e["outcome"]
                if dec in ("deny", "denied", "block", "blocked", "cancel", "cancelled") or out in (
                    "denied",
                    "cancelled",
                    "refused",
                    "blocked",
                    "error",
                ):
                    return EvidenceResult("found", f"Tool '{tool_name}' was denied/blocked ({dec}/{out})")

        return EvidenceResult("not_found", f"No denied/blocked entry for tool '{tool_name}' in audit log")

    def _eval_audit_no_tool_success(self, rule: Mapping[str, Any], since: Optional[datetime | str]) -> EvidenceResult:
        entries = self._get_entries(since)
        if entries is None:
            return EvidenceResult("unavailable", "Audit log is not available")

        for e in entries:
            dec = e["decision"]
            out = e["outcome"]
            if dec in ("allow", "allowed", "auto") and (out == "success" or out.startswith("success")):
                return EvidenceResult("not_found", f"A tool execution succeeded unexpectedly: {e['tool']}")

        return EvidenceResult("found", "No successful tool executions found in audit log")

    def _eval_audit_decision(self, rule: Mapping[str, Any], since: Optional[datetime | str]) -> EvidenceResult:
        target_dec = str(rule.get("decision", "")).lower()
        target_tool = rule.get("tool")
        if not target_dec:
            return EvidenceResult("unavailable", "audit_decision rule missing 'decision'")

        entries = self._get_entries(since)
        if entries is None:
            return EvidenceResult("unavailable", "Audit log is not available")

        for e in entries:
            dec = e["decision"]
            if dec == target_dec:
                if target_tool is None or e["tool"] == target_tool:
                    tool_desc = f" for '{e['tool']}'" if e['tool'] else ""
                    return EvidenceResult("found", f"Decision '{target_dec}' found in audit log{tool_desc}")

        return EvidenceResult("not_found", f"Decision '{target_dec}' not found in audit log")

    def _eval_audit_source(self, rule: Mapping[str, Any], since: Optional[datetime | str]) -> EvidenceResult:
        target_source = str(rule.get("source", ""))
        if not target_source:
            return EvidenceResult("unavailable", "audit_source rule missing 'source'")

        entries = self._get_entries(since)
        if entries is None:
            return EvidenceResult("unavailable", "Audit log is not available")

        for e in entries:
            if e["source"] == target_source:
                return EvidenceResult("found", f"Audit entry with source '{target_source}' found")

        return EvidenceResult("not_found", f"Source '{target_source}' not found in audit log")

    def _eval_setting_equals(self, rule: Mapping[str, Any]) -> EvidenceResult:
        key = rule.get("key")
        expected = rule.get("value")
        if not key:
            return EvidenceResult("unavailable", "setting_equals rule missing 'key'")

        try:
            val = self.settings_getter(key)
        except Exception as exc:
            return EvidenceResult("unavailable", f"Could not read setting '{key}': {exc}")

        # Normalize booleans and strings
        val_str = str(val).strip().lower() if val is not None else ""
        exp_str = str(expected).strip().lower() if expected is not None else ""

        if val_str == exp_str or val == expected:
            return EvidenceResult("found", f"Setting '{key}' equals '{expected}' (actual: '{val}')")
        return EvidenceResult("not_found", f"Setting '{key}' is '{val}', expected '{expected}'")

    def _eval_file_exists_under_home(self, rule: Mapping[str, Any]) -> EvidenceResult:
        rel = rule.get("relative_path")
        if not rel:
            return EvidenceResult("unavailable", "file_exists_under_home rule missing 'relative_path'")

        try:
            target = (self.orbit_home / rel).resolve()
            # Ensure path does not escape home root
            if not str(target).startswith(str(self.orbit_home.resolve())):
                return EvidenceResult("unavailable", f"Path '{rel}' escapes orbit home root")

            if target.exists():
                return EvidenceResult("found", f"File '{rel}' exists under home")
            return EvidenceResult("not_found", f"File '{rel}' not found under home")
        except Exception as exc:
            return EvidenceResult("unavailable", f"Filesystem error checking '{rel}': {exc}")
