"""Tests for RoutineStore: limits, name rules, upsert, corruption recovery, and SQL injection safety."""

from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from routines.store import RoutineLimit, RoutineStore, RoutineStoreError


class FakeClock:
    def __init__(self, now_iso: str = "2026-10-27T12:00:00Z"):
        self.now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))

    def __call__(self) -> datetime:
        return self.now_dt


def test_store_creation_and_schema(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    clock = FakeClock()
    store = RoutineStore(path=str(db_file), clock=clock)

    assert store.list() == []
    # Verify table schema
    with sqlite3.connect(str(db_file)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routines'")
        assert cursor.fetchone() is not None
        cursor.execute("PRAGMA table_info(routines)")
        cols = {row[1]: row[2] for row in cursor.fetchall()}
        assert "name" in cols
        assert "display_name" in cols
        assert "steps_json" in cols
        assert "schema_version" in cols
        assert "created_at" in cols
        assert "last_run_at" in cols
        assert "run_count" in cols


def test_name_rules_valid(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    store = RoutineStore(path=str(db_file), clock=FakeClock())

    valid_names = [
        "morning",
        "Morning Work",
        "dev-setup_1",
        "  daily review  ",  # trimmed
        "A" * 30,           # 30 chars
    ]

    for name in valid_names:
        r = store.save(display_name=name, steps=[{"tool": "open_application", "args": {"name": "notepad"}}])
        assert r.name == " ".join(name.strip().lower().split())
        assert store.get(name) is not None


def test_name_rules_invalid(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    store = RoutineStore(path=str(db_file), clock=FakeClock())
    steps = [{"tool": "open_application", "args": {"name": "notepad"}}]

    # Empty or whitespace only
    with pytest.raises(ValueError, match="1 and 30 characters"):
        store.save(display_name="", steps=steps)
    with pytest.raises(ValueError, match="1 and 30 characters"):
        store.save(display_name="   ", steps=steps)

    # Longer than 30 characters after trim
    with pytest.raises(ValueError, match="1 and 30 characters"):
        store.save(display_name="a" * 31, steps=steps)

    # Disallowed characters
    for bad in ["routine; rm -rf", "study & work", "my|routine", "test`run", "cost$10", "a<b", "http://google.com"]:
        with pytest.raises(ValueError, match="letters, digits, spaces"):
            store.save(display_name=bad, steps=steps)

    # Must not start with reserved words
    reserved = ["all", "list", "show", "delete", "create", "make", "save", "run", "start", "open", "close", "search"]
    for word in reserved:
        with pytest.raises(ValueError, match="must not start with"):
            store.save(display_name=f"{word} routine", steps=steps)
        with pytest.raises(ValueError, match="must not start with"):
            store.save(display_name=word.upper(), steps=steps)


def test_routine_limit(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    store = RoutineStore(path=str(db_file), clock=FakeClock())
    steps = [{"tool": "open_application", "args": {"name": "notepad"}}]

    # Can save up to 20 routines
    for i in range(1, 21):
        store.save(display_name=f"routine_{i}", steps=steps)

    assert len(store.list()) == 20

    # 21st routine raises RoutineLimit
    with pytest.raises(RoutineLimit, match="Maximum routine limit reached"):
        store.save(display_name="routine_21", steps=steps)


def test_upsert_overwrite(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    clock = FakeClock("2026-10-27T10:00:00Z")
    store = RoutineStore(path=str(db_file), clock=clock)

    step1 = [{"tool": "open_application", "args": {"name": "notepad"}}]
    r1 = store.save(display_name="Morning", steps=step1)
    assert r1.created_at == "2026-10-27T10:00:00+00:00"

    # Duplicate save without overwrite raises RoutineStoreError
    step2 = [{"tool": "open_application", "args": {"name": "calculator"}}]
    with pytest.raises(RoutineStoreError, match="already exists"):
        store.save(display_name="morning", steps=step2, overwrite=False)

    # Overwrite=True updates steps and display_name, preserves created_at
    clock.now_dt = datetime.fromisoformat("2026-10-27T11:00:00+00:00")
    r2 = store.save(display_name="morning", steps=step2, overwrite=True)
    assert r2.name == "morning"
    assert r2.display_name == "morning"
    assert r2.steps == step2
    assert r2.created_at == "2026-10-27T10:00:00+00:00"


def test_sql_injection_safe(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    store = RoutineStore(path=str(db_file), clock=FakeClock())

    # Step argument with SQL injection attack
    injected_args = {"site": "youtube", "query": "'); DROP TABLE routines; --"}
    steps = [{"tool": "search_site", "args": injected_args}]
    store.save(display_name="safe search", steps=steps)

    r = store.get("safe search")
    assert r is not None
    assert r.steps[0]["args"]["query"] == "'); DROP TABLE routines; --"

    # Verify table routines still exists and is intact
    assert len(store.list()) == 1


def test_corrupt_database_recovery(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    # Write garbage into database file
    db_file.write_text("NOT A VALID SQLITE DATABASE", encoding="utf-8")

    store = RoutineStore(path=str(db_file), clock=FakeClock())
    # Should safely recreate the database
    assert store.list() == []
    store.save(display_name="morning", steps=[{"tool": "open_application", "args": {"name": "calculator"}}])
    assert store.get("morning") is not None


def test_unreadable_row_skipped(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    store = RoutineStore(path=str(db_file), clock=FakeClock())
    store.save(display_name="valid routine", steps=[{"tool": "open_application", "args": {"name": "calculator"}}])

    # Insert a row with corrupted steps_json
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            """INSERT INTO routines (name, display_name, steps_json, schema_version, created_at, run_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("corrupt_row", "Corrupt Row", "NOT JSON DATA", 1, "2026-10-27T10:00:00+00:00", 0),
        )
        conn.commit()

    # Listing should skip corrupt row and return valid row
    routines = store.list()
    assert len(routines) == 1
    assert routines[0].name == "valid routine"

    # Direct get of corrupt row returns None safely
    assert store.get("corrupt_row") is None


def test_delete_and_mark_run(tmp_path: Path):
    db_file = tmp_path / "routines.db"
    clock = FakeClock("2026-10-27T08:00:00Z")
    store = RoutineStore(path=str(db_file), clock=clock)

    store.save(display_name="daily", steps=[{"tool": "open_application", "args": {"name": "calculator"}}])
    r = store.get("daily")
    assert r.run_count == 0
    assert r.last_run_at is None

    # Mark run
    clock.now_dt = datetime.fromisoformat("2026-10-27T09:30:00+00:00")
    store.mark_run("daily")
    r_updated = store.get("daily")
    assert r_updated.run_count == 1
    assert r_updated.last_run_at == "2026-10-27T09:30:00+00:00"

    # Delete
    assert store.delete("daily") is True
    assert store.get("daily") is None
    assert store.delete("daily") is False
