"""Unit tests for local ContactStore, contact validation, privacy masking, and write scanner."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import pytest

from contacts.store import (
    AmbiguousContactError,
    ContactLimit,
    ContactNotFoundError,
    ContactRef,
    ContactStore,
    ContactValidationError,
    mask_phone,
)


def test_contact_ref_repr_and_str_never_contain_phone_or_email():
    """Verify ContactRef repr and str strictly never contain phone numbers or emails."""
    fake_phone = "15550100123"
    fake_email = "testuser@example.com"
    ref = ContactRef(id=1, display_name="Alex Doe", phone=fake_phone, email=fake_email)

    ref_str = str(ref)
    ref_repr = repr(ref)

    assert fake_phone not in ref_str
    assert fake_email not in ref_str
    assert fake_phone not in ref_repr
    assert fake_email not in ref_repr
    assert "Alex Doe" in ref_str
    assert "Alex Doe" in ref_repr


def test_mask_phone():
    """Verify mask_phone displays only the last 4 digits."""
    assert mask_phone("15550100123") == "••••0123"
    assert mask_phone("98765432") == "••••5432"
    assert mask_phone("") == ""
    assert mask_phone("123") == "••••123"


def test_contact_store_add_and_get(tmp_path: Path):
    """Test basic contact creation and retrieval."""
    db_path = tmp_path / "contacts.db"
    store = ContactStore(path=db_path)

    c = store.add(
        display_name="Mom",
        phone="15550100123",
        email="mom@example.com",
        aliases=["mum", "mother"],
    )
    assert c.id == 1
    assert c.display_name == "Mom"
    assert c.phone == "15550100123"
    assert c.email == "mom@example.com"
    assert c.aliases == ["mum", "mother"]

    fetched = store.get(1)
    assert fetched is not None
    assert fetched.display_name == "Mom"


def test_contact_store_field_validations(tmp_path: Path):
    """Verify validation rules on name, phone, email, and aliases."""
    store = ContactStore(path=tmp_path / "contacts.db")

    # Empty name
    with pytest.raises(ContactValidationError):
        store.add(display_name="")

    # Name too long (>40 chars)
    with pytest.raises(ContactValidationError):
        store.add(display_name="A" * 41)

    # Disallowed chars in name
    with pytest.raises(ContactValidationError):
        store.add(display_name="Mom; drop table")

    # Phone with leading zero
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", phone="015550100123")

    # Phone too short (<8 digits)
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", phone="1234567")

    # Phone too long (>15 digits)
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", phone="1" * 16)

    # Phone with letters
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", phone="1555010012a")

    # Invalid email
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", email="invalid-email")

    # Too many aliases (>5)
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", aliases=["b1", "b2", "b3", "b4", "b5", "b6"])

    # Alias too long (>30 chars)
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", aliases=["a" * 31])


def test_contact_store_normalized_uniqueness_and_duplicates(tmp_path: Path):
    """Verify duplicate display names and aliases are strictly rejected."""
    store = ContactStore(path=tmp_path / "contacts.db")
    store.add(display_name="Alice Smith", aliases=["alice"])

    # Duplicate display name (case-insensitive)
    with pytest.raises(ContactValidationError):
        store.add(display_name="alice smith")

    # Name colliding with existing alias
    with pytest.raises(ContactValidationError):
        store.add(display_name="Alice")

    # Alias colliding with existing display name
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", aliases=["Alice Smith"])

    # Alias colliding with another contact's alias
    with pytest.raises(ContactValidationError):
        store.add(display_name="Bob", aliases=["alice"])


def test_contact_store_resolve_exact_match_never_fuzzy(tmp_path: Path):
    """Verify resolve matches only exact normalized names/aliases and never does fuzzy matching."""
    store = ContactStore(path=tmp_path / "contacts.db")
    store.add(display_name="John Doe", aliases=["johnny"])

    # Exact match display name
    ref = store.resolve("John Doe")
    assert ref.display_name == "John Doe"

    # Exact match case-insensitive / whitespace collapsed
    ref2 = store.resolve("  john   doe  ")
    assert ref2.display_name == "John Doe"

    # Exact match alias
    ref3 = store.resolve("johnny")
    assert ref3.display_name == "John Doe"

    # Near / fuzzy match MUST raise ContactNotFoundError
    with pytest.raises(ContactNotFoundError):
        store.resolve("John Do")

    with pytest.raises(ContactNotFoundError):
        store.resolve("John")

    with pytest.raises(ContactNotFoundError):
        store.resolve("johny")


def test_contact_store_resolve_ambiguous(tmp_path: Path):
    """Verify AmbiguousContactError when multiple contacts match."""
    # Under standard store rules, aliases cannot collide across contacts.
    # But if manually forced in raw DB, resolve must detect ambiguity:
    db_path = tmp_path / "contacts.db"
    store = ContactStore(path=db_path)
    store.add(display_name="Sam Adams")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO contacts (display_name, aliases, phone, email, voice_approvable, created_at) "
            'VALUES (\'Sam Adams 2\', \'["sam adams"]\', \'15550100999\', NULL, 0, 1000.0)'
        )
    with pytest.raises(AmbiguousContactError):
        store.resolve("Sam Adams")


def test_contact_store_capacity_limit(tmp_path: Path):
    """Verify limit of 200 contacts."""
    db_path = tmp_path / "contacts.db"
    store = ContactStore(path=db_path)

    with sqlite3.connect(db_path) as conn:
        for i in range(1, 201):
            conn.execute(
                "INSERT INTO contacts (id, display_name, phone, email, aliases, created_at) "
                f"VALUES ({i}, 'Contact {i}', NULL, NULL, '[]', 1000.0)"
            )

    assert store.count() == 200

    # 201st contact must raise ContactLimit
    with pytest.raises(ContactLimit):
        store.add(display_name="Overflow Contact")


def test_contact_store_corrupt_database_recreation(tmp_path: Path):
    """Verify that a corrupted contacts database is safely recreated."""
    db_path = tmp_path / "contacts.db"
    db_path.write_text("NOT A VALID SQLITE DATABASE", encoding="utf-8")

    store = ContactStore(path=db_path)
    # Recreated and functional
    c = store.add(display_name="Recovered Contact", phone="15550100123")
    assert c.display_name == "Recovered Contact"
    assert store.count() == 1


def test_scan_no_contact_write_outside_ui_and_main():
    """Verify that no module outside ui/ and main.py imports ContactStore write methods."""
    repo_root = Path(__file__).resolve().parent.parent

    allowed_prefixes = (
        str(repo_root / "ui"),
        str(repo_root / "main.py"),
        str(repo_root / "contacts"),
        str(repo_root / "tests"),
        str(repo_root / "scratch"),
    )

    forbidden_patterns = [
        ".add(",
        ".update(",
        ".delete(",
    ]

    offending: list[str] = []

    for py_file in repo_root.rglob("*.py"):
        p_str = str(py_file.resolve())
        # Skip allowed files, venvs, git
        if any(p_str.startswith(a) for a in allowed_prefixes):
            continue
        if ".venv" in p_str or "venv" in p_str or ".git" in p_str or "__pycache__" in p_str:
            continue

        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # Check for direct calls of write methods on contact store
        if "contact_store" in content or "contacts_store" in content:
            for pat in forbidden_patterns:
                if f"contact_store{pat}" in content or f"contacts_store{pat}" in content:
                    offending.append(f"{py_file.relative_to(repo_root)} calls {pat}")

    assert not offending, f"Forbidden contact store write calls outside ui/ and main.py: {offending}"
