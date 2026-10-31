"""Core verification session runner and state machine for ORBIT.

Manages item progression, auto-blocking rules, evidence polling, and local results recording.

Guarantees:
- Strictly decoupled from GUI (no tkinter imports).
- Runner never calls tools, models, dialogs, or the audit logger directly.
- Evaluates prerequisites (settings, packages, platform, demo mode) and auto-blocks
  with actionable, copyable fix instructions.
- Provides 'back', 'skip', 'retry', 'pass', 'fail', and note sanitization (<= 200 chars).
- Evidence only suggests; user always decides final status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

from config.paths import is_demo_mode
from config.settings_store import get_effective_setting
from verify.evidence import EvidenceEvaluator, EvidenceResult
from verify.schema import ChecklistItem, sort_items
from verify.store import ItemResult, VerificationStore, sanitize_note


@dataclass
class ItemBlockReason:
    """Explains why an item is currently blocked from verification."""

    kind: str  # "setting", "package", "platform", "demo"
    message: str
    suggested_fix: str


class VerificationSession:
    """State machine governing a guided verification workflow."""

    def __init__(
        self,
        items: Sequence[ChecklistItem | Mapping[str, Any]],
        store: Optional[VerificationStore] = None,
        evaluator: Optional[EvidenceEvaluator] = None,
        clock: Optional[Callable[[], datetime]] = None,
        settings_getter: Optional[Callable[[str], Any]] = None,
        demo_mode: Optional[bool] = None,
    ) -> None:
        self.raw_items = items
        self.items: list[ChecklistItem] = [
            it if isinstance(it, ChecklistItem) else ChecklistItem.from_dict(it)
            for it in sort_items(items)
        ]
        self.store = store or VerificationStore()
        self.evaluator = evaluator or EvidenceEvaluator()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.settings_getter = settings_getter or (lambda k: get_effective_setting(k).effective_value)
        self.demo_mode = is_demo_mode() if demo_mode is None else demo_mode

        self.started_at: datetime = self.clock()
        self.results: dict[str, ItemResult] = {}
        self.current_index: int = 0

    @property
    def total_items(self) -> int:
        return len(self.items)

    @property
    def current_item(self) -> Optional[ChecklistItem]:
        if 0 <= self.current_index < len(self.items):
            return self.items[self.current_index]
        return None

    def is_finished(self) -> bool:
        return self.current_index >= len(self.items)

    def check_blocking(self, item: ChecklistItem) -> Optional[ItemBlockReason]:
        """Determine if an item is blocked by settings, packages, platform, or demo mode."""
        # 1. Demo mode block
        if item.needs_real_mode and self.demo_mode:
            return ItemBlockReason(
                kind="demo",
                message=f"Item '{item.id}' requires real execution mode (disabled in demo mode)",
                suggested_fix="Launch ORBIT in normal mode (python main.py or python -m ui)",
            )

        # 2. Platform block
        if item.area in ("system", "laptop") and sys.platform != "win32":
            return ItemBlockReason(
                kind="platform",
                message=f"Item '{item.id}' requires Windows (detected: {sys.platform})",
                suggested_fix="Run this check on Windows 10/11",
            )

        # 3. Settings block
        req_settings = item.requires.get("settings", {})
        for key, exp_val in req_settings.items():
            try:
                curr = self.settings_getter(key)
            except Exception:
                curr = None

            curr_str = str(curr).strip().lower() if curr is not None else ""
            exp_str = str(exp_val).strip().lower() if exp_val is not None else ""

            if curr_str != exp_str and curr != exp_val:
                return ItemBlockReason(
                    kind="setting",
                    message=f"Setting '{key}' is '{curr}', but requires '{exp_val}'",
                    suggested_fix=f"/set {key} {exp_val}",
                )

        # 4. Package dependencies block
        req_packages = item.requires.get("packages", [])
        for pkg in req_packages:
            spec = importlib.util.find_spec(pkg)
            if spec is None:
                return ItemBlockReason(
                    kind="package",
                    message=f"Required package '{pkg}' is not installed",
                    suggested_fix=f"pip install {pkg}",
                )

        return None

    def poll_evidence(self) -> EvidenceResult:
        """Evaluate evidence rule for current item against audit log and settings."""
        item = self.current_item
        if not item or not item.evidence:
            return EvidenceResult("unavailable", "No evidence rule defined for this item")
        return self.evaluator.evaluate(item.evidence, since=self.started_at)

    def record_decision(
        self,
        status: str,
        notes: Optional[str] = None,
        evidence_status: Optional[str] = None,
    ) -> None:
        """Record status decision for the current item and advance."""
        item = self.current_item
        if not item:
            return

        clean_notes = sanitize_note(notes) if notes else None
        now_iso = self.clock().isoformat()

        self.results[item.id] = ItemResult(
            status=status,
            notes=clean_notes,
            timestamp=now_iso,
            evidence_status=evidence_status,
        )
        self.current_index += 1

    def pass_current(self, notes: Optional[str] = None, evidence_status: Optional[str] = None) -> None:
        self.record_decision("pass", notes, evidence_status)

    def fail_current(self, notes: Optional[str] = None, evidence_status: Optional[str] = None) -> None:
        self.record_decision("fail", notes, evidence_status)

    def skip_current(self, notes: Optional[str] = None, evidence_status: Optional[str] = None) -> None:
        self.record_decision("skip", notes, evidence_status)

    def block_current(self, reason: str, notes: Optional[str] = None) -> None:
        combined_notes = f"{reason} - {notes}" if notes else reason
        self.record_decision("blocked", combined_notes)

    def retry_current(self) -> None:
        """Keep current item pending without advancing."""
        item = self.current_item
        if item and item.id in self.results:
            del self.results[item.id]

    def back(self) -> bool:
        """Move back to the previous item. Returns True if position changed."""
        if self.current_index > 0:
            self.current_index -= 1
            return True
        return False

    def goto(self, target: int | str) -> bool:
        """Jump to specific index or item ID."""
        if isinstance(target, int):
            if 0 <= target < len(self.items):
                self.current_index = target
                return True
        elif isinstance(target, str):
            for idx, it in enumerate(self.items):
                if it.id == target:
                    self.current_index = idx
                    return True
        return False

    def get_summary(self) -> dict[str, int]:
        """Return counts of pass, fail, skip, blocked, pending."""
        counts = {"pass": 0, "fail": 0, "skip": 0, "blocked": 0, "pending": 0, "total": len(self.items)}
        for it in self.items:
            res = self.results.get(it.id)
            st = res.status if res else "pending"
            counts[st] = counts.get(st, 0) + 1
        return counts

    def save(self) -> Optional[Any]:
        """Save session results using store."""
        if not self.results:
            return None
        return self.store.save_run(
            results=self.results,
            started_at=self.started_at,
            finished_at=self.clock(),
            items=self.items,
        )
