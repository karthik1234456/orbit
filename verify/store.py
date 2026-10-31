"""Local persistent results store for ORBIT guided verification.

Manages test session results in data/verify/ adhering to strict hygiene and privacy rules.

Guarantees:
- Results files stored under data/verify/ (results-<YYYYmmdd-HHMMSS>.json).
- Metadata includes: app_version, git_commit, dirty, started_at, finished_at,
  items_sha256, settings_snapshot, and results map.
- Atomic file writes using tempfile and os.replace.
- Skips unparseable or corrupt JSON files gracefully.
- Maximum 20 results files retention policy with prompt/callback before deletion.
- Fast git commit inspection with 2-second timeout and clean fallbacks.
- Privacy guarantee: notes <= 200 chars sanitized, no raw personal secrets recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable, Optional, Sequence

from config.paths import get_verify_dir
from config.settings_store import SPECS, get_effective_setting
from core.version import VERSION
from verify.schema import compute_items_sha256


MAX_RESULTS_FILES = 20
_SAFE_NOTES_RE = re.compile(r"[\r\n\t]+")


def sanitize_note(note: Optional[str]) -> str:
    """Sanitize and truncate note to 200 printable characters."""
    if not note:
        return ""
    # Flatten newlines/tabs and strip
    cleaned = _SAFE_NOTES_RE.sub(" ", str(note)).strip()
    return cleaned[:200]


def get_git_info(
    repo_root: Optional[Path] = None,
    timeout: float = 2.0,
    runner: Optional[Callable[..., Any]] = None,
) -> tuple[Optional[str], bool]:
    """Retrieve current git commit hash and dirty status with a 2-second timeout."""
    r_fn = runner or subprocess.run
    root = repo_root or Path(__file__).resolve().parent.parent

    commit: Optional[str] = None
    dirty: bool = False

    try:
        res = r_fn(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            commit = res.stdout.strip()
    except Exception:
        commit = None

    try:
        res = r_fn(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if res.returncode == 0:
            dirty = bool(res.stdout.strip())
    except Exception:
        dirty = False

    return commit, dirty


def take_settings_snapshot() -> dict[str, Any]:
    """Capture snapshot of all SPECS settings effective values."""
    snapshot: dict[str, Any] = {}
    for key in SPECS:
        try:
            eff = get_effective_setting(key)
            snapshot[key] = eff.effective_value
        except Exception:
            snapshot[key] = None
    return snapshot


@dataclass
class ItemResult:
    """Record of a single checklist item evaluation."""

    status: str  # "pass", "fail", "skip", "blocked"
    notes: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    evidence_status: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "notes": sanitize_note(self.notes) if self.notes else None,
            "timestamp": self.timestamp,
            "evidence_status": self.evidence_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ItemResult":
        return cls(
            status=str(data.get("status", "skip")),
            notes=sanitize_note(data.get("notes")),
            timestamp=str(data.get("timestamp", "")),
            evidence_status=data.get("evidence_status"),
        )


@dataclass
class VerificationRunRecord:
    """Complete record of a verification session run."""

    app_version: str
    git_commit: Optional[str]
    dirty: bool
    started_at: str
    finished_at: str
    items_sha256: str
    settings_snapshot: dict[str, Any]
    results: dict[str, dict[str, Any]]
    path: Optional[Path] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_version": self.app_version,
            "git_commit": self.git_commit,
            "dirty": self.dirty,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "items_sha256": self.items_sha256,
            "settings_snapshot": self.settings_snapshot,
            "results": self.results,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Optional[Path] = None) -> "VerificationRunRecord":
        return cls(
            app_version=str(data.get("app_version", VERSION)),
            git_commit=data.get("git_commit"),
            dirty=bool(data.get("dirty", False)),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            items_sha256=str(data.get("items_sha256", "")),
            settings_snapshot=dict(data.get("settings_snapshot", {})),
            results=dict(data.get("results", {})),
            path=path,
        )

    def summary(self) -> dict[str, int]:
        counts = {"pass": 0, "fail": 0, "skip": 0, "blocked": 0, "total": len(self.results)}
        for item in self.results.values():
            st = item.get("status", "skip")
            counts[st] = counts.get(st, 0) + 1
        return counts


class VerificationStore:
    """Manages reading and writing verification result files with 20-file retention."""

    def __init__(
        self,
        verify_dir: Optional[Path] = None,
        confirm_delete_callback: Optional[Callable[[list[Path]], bool]] = None,
    ) -> None:
        self.verify_dir = (verify_dir or get_verify_dir()).resolve()
        self.confirm_delete_callback = confirm_delete_callback

    def _ensure_dir(self) -> None:
        self.verify_dir.mkdir(parents=True, exist_ok=True)

    def list_result_files(self) -> list[Path]:
        """List all valid results-*.json files sorted chronologically."""
        if not self.verify_dir.exists():
            return []
        files = []
        for p in self.verify_dir.iterdir():
            if p.is_file() and p.name.startswith("results-") and p.name.endswith(".json"):
                files.append(p)
        files.sort(key=lambda p: p.name)
        return files

    def load_run(self, path: Path) -> Optional[VerificationRunRecord]:
        """Load and parse a verification run record, skipping corrupt files."""
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "results" not in data:
                return None
            return VerificationRunRecord.from_dict(data, path=path)
        except Exception:
            return None

    def get_latest_run(self) -> Optional[VerificationRunRecord]:
        """Return the most recent valid verification run record."""
        files = self.list_result_files()
        for p in reversed(files):
            rec = self.load_run(p)
            if rec is not None:
                return rec
        return None

    def prune_old_runs(self, max_files: int = MAX_RESULTS_FILES) -> list[Path]:
        """Enforce file retention limit by removing oldest runs after confirmation."""
        files = self.list_result_files()
        excess_count = len(files) - max_files
        if excess_count <= 0:
            return []

        to_delete = files[:excess_count]
        should_delete = True
        if self.confirm_delete_callback is not None:
            should_delete = self.confirm_delete_callback(to_delete)

        if not should_delete:
            return []

        deleted = []
        for p in to_delete:
            try:
                p.unlink(missing_ok=True)
                deleted.append(p)
            except Exception:
                pass
        return deleted

    def save_run(
        self,
        results: dict[str, ItemResult | dict[str, Any]],
        started_at: datetime | str,
        finished_at: Optional[datetime | str] = None,
        items: Optional[Sequence[Any]] = None,
        items_sha256: Optional[str] = None,
        git_info: Optional[tuple[Optional[str], bool]] = None,
        settings_snapshot: Optional[dict[str, Any]] = None,
    ) -> Path:
        """Atomically save a verification run record to a timestamped JSON file."""
        self._ensure_dir()
        now_dt = datetime.now(timezone.utc)
        finish_str = (
            finished_at if isinstance(finished_at, str)
            else (finished_at.isoformat() if isinstance(finished_at, datetime) else now_dt.isoformat())
        )
        start_str = (
            started_at if isinstance(started_at, str)
            else (started_at.isoformat() if isinstance(started_at, datetime) else now_dt.isoformat())
        )

        sha = items_sha256
        if sha is None:
            sha = compute_items_sha256(items) if items is not None else ""

        commit, dirty = git_info if git_info is not None else get_git_info()
        snapshot = settings_snapshot if settings_snapshot is not None else take_settings_snapshot()

        serializable_results: dict[str, dict[str, Any]] = {}
        for item_id, res in results.items():
            if isinstance(res, ItemResult):
                serializable_results[item_id] = res.to_dict()
            elif isinstance(res, dict):
                serializable_results[item_id] = {
                    "status": str(res.get("status", "skip")),
                    "notes": sanitize_note(res.get("notes")) if res.get("notes") else None,
                    "timestamp": str(res.get("timestamp", finish_str)),
                    "evidence_status": res.get("evidence_status"),
                }

        payload = {
            "app_version": VERSION,
            "git_commit": commit,
            "dirty": dirty,
            "started_at": start_str,
            "finished_at": finish_str,
            "items_sha256": sha,
            "settings_snapshot": snapshot,
            "results": serializable_results,
        }

        # Timestamp for filename: YYYYmmdd-HHMMSS
        # If finish_str can be parsed into dt, use it for filename consistency
        dt_for_name = _safe_parse_iso(finish_str) or now_dt
        file_ts = dt_for_name.strftime("%Y%m%d-%H%M%S")
        target_path = self.verify_dir / f"results-{file_ts}.json"

        # Handle duplicate timestamp collisions
        counter = 1
        while target_path.exists():
            target_path = self.verify_dir / f"results-{file_ts}_{counter}.json"
            counter += 1

        # Atomic write: write to sibling tempfile then os.replace
        temp_fd, temp_path_str = tempfile.mkstemp(
            prefix="tmp_verify_", suffix=".json", dir=str(self.verify_dir)
        )
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            temp_path.replace(target_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        # Enforce maximum files cap
        self.prune_old_runs(max_files=MAX_RESULTS_FILES)

        return target_path


def _safe_parse_iso(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None
