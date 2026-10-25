"""Unit tests for SQLite persistent MemoryStore."""

from pathlib import Path
import pytest

from memory.store import MemoryFull, MemoryStore


def test_add_and_list_all(tmp_path: Path):
    """Verify add stores memories correctly and list_all returns newest first."""
    db_path = tmp_path / "data" / "memory.db"
    store = MemoryStore(path=db_path)

    mem1, created1 = store.add("User prefers dark mode in editors", category="preference")
    assert created1 is True
    assert mem1.id == 1
    assert mem1.content == "User prefers dark mode in editors"
    assert mem1.category == "preference"
    assert mem1.use_count == 0
    assert mem1.created_at == mem1.last_used_at

    mem2, created2 = store.add("User lives in Tokyo", category="fact")
    assert created2 is True
    assert mem2.id == 2

    # list_all returns newest first
    all_mems = store.list_all()
    assert len(all_mems) == 2
    assert all_mems[0].id == mem2.id
    assert all_mems[1].id == mem1.id
    assert store.count() == 2


def test_duplicates_case_differences_not_readded(tmp_path: Path):
    """Verify duplicates differing only in casing and spacing are not re-added."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    mem1, created1 = store.add("User loves Python programming", category="preference")
    assert created1 is True

    # Same content with different case and extra spacing
    mem2, created2 = store.add("   user loves PYTHON programming   ", category="preference")
    assert created2 is False
    assert mem2.id == mem1.id
    assert mem2.content == mem1.content
    assert store.count() == 1


def test_memory_full_at_limit(tmp_path: Path):
    """Verify MemoryFull is raised when attempting to add beyond max_items capacity."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path, max_items=2)

    store.add("Item 1")
    store.add("Item 2")
    assert store.count() == 2

    # Existing duplicate should still return False and not raise MemoryFull
    _, created = store.add("Item 1")
    assert created is False

    # Adding a 3rd distinct item must raise MemoryFull
    with pytest.raises(MemoryFull) as excinfo:
        store.add("Item 3")
    assert "capacity limit reached" in str(excinfo.value).lower()
    assert store.count() == 2


def test_search_ranks_more_matching_words_first(tmp_path: Path):
    """Verify search ranks memories by the number of matched query words descending."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    # mem_high matches: python, web, development, fast
    mem_high, _ = store.add("User specializes in Python web development and fast APIs")
    # mem_low matches only: python
    mem_low, _ = store.add("User has installed Python 3.13")

    query = "python web development"
    results = store.search(query, limit=5, touch=True)

    assert len(results) == 2
    assert results[0].id == mem_high.id
    assert results[1].id == mem_low.id

    # Verify touch=True updated use_count and last_used_at
    assert results[0].use_count == 1
    assert results[1].use_count == 1


def test_search_touch_false_does_not_increment_use_count(tmp_path: Path):
    """Verify search with touch=False does not modify use_count."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    mem, _ = store.add("User prefers automated tests")
    results = store.search("automated tests", touch=False)
    assert len(results) == 1
    assert results[0].use_count == 0

    reloaded = store.list_all()
    assert reloaded[0].use_count == 0


def test_search_returns_empty_for_stopword_only_queries(tmp_path: Path):
    """Verify queries containing only stopwords return an empty list without searching."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    store.add("General test fact about software")
    # Query with stopwords only and words under 3 chars
    results = store.search("what is that for you and the")
    assert results == []

    # Blank / whitespace query
    assert store.search("") == []
    assert store.search("   ") == []


def test_like_wildcards_in_query_do_not_match_everything(tmp_path: Path):
    """Verify '%' and '_' in queries are escaped so '100%' matches only literal '100%'."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    mem_percent, _ = store.add("Battery charge is 100% full")
    mem_number, _ = store.add("There are 100 cats in the park")

    results = store.search("100%")
    assert len(results) == 1
    assert results[0].id == mem_percent.id
    assert results[0].content == "Battery charge is 100% full"


def test_sql_injection_attempt_handled_safely(tmp_path: Path):
    """Verify SQL injection strings as content or search query are handled safely."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    injection_content = "'; DROP TABLE memories;--"
    mem, created = store.add(injection_content)
    assert created is True
    assert mem.content == injection_content

    # Query with SQL injection attack string
    search_res = store.search("'; DROP TABLE memories;--")
    assert len(search_res) >= 1
    assert search_res[0].id == mem.id

    # Verify table is completely intact and functional
    assert store.count() == 1
    store.add("Normal post-attack fact")
    assert store.count() == 2


def test_delete_works_and_missing_id_returns_false(tmp_path: Path):
    """Verify delete removes existing items and returns False for non-existent IDs."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    # Missing ID returns False
    assert store.delete(99999) is False

    mem1, _ = store.add("Temporary memory to delete")
    assert store.count() == 1

    # Delete existing item
    assert store.delete(mem1.id) is True
    assert store.count() == 0

    # Deleting again returns False
    assert store.delete(mem1.id) is False


def test_clear_removes_all_items(tmp_path: Path):
    """Verify clear empties the store and returns count of removed items."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)

    store.add("Item 1")
    store.add("Item 2")
    store.add("Item 3")
    assert store.count() == 3

    removed = store.clear()
    assert removed == 3
    assert store.count() == 0


def test_persistence_across_instances(tmp_path: Path):
    """Verify a newly initialized MemoryStore instance reads existing on-disk database."""
    db_path = tmp_path / "persistent" / "memory.db"
    store1 = MemoryStore(path=db_path)
    store1.add("User prefers pytest for unit testing", category="preference")

    # Second instance pointing to identical path
    store2 = MemoryStore(path=db_path)
    assert store2.count() == 1
    records = store2.list_all()
    assert len(records) == 1
    assert records[0].content == "User prefers pytest for unit testing"
    assert records[0].category == "preference"
