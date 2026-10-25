"""Unit tests for memory tools and permission specifications."""

from pathlib import Path
import pytest

from memory.store import MemoryStore
from security.permissions import PermissionLevel
from tools.memory_tools import make_memory_tools


@pytest.fixture
def memory_tools(tmp_path: Path):
    """Fixture providing a fresh memory store and tool dictionary."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(path=db_path)
    tools = make_memory_tools(store)
    return {t.name: t for t in tools}, store


def test_memory_tools_permissions(memory_tools):
    """Verify each memory tool has the exact specified permission level."""
    tools, _ = memory_tools

    assert tools["remember_fact"].permission == PermissionLevel.CONFIRM
    assert tools["recall_memories"].permission == PermissionLevel.SAFE
    assert tools["list_memories"].permission == PermissionLevel.SAFE
    assert tools["forget_memory"].permission == PermissionLevel.CONFIRM


def test_remember_then_recall_memories(memory_tools):
    """Verify remember_fact stores a fact and recall_memories retrieves it."""
    tools, store = memory_tools

    res = tools["remember_fact"].func(content="User prefers dark mode in editors", category="preference")
    assert "Remembered:" in res
    assert "(id: 1)" in res

    recall_res = tools["recall_memories"].func(query="dark mode")
    assert "[1] (preference) User prefers dark mode in editors" in recall_res


def test_remember_duplicate_message(memory_tools):
    """Verify storing an identical fact returns 'Already remembered.'."""
    tools, store = memory_tools

    res1 = tools["remember_fact"].func(content="User lives in Seattle")
    assert "Remembered:" in res1

    # Exact duplicate (case/whitespace insensitive)
    res2 = tools["remember_fact"].func(content="  user LIVES in Seattle  ")
    assert res2 == "Already remembered."
    assert store.count() == 1


def test_remember_secret_refused_with_error(memory_tools):
    """Verify secret patterns are refused with a string starting with ERROR:."""
    tools, store = memory_tools

    res_pw = tools["remember_fact"].func(content="my password: supersecretpassword123")
    assert res_pw.startswith("ERROR:")
    assert store.count() == 0

    res_key = tools["remember_fact"].func(content="API key: AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345")
    assert res_key.startswith("ERROR:")
    assert store.count() == 0


def test_forget_memory_success_and_missing_returns_error(memory_tools):
    """Verify forget_memory deletes an existing fact and returns ERROR for missing ID."""
    tools, store = memory_tools

    # Non-existent ID returns error
    err_res = tools["forget_memory"].func(memory_id=99999)
    assert err_res.startswith("ERROR:")
    assert "not found" in err_res.lower()

    # Add memory and then forget it
    tools["remember_fact"].func(content="Temporary note")
    assert store.count() == 1

    del_res = tools["forget_memory"].func(memory_id=1)
    assert "forgotten" in del_res.lower()
    assert store.count() == 0


def test_list_memories(memory_tools):
    """Verify list_memories returns the recent memories or a clean empty message."""
    tools, store = memory_tools

    # Empty store
    empty_res = tools["list_memories"].func()
    assert "No memories" in empty_res

    # Multiple memories
    tools["remember_fact"].func(content="Fact number one")
    tools["remember_fact"].func(content="Fact number two")

    list_res = tools["list_memories"].func()
    assert "[1] (fact) Fact number one" in list_res
    assert "[2] (fact) Fact number two" in list_res


def test_remember_fact_description_trigger(memory_tools):
    """Verify remember_fact description starts with trigger sentence and keeps safety notes."""
    tools, _ = memory_tools
    desc = tools["remember_fact"].description
    trigger = (
        "Call this whenever the user says remember, save, note that, or states a lasting preference or fact about themselves. "
        "You cannot remember anything without calling this tool."
    )
    assert desc.startswith(trigger)
    assert "Never save passwords, keys, tokens" in desc
