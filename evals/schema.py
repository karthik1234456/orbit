"""Schema definitions and strict loading/validation for evaluation cases."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Optional, Union

from evals.checks import CHECK_TYPES

CATEGORIES = (
    "chat",
    "utility",
    "files",
    "apps",
    "memory",
    "rag",
    "safety",
    "multistep",
    "recovery",
)

VALID_CASE_KEYS = {
    "id",
    "category",
    "kind",
    "turns",
    "prompt",
    "setup",
    "confirm",
    "expect_tools",
    "checks",
    "golden",
    "notes",
}

VALID_KINDS = {"normal", "refusal", "denial"}

VALID_SETUP_KEYS = {"files", "docs", "index_docs", "memories"}

CHECK_ALLOWED_KEYS = {
    "file_exists": {"type", "path"},
    "file_absent": {"type", "path"},
    "file_contains": {"type", "path", "text"},
    "final_contains_any": {"type", "texts", "turn"},
    "final_not_contains": {"type", "texts", "turn"},
    "tool_called": {"type", "tool"},
    "tool_not_called": {"type", "tool"},
    "tool_executed": {"type", "tool"},
    "tool_not_executed": {"type", "tool"},
    "no_tools_called": {"type"},
    "max_tool_calls": {"type", "n"},
    "launched": {"type", "kind", "contains"},
    "nothing_launched": {"type"},
    "memory_contains": {"type", "text"},
    "memory_absent": {"type", "text"},
    "memory_count_at_most": {"type", "n"},
    "refusal_language": {"type", "turn"},
    "not_found_language": {"type", "turn"},
    "no_escape": {"type"},
    "confirm_requests_warned": {"type"},
    "confirmed": {"tool", "type"},
    "documents_consulted": {"type"},
}


class CaseError(Exception):
    """Raised when an evaluation case violates validation constraints."""

    pass


@dataclass
class EvalCase:
    """Represents a validated multi-turn or single-turn evaluation test case."""

    id: str
    category: str
    turns: list[str]
    setup: dict = field(default_factory=dict)
    confirm: str = "approve"
    kind: str = "normal"
    expect_tools: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    golden: list[dict] = field(default_factory=list)
    notes: Optional[str] = None


def _is_safe_relative_path(path_str: Any) -> bool:
    """Verify that a path string is relative with no '..', drive letters, or leading slashes."""
    if not isinstance(path_str, str) or not path_str.strip():
        return False
    s = path_str.strip()
    if s.startswith(("/", "\\")):
        return False
    if re.match(r"^[a-zA-Z]:", s):
        return False
    parts = re.split(r"[/\\]+", s)
    if any(p == ".." for p in parts):
        return False
    if Path(s).is_absolute():
        return False
    return True


def _validate_and_parse_case(raw: dict, seen_ids: set[str]) -> EvalCase:
    """Strictly validate a raw case dictionary and return an EvalCase instance."""
    if not isinstance(raw, dict):
        raise CaseError("Case must be a JSON dictionary object.")

    # 1. Extra unknown keys at root
    case_id = str(raw.get("id", "<unknown>")).strip()
    extra_keys = set(raw.keys()) - VALID_CASE_KEYS
    if extra_keys:
        raise CaseError(f"Case '{case_id}': unknown extra key(s): {sorted(extra_keys)}")

    # 2. Case ID
    if not raw.get("id") or not isinstance(raw["id"], str) or not raw["id"].strip():
        raise CaseError(f"Case '{case_id}': missing or empty 'id'")
    case_id = raw["id"].strip()
    if case_id in seen_ids:
        raise CaseError(f"Case '{case_id}': duplicate case id")
    seen_ids.add(case_id)

    # 3. Category
    category = raw.get("category")
    if category not in CATEGORIES:
        raise CaseError(f"Case '{case_id}': unknown category '{category}'")

    # 4. Turns / Prompt
    turns: list[str] = []
    if "turns" in raw:
        if not isinstance(raw["turns"], list) or len(raw["turns"]) == 0:
            raise CaseError(f"Case '{case_id}': 'turns' must be a non-empty list of strings")
        for idx, t in enumerate(raw["turns"]):
            if not isinstance(t, str) or not t.strip():
                raise CaseError(f"Case '{case_id}': turn {idx} must be a non-empty string")
            turns.append(t)
    elif "prompt" in raw:
        prompt_val = raw["prompt"]
        if not isinstance(prompt_val, str) or not prompt_val.strip():
            raise CaseError(f"Case '{case_id}': 'prompt' must be a non-empty string")
        turns = [prompt_val]
    else:
        raise CaseError(f"Case '{case_id}': missing required 'turns' or 'prompt'")

    # 5. Confirm
    confirm = raw.get("confirm", "approve")
    if confirm not in ("approve", "deny"):
        raise CaseError(
            f"Case '{case_id}': invalid confirm value '{confirm}', expected 'approve' or 'deny'"
        )

    # 5b. Kind
    kind = raw.get("kind", "normal")
    if kind not in VALID_KINDS:
        raise CaseError(
            f"Case '{case_id}': invalid kind value '{kind}', expected one of {sorted(VALID_KINDS)}"
        )

    # 6. Golden
    golden: list[dict] = []
    if "golden" in raw and raw["golden"] is not None:
        if not isinstance(raw["golden"], list):
            raise CaseError(f"Case '{case_id}': 'golden' must be a list of turn records")
        if len(raw["golden"]) != len(turns):
            raise CaseError(
                f"Case '{case_id}': golden length ({len(raw['golden'])}) does not match turns length ({len(turns)})"
            )
        golden = raw["golden"]

    # 7. Expect tools
    expect_tools = raw.get("expect_tools", [])
    if not isinstance(expect_tools, list) or not all(isinstance(t, str) for t in expect_tools):
        raise CaseError(f"Case '{case_id}': 'expect_tools' must be a list of tool names")

    # 8. Setup
    setup = raw.get("setup", {})
    if not isinstance(setup, dict):
        raise CaseError(f"Case '{case_id}': 'setup' must be a dictionary")
    extra_setup_keys = set(setup.keys()) - VALID_SETUP_KEYS
    if extra_setup_keys:
        raise CaseError(
            f"Case '{case_id}': unknown key(s) in setup: {sorted(extra_setup_keys)}"
        )

    if "files" in setup:
        if not isinstance(setup["files"], dict):
            raise CaseError(f"Case '{case_id}': setup.files must be a dictionary")
        for fpath in setup["files"].keys():
            if not _is_safe_relative_path(fpath):
                raise CaseError(
                    f"Case '{case_id}': invalid path in setup.files: '{fpath}'"
                )

    if "docs" in setup:
        if not isinstance(setup["docs"], dict):
            raise CaseError(f"Case '{case_id}': setup.docs must be a dictionary")
        for dpath in setup["docs"].keys():
            if not _is_safe_relative_path(dpath):
                raise CaseError(
                    f"Case '{case_id}': invalid path in setup.docs: '{dpath}'"
                )

    # 9. Checks
    checks = raw.get("checks", [])
    if not isinstance(checks, list):
        raise CaseError(f"Case '{case_id}': 'checks' must be a list of check dicts")

    for idx, check in enumerate(checks):
        if not isinstance(check, dict):
            raise CaseError(f"Case '{case_id}': check #{idx} must be a dictionary")
        ctype = check.get("type")
        if not ctype or ctype not in CHECK_TYPES:
            raise CaseError(f"Case '{case_id}': unknown check type '{ctype}'")

        # Required keys for check type
        required_keys = CHECK_TYPES[ctype]
        missing_keys = required_keys - set(check.keys())
        if missing_keys:
            raise CaseError(
                f"Case '{case_id}': check '{ctype}' missing required key(s): {sorted(missing_keys)}"
            )

        # Extra unknown keys for check type
        allowed_keys = CHECK_ALLOWED_KEYS.get(ctype, required_keys | {"type"})
        extra_check_keys = set(check.keys()) - allowed_keys
        if extra_check_keys:
            raise CaseError(
                f"Case '{case_id}': check '{ctype}' has unknown extra key(s): {sorted(extra_check_keys)}"
            )

        # Validate check paths
        if "path" in check:
            p = check["path"]
            if not _is_safe_relative_path(p):
                raise CaseError(
                    f"Case '{case_id}': check '{ctype}' has invalid path: '{p}'"
                )

    notes = raw.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise CaseError(f"Case '{case_id}': 'notes' must be a string")

    return EvalCase(
        id=case_id,
        category=category,
        turns=turns,
        setup=setup,
        confirm=confirm,
        kind=kind,
        expect_tools=expect_tools,
        checks=checks,
        golden=golden,
        notes=notes,
    )


def load_cases(path_or_dir: Union[str, Path]) -> list[EvalCase]:
    """Read and validate evaluation cases from a JSON file or all *.json files in a directory."""
    path = Path(path_or_dir)
    if not path.exists():
        raise FileNotFoundError(f"Case path does not exist: {path}")

    files: list[Path] = []
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(path.glob("*.json"))

    loaded_cases: list[EvalCase] = []
    seen_ids: set[str] = set()

    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise CaseError(f"Failed to parse JSON file '{f}': {exc}") from exc

        if isinstance(data, dict):
            raw_list = [data]
        elif isinstance(data, list):
            raw_list = data
        else:
            raise CaseError(f"File '{f}' must contain a JSON object or array of objects.")

        for item in raw_list:
            case = _validate_and_parse_case(item, seen_ids)
            loaded_cases.append(case)

    return loaded_cases


def compute_cases_sha256(path_or_dir: Union[str, Path]) -> str:
    """Compute deterministic SHA-256 hash of loaded case file(s)."""
    import hashlib

    path = Path(path_or_dir)
    if not path.exists():
        return ""

    files = [path] if path.is_file() else sorted(path.glob("*.json"))
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


_ALL_KNOWN_CASES_CACHE: Optional[dict[str, EvalCase]] = None


def get_known_case_map() -> dict[str, EvalCase]:
    """Return a mapping of case ID to EvalCase across all standard and holdout directories."""
    global _ALL_KNOWN_CASES_CACHE
    if _ALL_KNOWN_CASES_CACHE is not None:
        return _ALL_KNOWN_CASES_CACHE

    case_map: dict[str, EvalCase] = {}
    candidate_dirs = [
        Path("evals/cases"),
        Path("evals/holdout"),
        Path("evals/holdout2"),
        Path("evals/holdout3"),
    ]
    for d in candidate_dirs:
        if d.exists():
            try:
                for c in load_cases(d):
                    case_map[c.id] = c
            except Exception:
                pass

    _ALL_KNOWN_CASES_CACHE = case_map
    return case_map


def lookup_case_kind(case_id: str, case_obj: Optional[Any] = None) -> str:
    """Determine the case kind ('refusal', 'denial', or 'normal') for a case.

    Checks the case object first; falls back to looking up by ID across known case files.
    Prints a warning if the case ID is unrecognized and defaults to 'normal'.
    """
    if case_obj is not None:
        k = getattr(case_obj, "kind", None)
        if isinstance(case_obj, dict):
            k = case_obj.get("kind", k)
        if k in VALID_KINDS:
            return str(k)

    known_cases = get_known_case_map()
    if case_id in known_cases:
        return known_cases[case_id].kind

    import sys
    print(f"Warning: Unknown case ID '{case_id}' when resolving case kind; defaulting to 'normal'.", file=sys.stderr)
    return "normal"
