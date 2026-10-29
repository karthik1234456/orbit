"""Task run state tracking, step auditing, and execution metrics for ORBIT."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Optional, Union

from security.audit import redact_data
from config.paths import get_runs_log_path


class RunStatus(Enum):
    """Execution termination status for a task run."""
    COMPLETED = "COMPLETED"
    STEP_LIMIT = "STEP_LIMIT"
    LOOP_DETECTED = "LOOP_DETECTED"
    TOO_MANY_ERRORS = "TOO_MANY_ERRORS"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class StepRecord:
    """Record of an individual tool execution step in a task run."""
    index: int
    tool: str
    args: dict
    status: str  # "success" | "error" | "denied" | "blocked" | "rejected"
    result: str
    duration_ms: int
    decision: Optional[str] = None

    def __post_init__(self) -> None:
        if self.result is not None:
            self.result = str(self.result)[:200]
        else:
            self.result = ""

        if self.duration_ms is not None:
            self.duration_ms = int(round(self.duration_ms))
        else:
            self.duration_ms = 0


@dataclass
class TaskRun:
    """Encapsulates the state, steps, and outcome of a multi-step task execution."""
    goal: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    steps: list[StepRecord] = field(default_factory=list)
    status: Optional[RunStatus] = None
    final_text: str = ""
    llm_calls: int = 0
    unverified_claims: int = 0
    auto_retrievals: list[dict] = field(default_factory=list)
    context: Optional[dict] = None
    plan: Optional[dict] = None
    source: str = "typed"
    routing_on: bool = False
    routing_groups: list[str] = field(default_factory=list)
    tools_offered_count: int = 0
    tools_registered_count: int = 0
    off_route_call: int = 0
    routing_retry: bool = False
    prompt_tokens: Optional[int] = None

    def add_step(
        self,
        tool: Union[str, StepRecord],
        args: Optional[dict] = None,
        status: str = "success",
        result: str = "",
        duration_ms: Union[int, float] = 0,
        index: Optional[int] = None,
        decision: Optional[str] = None,
    ) -> StepRecord:
        """Add a step record to the run."""
        if isinstance(tool, StepRecord):
            step = tool
        else:
            step_index = index if index is not None else len(self.steps) + 1
            step = StepRecord(
                index=step_index,
                tool=tool,
                args=args or {},
                status=status,
                result=result,
                duration_ms=int(round(duration_ms)),
                decision=decision,
            )
        self.steps.append(step)
        return step

    def finish(self, status: RunStatus, text: str = "") -> None:
        """Mark the task run as finished with final status and summary text."""
        self.status = status
        self.final_text = text

    def to_dict(self) -> dict:
        """Return a JSON-serializable dictionary with arguments redacted."""
        status_val = None
        if self.status is not None:
            status_val = self.status.value if isinstance(self.status, Enum) else str(self.status)

        steps_data = []
        for s in self.steps:
            st = {
                "index": s.index,
                "tool": s.tool,
                "args": redact_data(s.args),
                "status": s.status,
                "result": s.result[:200] if s.result else "",
                "duration_ms": s.duration_ms,
            }
            if s.decision is not None:
                st["decision"] = s.decision
            steps_data.append(st)

        res = {
            "goal": self.goal,
            "started_at": self.started_at,
            "status": status_val,
            "final_text": self.final_text,
            "llm_calls": self.llm_calls,
            "unverified_claims": self.unverified_claims,
            "auto_retrievals": list(self.auto_retrievals),
            "context": dict(self.context) if self.context is not None else None,
            "source": self.source,
            "steps": steps_data,
            "routing_on": self.routing_on,
            "routing_groups": list(self.routing_groups),
            "tools_offered_count": self.tools_offered_count,
            "tools_registered_count": self.tools_registered_count,
            "off_route_call": self.off_route_call,
            "routing_retry": self.routing_retry,
            "prompt_tokens": self.prompt_tokens,
            "routing": {
                "on": self.routing_on,
                "groups": list(self.routing_groups),
                "tools_offered": self.tools_offered_count,
                "tools_registered": self.tools_registered_count,
                "off_route_calls": self.off_route_call,
                "routing_retry": self.routing_retry,
                "prompt_tokens": self.prompt_tokens,
            },
        }
        if self.plan is not None:
            res["plan"] = self.plan
        return res


    def summary(self) -> str:
        """Return a short multi-line summary of each step with checkmark or cross."""
        if not self.steps:
            return "No steps executed."

        lines = []
        for s in self.steps:
            mark = "✓" if s.status == "success" else "✗"
            lines.append(f"{mark} Step {s.index}: {s.tool} ({s.status}) -> {s.result}")
        return "\n".join(lines)


class RunLogger:
    """Appends structured JSON-lines run records for completed task executions."""

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = Path(path if path is not None else get_runs_log_path()).resolve()
        self._warned = False
        try:
            if self.path.parent and not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            if not self._warned:
                print(f"Warning: RunLogger could not create log directory '{self.path.parent}': {exc}")
                self._warned = True

    def log(self, run: TaskRun) -> None:
        """Append run.to_dict() as one JSON line. Never raises exceptions."""
        try:
            if self.path.parent and not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)

            entry = run.to_dict()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            if not self._warned:
                print(f"Warning: RunLogger failed to write run entry to '{self.path}': {exc}")
                self._warned = True

    def read_all(self) -> list[dict]:
        """Read and parse all logged run records (helper for testing and inspection)."""
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
                print(f"Warning: RunLogger failed reading logs from '{self.path}': {exc}")
                self._warned = True
        return records
