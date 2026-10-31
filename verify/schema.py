"""Schema and validation rules for ORBIT guided verification items.

Guarantees:
- Every item has an id, area, version_added, title, risk, needs_real_mode, requires,
  steps, expected, and negative flag.
- Allowed areas, risks, and evidence rule types are strictly closed sets.
- Every setting mentioned in requires.settings must exist in SPECS.
- Every evidence rule is structurally validated.
- Items are strictly ordered safe-first, then by area.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from config.settings_store import SPECS


ALLOWED_AREAS: tuple[str, ...] = (
    "smoke",
    "voice",
    "hud",
    "browser",
    "messaging",
    "reminders",
    "routines",
    "settings",
    "system",
    "laptop",
    "retrieval",
    "offline",
    "demo",
    "security",
)

ALLOWED_RISKS: tuple[str, ...] = (
    "safe",
    "touches_apps",
    "sends_data",
)

VALID_EVIDENCE_TYPES: tuple[str, ...] = (
    "audit_tool_success",
    "audit_tool_denied",
    "audit_no_tool_success",
    "audit_decision",
    "audit_source",
    "setting_equals",
    "file_exists_under_home",
    "all_of",
)


@dataclass
class ChecklistItem:
    """A single structured checklist item for live verification."""

    id: str
    area: str
    version_added: str
    title: str
    risk: str
    needs_real_mode: bool
    requires: dict[str, Any]
    steps: list[str]
    expected: str
    evidence: Optional[dict[str, Any]] = None
    negative: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "area": self.area,
            "version_added": self.version_added,
            "title": self.title,
            "risk": self.risk,
            "needs_real_mode": self.needs_real_mode,
            "requires": self.requires,
            "steps": list(self.steps),
            "expected": self.expected,
            "negative": self.negative,
        }
        if self.evidence is not None:
            d["evidence"] = self.evidence
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChecklistItem:
        return cls(
            id=str(data["id"]),
            area=str(data["area"]),
            version_added=str(data.get("version_added", "2.0.0")),
            title=str(data["title"]),
            risk=str(data.get("risk", "safe")),
            needs_real_mode=bool(data.get("needs_real_mode", False)),
            requires=dict(data.get("requires", {})),
            steps=list(data.get("steps", [])),
            expected=str(data.get("expected", "")),
            evidence=dict(data["evidence"]) if data.get("evidence") else None,
            negative=bool(data.get("negative", False)),
        )


def validate_evidence_rule(rule: Any, path: str = "evidence") -> list[str]:
    """Validate that an evidence rule conforms strictly to closed set schemas."""
    errors: list[str] = []
    if not isinstance(rule, dict):
        return [f"{path}: evidence rule must be a dictionary"]

    # Support flat format {"type": "..."} and nested format {"audit_tool_success": {...}}
    if "type" in rule:
        rule_type = rule["type"]
        rule_body = rule
    elif len(rule) == 1:
        rule_type, body_val = next(iter(rule.items()))
        rule_body = body_val if isinstance(body_val, dict) else {"type": rule_type, "value": body_val}
    else:
        return [f"{path}: evidence rule must have 'type' or single rule key, got {list(rule.keys())}"]

    if rule_type not in VALID_EVIDENCE_TYPES:
        return [f"{path}: unknown evidence rule type '{rule_type}'. Allowed: {VALID_EVIDENCE_TYPES}"]

    if rule_type == "all_of":
        subrules = rule_body.get("rules") or (rule_body if isinstance(rule_body, list) else [])
        if not isinstance(subrules, list) or not subrules:
            errors.append(f"{path}.all_of: must be a non-empty list of rules")
        else:
            for idx, subrule in enumerate(subrules):
                errors.extend(validate_evidence_rule(subrule, path=f"{path}.all_of[{idx}]"))
        return errors

    if not isinstance(rule_body, dict):
        return [f"{path}.{rule_type}: body must be a dictionary"]

    if rule_type in ("audit_tool_success", "audit_tool_denied"):
        if "tool" not in rule_body or not isinstance(rule_body["tool"], str):
            errors.append(f"{path}.{rule_type}: missing or non-string 'tool'")

    elif rule_type == "audit_no_tool_success":
        # No tool requirement
        pass

    elif rule_type == "audit_decision":
        if "decision" not in rule_body or not isinstance(rule_body["decision"], str):
            errors.append(f"{path}.{rule_type}: missing or non-string 'decision'")

    elif rule_type == "audit_source":
        if "source" not in rule_body or not isinstance(rule_body["source"], str):
            errors.append(f"{path}.{rule_type}: missing or non-string 'source'")

    elif rule_type == "setting_equals":
        if "key" not in rule_body or not isinstance(rule_body["key"], str):
            errors.append(f"{path}.setting_equals: missing or non-string 'key'")
        elif rule_body["key"] not in SPECS:
            errors.append(f"{path}.setting_equals: key '{rule_body['key']}' does not exist in SPECS")
        if "value" not in rule_body:
            errors.append(f"{path}.setting_equals: missing 'value'")

    elif rule_type == "file_exists_under_home":
        rel = rule_body.get("relative_path") or rule_body.get("relative")
        if not rel or not isinstance(rel, str):
            errors.append(f"{path}.file_exists_under_home: missing or non-string 'relative_path'")

    return errors


def validate_item(item: Mapping[str, Any]) -> list[str]:
    """Validate a single checklist item dictionary."""
    errors: list[str] = []
    item_id = item.get("id")
    if not item_id or not isinstance(item_id, str):
        errors.append("Item missing string 'id'")

    prefix = f"Item {item_id or 'unknown'}"

    area = item.get("area")
    if area not in ALLOWED_AREAS:
        errors.append(f"{prefix}: invalid area '{area}'. Allowed: {ALLOWED_AREAS}")

    risk = item.get("risk")
    if risk not in ALLOWED_RISKS:
        errors.append(f"{prefix}: invalid risk '{risk}'. Allowed: {ALLOWED_RISKS}")

    title = item.get("title")
    if not title or not isinstance(title, str):
        errors.append(f"{prefix}: missing or empty 'title'")

    steps = item.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, str) and s.strip() for s in steps):
        errors.append(f"{prefix}: 'steps' must be a non-empty list of non-empty strings")

    expected = item.get("expected")
    if not expected or not isinstance(expected, str):
        errors.append(f"{prefix}: missing or empty 'expected'")

    requires = item.get("requires", {})
    if not isinstance(requires, dict):
        errors.append(f"{prefix}: 'requires' must be a dictionary")
    else:
        settings_req = requires.get("settings", {})
        if not isinstance(settings_req, dict):
            errors.append(f"{prefix}: 'requires.settings' must be a dictionary")
        else:
            for key in settings_req.keys():
                if key not in SPECS:
                    errors.append(f"{prefix}: required setting '{key}' does not exist in SPECS")

        packages_req = requires.get("packages", [])
        if not isinstance(packages_req, list) or not all(isinstance(p, str) for p in packages_req):
            errors.append(f"{prefix}: 'requires.packages' must be a list of strings")

    evidence = item.get("evidence")
    if evidence is not None:
        errors.extend(validate_evidence_rule(evidence, path=f"{prefix}.evidence"))

    return errors


def compute_items_sha256(items_or_path: Union[str, Path, Sequence[Any]]) -> str:
    """Compute deterministic SHA-256 hex digest of items or items.json file."""
    if isinstance(items_or_path, (str, Path)):
        p = Path(items_or_path).resolve()
        if not p.is_file():
            return ""
        data = p.read_bytes()
        return hashlib.sha256(data).hexdigest()
    elif isinstance(items_or_path, Sequence):
        serializable = [
            it.to_dict() if isinstance(it, ChecklistItem) else it
            for it in items_or_path
        ]
        data = json.dumps(serializable, sort_keys=True).encode("utf-8")
        return hashlib.sha256(data).hexdigest()
    return ""


def load_items_from_json(path: Union[str, Path]) -> list[ChecklistItem]:
    """Load, parse, and instantiate ChecklistItem objects from a JSON file."""
    p = Path(path).resolve()
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected list of items in {path}, got {type(data).__name__}")
    return [ChecklistItem.from_dict(it) for it in data]


def sort_items(items: Sequence[ChecklistItem | Mapping[str, Any]]) -> list[Any]:
    """Sort items safe-first, then by area order, then by id."""
    area_order = {area: idx for idx, area in enumerate(ALLOWED_AREAS)}

    def _key(it: ChecklistItem | Mapping[str, Any]) -> tuple[int, int, str]:
        risk = it.risk if isinstance(it, ChecklistItem) else it.get("risk", "")
        area = it.area if isinstance(it, ChecklistItem) else it.get("area", "")
        iid = it.id if isinstance(it, ChecklistItem) else str(it.get("id", ""))
        risk_priority = 0 if risk == "safe" else 1
        area_priority = area_order.get(area, 999)
        return (risk_priority, area_priority, iid)

    return sorted(items, key=_key)


def validate_items_list(items: Sequence[ChecklistItem | Mapping[str, Any]]) -> list[str]:
    """Validate a complete collection of checklist items."""
    errors: list[str] = []
    seen_ids: set[str] = set()
    enabled_specs_seen: set[str] = set()

    for it in items:
        d = it.to_dict() if isinstance(it, ChecklistItem) else dict(it)
        errors.extend(validate_item(d))
        iid = it.id if isinstance(it, ChecklistItem) else str(it.get("id", ""))
        if iid in seen_ids:
            errors.append(f"Duplicate item id: '{iid}'")
        seen_ids.add(iid)

        # Track requires settings
        requires = it.requires if isinstance(it, ChecklistItem) else it.get("requires", {})
        for key in requires.get("settings", {}).keys():
            if key.endswith("_ENABLED"):
                enabled_specs_seen.add(key)

    # Check all *_ENABLED in SPECS appear in at least one item
    all_enabled_specs = {k for k in SPECS.keys() if k.endswith("_ENABLED")}
    missing_enabled = all_enabled_specs - enabled_specs_seen
    if missing_enabled:
        errors.append(f"The following *_ENABLED settings in SPECS do not appear in any item: {sorted(missing_enabled)}")

    # Check ordering: safe first, then by area
    sorted_copy = sort_items(items)
    for idx, (actual, expected) in enumerate(zip(items, sorted_copy)):
        act_id = actual.id if isinstance(actual, ChecklistItem) else actual.get("id")
        exp_id = expected.id if isinstance(expected, ChecklistItem) else expected.get("id")
        if act_id != exp_id:
            errors.append(
                f"Items are not sorted safe-first by area: at position {idx}, expected '{exp_id}' but found '{act_id}'"
            )
            break

    return errors
