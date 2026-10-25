"""Audit logging module for recording tool execution events and security decisions."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional

# Keywords that trigger automatic credential/secret redaction
_SENSITIVE_KEYWORDS = ("password", "token", "secret", "key", "credential")
_MAX_STRING_LENGTH = 200


def redact_data(data: Any) -> Any:
    """Recursively redact sensitive keys and truncate long strings."""
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(keyword in k_lower for keyword in _SENSITIVE_KEYWORDS):
                redacted[k] = "***"
            else:
                redacted[k] = redact_data(v)
        return redacted
    elif isinstance(data, list):
        return [redact_data(item) for item in data]
    elif isinstance(data, str):
        return data[:_MAX_STRING_LENGTH]
    return data


class AuditLogger:
    """Appends structured JSON-lines audit records for tool executions and security decisions."""

    _redact_data = staticmethod(redact_data)

    def __init__(self, path: str = "logs/audit.jsonl") -> None:
        self.path = Path(path)
        self._warned = False

        # Attempt to create parent directory ahead of time
        try:
            if self.path.parent and not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            if not self._warned:
                print(f"Warning: AuditLogger could not create log directory '{self.path.parent}': {exc}")
                self._warned = True

    def log(
        self,
        tool: str,
        args: dict,
        decision: Any,
        outcome: str,
        result: Optional[Any] = None,
        duration_ms: Optional[float] = None,
        tainted: bool = False,
        extra: Optional[dict] = None,
    ) -> None:
        """Append an audit record to the JSON-lines log file. Never raises exceptions."""
        try:
            decision_str = decision.name.lower() if hasattr(decision, "name") else str(decision).lower()

            # Truncate result string to 200 chars if present
            truncated_result = None
            if result is not None:
                truncated_result = str(result)[:_MAX_STRING_LENGTH]

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool": tool,
                "args": self._redact_data(args),
                "decision": decision_str,
                "outcome": outcome,
                "result": truncated_result,
                "duration_ms": duration_ms,
                "tainted": bool(tainted),
            }

            if extra:
                redacted_extra = self._redact_data(extra)
                if isinstance(redacted_extra, dict):
                    entry.update(redacted_extra)
                    entry["extra"] = redacted_extra

            # Ensure parent exists in case directory was removed or initialized lazily
            if self.path.parent and not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

        except Exception as exc:
            # Audit logging must never compromise execution flow of the assistant
            if not self._warned:
                print(f"Warning: AuditLogger failed to write audit entry to '{self.path}': {exc}")
                self._warned = True

    def read_all(self) -> list[dict]:
        """Read and parse all audit records from the log file (helper for testing and inspection)."""
        if not self.path.exists():
            return []

        records = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except Exception as exc:
            if not self._warned:
                print(f"Warning: AuditLogger failed reading logs from '{self.path}': {exc}")
                self._warned = True
        return records
