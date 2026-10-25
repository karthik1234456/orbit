"""Memory tools for ORBIT assistant, enabling persistent fact recall and management."""

from typing import List

from memory.safety import MemoryRejected
from memory.store import MemoryFull, MemoryStore
from security.permissions import PermissionLevel
from tools.registry import Tool


def make_memory_tools(store: MemoryStore) -> List[Tool]:
    """Create and return Tool objects for interacting with the persistent memory store."""

    def remember_fact(content: str, category: str = "fact") -> str:
        """Save a durable fact or preference to memory."""
        try:
            mem, created = store.add(content, category=category)
            if not created:
                return "Already remembered."
            return f"Remembered: {mem.content} (id: {mem.id})"
        except MemoryRejected as exc:
            return f"ERROR: REJECTED: {exc}"
        except MemoryFull as exc:
            return f"ERROR: {exc}"

    def recall_memories(query: str) -> str:
        """Search stored memories using keyword matching."""
        results = store.search(query, limit=5, touch=True)
        if not results:
            return "No matching memories."
        return "\n".join(f"[{m.id}] ({m.category}) {m.content}" for m in results)

    def list_memories() -> str:
        """List the 20 most recently saved memories."""
        results = store.list_all(limit=20)
        if not results:
            return "No memories stored."
        return "\n".join(f"[{m.id}] ({m.category}) {m.content}" for m in results)

    def forget_memory(memory_id: int) -> str:
        """Delete a stored memory by its integer ID."""
        deleted = store.delete(memory_id)
        if not deleted:
            return f"ERROR: Memory with ID {memory_id} not found."
        return f"Memory {memory_id} forgotten."

    return [
        Tool(
            name="remember_fact",
            description=(
                "Call this whenever the user says remember, save, note that, or states a lasting preference or fact about themselves. "
                "You cannot remember anything without calling this tool. "
                "Never save passwords, keys, tokens, or anything the user didn't say about themselves; "
                "one fact per call."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Short, durable fact or preference to remember.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional category ('preference', 'fact', 'project', 'other'). Defaults to 'fact'.",
                    },
                },
                "required": ["content"],
            },
            func=remember_fact,
            permission=PermissionLevel.CONFIRM,
        ),
        Tool(
            name="recall_memories",
            description="Search stored memories and user facts using keywords.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords or search term to match against memories.",
                    }
                },
                "required": ["query"],
            },
            func=recall_memories,
            permission=PermissionLevel.SAFE,
        ),
        Tool(
            name="list_memories",
            description="List the 20 most recent memories.",
            parameters={
                "type": "object",
                "properties": {},
            },
            func=list_memories,
            permission=PermissionLevel.SAFE,
        ),
        Tool(
            name="forget_memory",
            description="Delete a stored memory by its ID.",
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "integer",
                        "description": "The unique integer ID of the memory to delete.",
                    }
                },
                "required": ["memory_id"],
            },
            func=forget_memory,
            permission=PermissionLevel.CONFIRM,
        ),
    ]
