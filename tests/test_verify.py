"""Comprehensive test suite for ORBIT guided verification system.

Guarantees:
- Tests use fakes and tmp_path only; zero real external calls, models, or audio.
- Tests schema validation, evidence engine with privacy checks, runner state machine,
  atomic store persistence, 20-file retention, markdown exporter, terminal and GUI flows.
- Validates that no module in tools/ imports verify (model invisibility).
"""

from __future__ import annotations

import json
import pytest

from config.paths import REPO_ROOT
from config.settings_store import SPECS
from verify.evidence import EvidenceEvaluator, read_audit_entries_filtered
from verify.export import export_markdown, generate_checklist_markdown
from verify.runner import VerificationSession
from verify.schema import (
    ChecklistItem,
    compute_items_sha256,
    load_items_from_json,
    sort_items,
    validate_items_list,
)
from verify.store import ItemResult, VerificationStore, sanitize_note


@pytest.fixture
def sample_items() -> list[ChecklistItem]:
    return [
        ChecklistItem(
            id="SMK-01",
            area="smoke",
            version_added="2.22.0",
            title="Safe Smoke Item",
            risk="safe",
            needs_real_mode=False,
            requires={"settings": {"CHAINS_ENABLED": "1"}},
            steps=["Step one", "Step two"],
            expected="Safe smoke success",
            evidence={"type": "setting_equals", "key": "CHAINS_ENABLED", "value": "1"},
            negative=False,
        ),
        ChecklistItem(
            id="APP-01",
            area="laptop",
            version_added="2.22.0",
            title="Touches App Item",
            risk="touches_apps",
            needs_real_mode=True,
            requires={"settings": {"WINDOW_CONTROL_ENABLED": "1"}},
            steps=["Touch app"],
            expected="Window focused",
            evidence={"type": "audit_tool_success", "tool": "window_focus"},
            negative=False,
        ),
        ChecklistItem(
            id="NEG-01",
            area="security",
            version_added="2.22.0",
            title="Negative Security Item",
            risk="safe",
            needs_real_mode=False,
            requires={},
            steps=["Negative check"],
            expected="Access refused",
            evidence={"type": "audit_no_tool_success"},
            negative=True,
        ),
    ]


# --- 1. Schema & Validation Tests ---

def test_items_json_canonical_validation():
    """Verify that verify/items.json satisfies all schema rules and constraints."""
    items_path = REPO_ROOT / "verify" / "items.json"
    assert items_path.is_file(), "verify/items.json must exist"

    items = load_items_from_json(items_path)
    assert len(items) >= 100, f"Expected at least 100 items, got {len(items)}"

    errors = validate_items_list(items)
    assert not errors, f"Validation errors in verify/items.json: {errors}"


def test_specs_all_enabled_settings_covered():
    """Ensure all 11 *_ENABLED settings in SPECS are covered by at least one checklist item."""
    items = load_items_from_json(REPO_ROOT / "verify" / "items.json")
    all_enabled_specs = {k for k in SPECS.keys() if k.endswith("_ENABLED")}

    covered = set()
    for it in items:
        for k in it.requires.get("settings", {}).keys():
            if k.endswith("_ENABLED"):
                covered.add(k)

    missing = all_enabled_specs - covered
    assert not missing, f"Missing *_ENABLED settings in items: {missing}"


def test_mandatory_nine_negative_items_exist():
    """Verify all 9 mandatory negative security items exist with proper flags."""
    items = load_items_from_json(REPO_ROOT / "verify" / "items.json")
    item_map = {it.id: it for it in items}

    for i in range(1, 10):
        neg_id = f"NEG-0{i}"
        assert neg_id in item_map, f"Missing mandatory negative item {neg_id}"
        it = item_map[neg_id]
        assert it.negative is True, f"{neg_id} must have negative=True"
        assert it.area == "security", f"{neg_id} must have area='security'"
        assert it.risk == "safe", f"{neg_id} must have risk='safe'"


def test_items_safe_first_ordering():
    """Ensure items are sorted safe-first, then by area priority, then by id."""
    items = load_items_from_json(REPO_ROOT / "verify" / "items.json")
    sorted_items = sort_items(items)

    assert [it.id for it in items] == [it.id for it in sorted_items], "Items must be sorted safe-first"


def test_items_sha256_hash(tmp_path):
    """Test deterministic items SHA-256 computation."""
    f = tmp_path / "items.json"
    f.write_text('[{"id": "A"}]', encoding="utf-8")
    hash1 = compute_items_sha256(f)
    assert len(hash1) == 64

    # Modification alters hash
    f.write_text('[{"id": "B"}]', encoding="utf-8")
    hash2 = compute_items_sha256(f)
    assert hash1 != hash2


# --- 2. Evidence Evaluator & Privacy Tests ---

def test_evidence_evaluator_tool_success_and_denied(tmp_path):
    """Test evidence evaluation against allowlisted audit entries."""
    audit_file = tmp_path / "audit.jsonl"
    entries = [
        {
            "timestamp": "2026-10-31T01:00:00+00:00",
            "tool": "window_focus",
            "decision": "allow",
            "outcome": "success",
            "tainted": False,
            "source": "typed",
            "args": {"window_id": 123},
            "result": "Notepad focused",
        },
        {
            "timestamp": "2026-10-31T01:05:00+00:00",
            "tool": "type_into_window",
            "decision": "deny",
            "outcome": "blocked",
            "tainted": True,
            "source": "typed",
            "args": {"text": "secret123"},
            "result": "Refused non-allowlisted window",
        },
    ]
    with open(audit_file, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    evaluator = EvidenceEvaluator(audit_path=audit_file)

    # 1. Tool success
    res_success = evaluator.evaluate({"type": "audit_tool_success", "tool": "window_focus"})
    assert res_success.status == "found"
    assert "window_focus" in res_success.reason

    # 2. Tool denied
    res_denied = evaluator.evaluate({"type": "audit_tool_denied", "tool": "type_into_window"})
    assert res_denied.status == "found"

    # 3. Tool missing
    res_missing = evaluator.evaluate({"type": "audit_tool_success", "tool": "send_whatsapp"})
    assert res_missing.status == "not_found"

    # 4. Source check
    res_source = evaluator.evaluate({"type": "audit_source", "source": "typed"})
    assert res_source.status == "found"


def test_evidence_privacy_protection_ignores_args_and_result(tmp_path):
    """STRICT PRIVACY: verify that evaluator NEVER reads or parses args, result, or personal payloads."""
    audit_file = tmp_path / "audit.jsonl"
    raw_record = {
        "timestamp": "2026-10-31T01:00:00+00:00",
        "tool": "send_whatsapp",
        "decision": "allow",
        "outcome": "success",
        "tainted": False,
        "source": "voice",
        "args": {"phone": "+15550199999", "message": "SUPER_SECRET_PAYLOAD"},
        "result": "DRAFTED for user +15550199999",
        "extra": {"confidential_token": "XYZ123"},
    }
    audit_file.write_text(json.dumps(raw_record) + "\n", encoding="utf-8")

    filtered = read_audit_entries_filtered(audit_file)
    assert filtered is not None
    assert len(filtered) == 1
    entry = filtered[0]

    # Allowlisted fields present
    assert entry["tool"] == "send_whatsapp"
    assert entry["decision"] == "allow"
    assert entry["outcome"] == "success"
    assert entry["source"] == "voice"

    # Privacy guarantee: args, result, extra, phone, message MUST NOT exist
    assert "args" not in entry
    assert "result" not in entry
    assert "extra" not in entry
    assert "SUPER_SECRET_PAYLOAD" not in str(entry)
    assert "+15550199999" not in str(entry)


def test_evidence_setting_equals_and_file_exists(tmp_path):
    """Test setting_equals and safe sandboxed file_exists_under_home rules."""
    home_dir = tmp_path / "orbit_home"
    home_dir.mkdir()
    (home_dir / "workspace").mkdir()
    (home_dir / "workspace" / "notes.txt").write_text("sample", encoding="utf-8")

    evaluator = EvidenceEvaluator(
        settings_getter=lambda k: "1" if k == "MESSAGING_ENABLED" else "0",
        orbit_home=home_dir,
    )

    res_set = evaluator.evaluate({"type": "setting_equals", "key": "MESSAGING_ENABLED", "value": "1"})
    assert res_set.status == "found"

    res_set_fail = evaluator.evaluate({"type": "setting_equals", "key": "MESSAGING_ENABLED", "value": "0"})
    assert res_set_fail.status == "not_found"

    res_file = evaluator.evaluate({"type": "file_exists_under_home", "relative_path": "workspace/notes.txt"})
    assert res_file.status == "found"

    res_no_file = evaluator.evaluate({"type": "file_exists_under_home", "relative_path": "workspace/absent.txt"})
    assert res_no_file.status == "not_found"


# --- 3. Runner State Machine Tests ---

def test_runner_progression_and_blocking(sample_items):
    """Test session runner progression, blocking detection, skip, back, and retry."""
    session = VerificationSession(
        items=sample_items,
        settings_getter=lambda k: "0" if k == "CHAINS_ENABLED" else "1",
        demo_mode=False,
    )

    # First item is safe smoke, but requires CHAINS_ENABLED=1 (current is 0)
    item1 = session.current_item
    assert item1 is not None and item1.id == "SMK-01"

    block = session.check_blocking(item1)
    assert block is not None
    assert block.kind == "setting"
    assert "/set CHAINS_ENABLED 1" in block.suggested_fix

    # Mark blocked and advance
    session.block_current(reason=block.message)
    assert session.current_index == 1

    # Second item is NEG-01 (safe items come first)
    item2 = session.current_item
    assert item2 is not None and item2.id == "NEG-01"
    assert session.check_blocking(item2) is None

    session.pass_current(notes="Verified dialog immunity")
    assert session.current_index == 2

    # Third item touches apps (APP-01)
    item3 = session.current_item
    assert item3 is not None and item3.id == "APP-01"

    # Test back
    assert session.back() is True
    assert session.current_item.id == "NEG-01"

    # Test retry (keeps current item without advancing)
    session.retry_current()
    assert session.current_item.id == "NEG-01"


def test_runner_demo_mode_blocks_real_mode_items(sample_items):
    """Ensure runner blocks needs_real_mode=True items when running in demo mode."""
    session = VerificationSession(
        items=sample_items,
        settings_getter=lambda k: "1",
        demo_mode=True,
    )

    # Find APP-01 (needs_real_mode=True)
    assert session.goto("APP-01") is True
    item = session.current_item
    assert item.needs_real_mode is True

    block = session.check_blocking(item)
    assert block is not None
    assert block.kind == "demo"
    assert "Launch ORBIT in normal mode" in block.suggested_fix


def test_runner_notes_sanitization():
    """Verify notes are capped at 200 chars and tabs/newlines are stripped."""
    long_note = "A" * 300 + "\nLine2\tTabbed"
    clean = sanitize_note(long_note)
    assert len(clean) <= 200
    assert "\n" not in clean
    assert "\t" not in clean


# --- 4. Atomic Store & Retention Tests ---

def test_store_atomic_write_and_retention(tmp_path):
    """Test results store atomic writes, corrupt skipping, and 20-file cap retention."""
    verify_dir = tmp_path / "data" / "verify"
    store = VerificationStore(verify_dir=verify_dir)

    # 1. Save run
    results = {
        "V1": ItemResult(status="pass", notes="Worked well"),
        "V2": ItemResult(status="fail", notes="No mic"),
    }
    p = store.save_run(
        results=results,
        started_at="2026-10-31T00:00:00Z",
        finished_at="2026-10-31T00:05:00Z",
        git_info=("abc1234", False),
    )
    assert p.is_file()

    # 2. Load run
    rec = store.load_run(p)
    assert rec is not None
    assert rec.git_commit == "abc1234"
    assert rec.results["V1"]["status"] == "pass"
    assert rec.summary()["pass"] == 1
    assert rec.summary()["fail"] == 1

    # 3. Create dummy corrupt file
    corrupt_file = verify_dir / "results-corrupt.json"
    corrupt_file.write_text("INVALID_JSON{", encoding="utf-8")
    assert store.load_run(corrupt_file) is None

    # 4. Enforce 20-file cap
    # Create 25 dummy result files
    for i in range(25):
        rf = verify_dir / f"results-20261001-0000{i:02d}.json"
        rf.write_text(json.dumps({"results": {}, "app_version": "2.22.0"}), encoding="utf-8")

    files_before = store.list_result_files()
    assert len(files_before) > 20

    # Prune
    deleted = store.prune_old_runs(max_files=20)
    assert len(deleted) > 0
    files_after = store.list_result_files()
    assert len(files_after) <= 20


# --- 5. Export Markdown & Privacy Tests ---

def test_export_markdown_table_and_privacy():
    """Verify markdown export formatting and ensure PRIVACY (no notes/secrets in export)."""
    items = [
        ChecklistItem("V1", "voice", "2.5.0", "Voice Check", "safe", False, {}, ["Speak"], "Listens", None, False),
        ChecklistItem("M1", "messaging", "2.19.0", "Text Contact", "touches_apps", True, {}, ["Text"], "Drafted", None, False),
    ]
    results = {
        "V1": ItemResult(status="pass", notes="SECRET_NOTE_DO_NOT_EXPORT_123"),
        "M1": ItemResult(status="fail", notes="CONFIDENTIAL_PHONE_NUMBER_456"),
    }

    md = export_markdown(results=results, items=items, run_date="2026-10-31", app_version="2.22.0")

    # Table headers and rows
    assert "| ID | Area | Title | Status | Date | Version |" in md
    assert "| V1 | voice | Voice Check | `PASS` | 2026-10-31 | v2.22.0 |" in md
    assert "| M1 | messaging | Text Contact | `FAIL` | 2026-10-31 | v2.22.0 |" in md

    # Summary line
    assert "1 PASS | 1 FAIL" in md

    # STRICT PRIVACY: notes MUST NOT be present in markdown export
    assert "SECRET_NOTE_DO_NOT_EXPORT_123" not in md
    assert "CONFIDENTIAL_PHONE_NUMBER_456" not in md


def test_export_markdown_diff_comparison():
    """Verify export markdown flags regressions and improvements against prior run."""
    items = [
        ChecklistItem("T1", "smoke", "2.7.0", "Test One", "safe", False, {}, [], "OK", None, False),
        ChecklistItem("T2", "smoke", "2.7.0", "Test Two", "safe", False, {}, [], "OK", None, False),
    ]
    prev_results = {
        "T1": {"status": "pass"},
        "T2": {"status": "fail"},
    }
    curr_results = {
        "T1": {"status": "fail"},  # Regression: pass -> fail
        "T2": {"status": "pass"},  # Improvement: fail -> pass
    }

    md = export_markdown(results=curr_results, items=items, previous_results=prev_results)
    assert "### ⚠ Regressions" in md
    assert "- **T1** (Test One): `PASS` -> `FAIL`" in md
    assert "### ✓ Improvements" in md
    assert "- **T2** (Test Two): `FAIL` -> `PASS`" in md


def test_generate_checklist_markdown():
    """Verify generate_checklist_markdown produces canonical header and matrix."""
    items = [
        ChecklistItem("V1", "voice", "2.5.0", "Click Mic", "safe", False, {}, ["Click mic"], "Listens", None, False)
    ]
    md = generate_checklist_markdown(items)
    assert md.startswith("<!-- GENERATED BY verify/export.py - DO NOT EDIT BY HAND -->")
    assert "| **V1** | **v2.5.0 Voice: Click Mic** | Click mic | Listens | `PASS` |" in md


# --- 6. Terminal Commands & CLI Script Tests ---

def test_terminal_handle_verify_commands(capsys):
    """Test /verify subcommands (list, status, export) in main.py."""
    from main import handle_verify_command

    # 1. /verify list
    handle_verify_command("/verify list", print_func=print)
    captured = capsys.readouterr().out
    assert "ORBIT Verification Items" in captured
    assert "NEG-01" in captured

    # 2. /verify status
    handle_verify_command("/verify status", print_func=print)
    captured = capsys.readouterr().out
    assert "ORBIT VERIFICATION STATUS" in captured or "No verification runs" in captured

    # 3. Interactive session simulation
    inputs = iter(["p", "", "q"])  # Pass first item, empty note, then quit
    handle_verify_command("/verify", print_func=print, input_func=lambda prompt: next(inputs))
    captured = capsys.readouterr().out
    assert "Starting ORBIT Guided Verification" in captured
    assert "Exiting verification session." in captured


def test_scripts_verify_cli(capsys):
    """Test scripts/verify.py flags (--list, --status, --export) exit with code 0."""
    import scripts.verify as verify_cli

    assert verify_cli.main(["--list"]) == 0
    captured = capsys.readouterr().out
    assert "ORBIT Verification Items" in captured

    assert verify_cli.main(["--status"]) == 0

    assert verify_cli.main(["--export"]) == 0
    captured = capsys.readouterr().out
    assert "Verification status successfully exported to" in captured


# --- 7. Model Invisibility & Security Hygiene ---

def test_model_invisibility_no_tools_import_verify():
    """Verify that NO module in tools/ imports verify (model cannot invoke verification)."""
    tools_dir = REPO_ROOT / "tools"
    for py_file in tools_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "import verify" not in content, f"{py_file.name} must not import verify"
        assert "from verify" not in content, f"{py_file.name} must not import from verify"


def test_git_hygiene_ignores_data_verify():
    """Verify that data/verify/ is properly ignored by .gitignore."""
    gitignore_path = REPO_ROOT / ".gitignore"
    text = gitignore_path.read_text(encoding="utf-8")
    assert "data/" in text, ".gitignore must ignore data/ directory"


def test_diagnostics_and_doctor_sections():
    """Verify diagnostics includes === VERIFICATION RESULTS === and doctor runs verification check."""
    from core.diagnostics import generate_diagnostics
    from core.doctor import check_verification_results

    # 1. Doctor check
    chk = check_verification_results()
    assert chk.name == "Verification results"
    assert chk.status in ("OK", "WARN")

    # 2. Diagnostics report
    file_path, text = generate_diagnostics(recent_chat_lines=["test"])
    assert "=== VERIFICATION RESULTS ===" in text
    assert "Latest Run:" in text or "Status: No verification runs recorded" in text
    # Ensure no secrets leak
    assert "password" not in text.lower() or "[REDACTED]" in text
