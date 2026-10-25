"""Tests for Agent taint tracking, untrusted tool output warnings, and extra system text isolation."""

from pathlib import Path
from typing import Optional
import pytest

from core.agent import Agent, TAINT_WARNING
from core.llm import LLMProvider, LLMResponse, ToolCall
from security.audit import AuditLogger
from security.permissions import PermissionLevel, PermissionPolicy
from tools.registry import Tool, ToolRegistry


class FakeProvider(LLMProvider):
    """Fake LLM provider returning scripted responses and tracking messages."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
    ) -> LLMResponse:
        self.call_history.append([dict(m) for m in messages])
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="Default fallback response")


@pytest.fixture
def taint_registry() -> ToolRegistry:
    """Fixture providing a registry with safe, untrusted, confirm, and restricted tools."""
    reg = ToolRegistry()

    # Safe tool with untrusted output (e.g. read_file or search_documents)
    reg.register(
        Tool(
            name="read_untrusted_doc",
            description="Reads an untrusted document",
            parameters={"type": "object", "properties": {"doc_id": {"type": "string"}}},
            func=lambda doc_id="1": f"content of doc {doc_id}",
            permission=PermissionLevel.SAFE,
            untrusted_output=True,
        )
    )

    # Safe tool that fails with an error
    reg.register(
        Tool(
            name="failing_untrusted_doc",
            description="Reads an untrusted document but fails",
            parameters={"type": "object", "properties": {}},
            func=lambda: "ERROR: file corrupted or not found",
            permission=PermissionLevel.SAFE,
            untrusted_output=True,
        )
    )

    # Safe tool with normal trusted output
    reg.register(
        Tool(
            name="safe_trusted_tool",
            description="Safe trusted tool",
            parameters={"type": "object", "properties": {}},
            func=lambda: "trusted result",
            permission=PermissionLevel.SAFE,
            untrusted_output=False,
        )
    )

    # Confirm-level tool requiring user confirmation
    reg.register(
        Tool(
            name="write_file",
            description="Writes content to a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
            func=lambda path="test.txt", content="": f"Wrote to {path}",
            permission=PermissionLevel.CONFIRM,
        )
    )

    # Restricted tool
    reg.register(
        Tool(
            name="delete_system_file",
            description="Restricted system deletion",
            parameters={"type": "object", "properties": {}},
            func=lambda: "deleted",
            permission=PermissionLevel.RESTRICTED,
        )
    )

    return reg


def test_safe_untrusted_then_confirm_passes_warning(taint_registry: ToolRegistry, tmp_path: Path):
    """A SAFE untrusted tool followed by a CONFIRM tool in the same turn passes a warning to confirm_callback."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()

    # Step 1: LLM calls read_untrusted_doc
    # Step 2: LLM calls write_file
    # Step 3: LLM finishes
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="read_untrusted_doc", args={"doc_id": "secret"})]),
        LLMResponse(tool_calls=[ToolCall(name="write_file", args={"path": "out.txt", "content": "data"})]),
        LLMResponse(text="File written successfully."),
    ])

    captured_warnings: list[Optional[str]] = []

    def confirm_cb(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
        captured_warnings.append(warning)
        return True

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )

    reply = agent.chat("Read document and write output")
    assert reply == "File written successfully."

    # confirm_callback must have been called for write_file with a warning
    assert len(captured_warnings) == 1
    assert captured_warnings[0] is not None
    assert "untrusted content" in captured_warnings[0]
    assert captured_warnings[0] == TAINT_WARNING


def test_safe_untrusted_then_confirm_in_same_response_passes_warning(taint_registry: ToolRegistry, tmp_path: Path):
    """When untrusted and confirm tools are emitted in a single multi-tool response, warning is passed."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()

    provider = FakeProvider([
        LLMResponse(tool_calls=[
            ToolCall(name="read_untrusted_doc", args={"doc_id": "123"}),
            ToolCall(name="write_file", args={"path": "out.txt", "content": "123"}),
        ]),
        LLMResponse(text="Done"),
    ])

    captured_warnings: list[Optional[str]] = []

    def confirm_cb(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
        captured_warnings.append(warning)
        return True

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )

    agent.chat("Process doc in batch")
    assert len(captured_warnings) == 1
    assert captured_warnings[0] == TAINT_WARNING


def test_confirm_tool_without_untrusted_gets_no_warning(taint_registry: ToolRegistry, tmp_path: Path):
    """A CONFIRM tool in a turn with no untrusted tool gets warning=None."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="safe_trusted_tool", args={})]),
        LLMResponse(tool_calls=[ToolCall(name="write_file", args={"path": "notes.txt", "content": "ok"})]),
        LLMResponse(text="Note created."),
    ])

    captured_warnings: list[Optional[str]] = []

    def confirm_cb(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
        captured_warnings.append(warning)
        return True

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )

    agent.chat("Write a note")
    assert len(captured_warnings) == 1
    assert captured_warnings[0] is None


def test_taint_resets_at_next_chat_call(taint_registry: ToolRegistry, tmp_path: Path):
    """Taint resets at the next chat() call; a CONFIRM tool in the subsequent turn gets warning=None."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()

    # Turn 1: Read untrusted doc
    # Turn 2: Run confirm tool write_file
    provider = FakeProvider([
        # Turn 1 responses
        LLMResponse(tool_calls=[ToolCall(name="read_untrusted_doc", args={"doc_id": "doc1"})]),
        LLMResponse(text="Read finished."),
        # Turn 2 responses
        LLMResponse(tool_calls=[ToolCall(name="write_file", args={"path": "save.txt", "content": "saved"})]),
        LLMResponse(text="Saved."),
    ])

    captured_warnings: list[Optional[str]] = []

    def confirm_cb(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
        captured_warnings.append(warning)
        return True

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )

    # Turn 1
    agent.chat("Read the document")
    assert len(captured_warnings) == 0

    # Turn 2
    agent.chat("Save it to file")
    assert len(captured_warnings) == 1
    assert captured_warnings[0] is None


def test_audit_entry_records_tainted_flag(taint_registry: ToolRegistry, tmp_path: Path):
    """The audit entry for the tainted action has tainted=True and an untainted one has False."""
    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(path=str(audit_file))
    policy = PermissionPolicy()

    provider = FakeProvider([
        # Turn 1: Untrusted read then confirm write
        LLMResponse(tool_calls=[ToolCall(name="read_untrusted_doc", args={"doc_id": "alpha"})]),
        LLMResponse(tool_calls=[ToolCall(name="write_file", args={"path": "alpha.txt", "content": "data"})]),
        LLMResponse(text="Turn 1 done"),
        # Turn 2: Untainted confirm write
        LLMResponse(tool_calls=[ToolCall(name="write_file", args={"path": "beta.txt", "content": "data2"})]),
        LLMResponse(text="Turn 2 done"),
    ])

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        policy=policy,
        audit=audit,
        confirm_callback=lambda name, args, warning=None: True,
    )

    agent.chat("Turn 1 message")
    agent.chat("Turn 2 message")

    records = audit.read_all()
    assert len(records) == 3

    # Record 0: read_untrusted_doc (tainted is False when executing this tool)
    assert records[0]["tool"] == "read_untrusted_doc"
    assert records[0]["tainted"] is False

    # Record 1: write_file in Turn 1 (executed after read_untrusted_doc succeeded, so tainted=True)
    assert records[1]["tool"] == "write_file"
    assert records[1]["tainted"] is True

    # Record 2: write_file in Turn 2 (fresh turn, so tainted=False)
    assert records[2]["tool"] == "write_file"
    assert records[2]["tainted"] is False


def test_restricted_tool_denied_while_tainted(taint_registry: ToolRegistry, tmp_path: Path):
    """A RESTRICTED tool is still denied while tainted."""
    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(path=str(audit_file))
    policy = PermissionPolicy()

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="read_untrusted_doc", args={"doc_id": "malicious"})]),
        LLMResponse(tool_calls=[ToolCall(name="delete_system_file", args={})]),
        LLMResponse(text="Execution halted."),
    ])

    confirm_called = False

    def confirm_cb(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )

    agent.chat("Inspect doc and delete")

    # Confirm callback should never be called for RESTRICTED tool
    assert not confirm_called

    # Check that tool was blocked in history
    assert any("tool delete_system_file is not permitted" in str(msg.get("content", "")) for msg in agent.history)

    # Check audit record
    records = audit.read_all()
    assert len(records) == 2
    assert records[1]["tool"] == "delete_system_file"
    assert records[1]["decision"] == "deny"
    assert records[1]["outcome"] == "blocked"
    assert records[1]["tainted"] is True


def test_failed_untrusted_tool_does_not_set_taint(taint_registry: ToolRegistry, tmp_path: Path):
    """An untrusted tool that failed (ERROR) does not set taint."""
    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(path=str(audit_file))
    policy = PermissionPolicy()

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="failing_untrusted_doc", args={})]),
        LLMResponse(tool_calls=[ToolCall(name="write_file", args={"path": "out.txt", "content": "retry"})]),
        LLMResponse(text="Done"),
    ])

    captured_warnings: list[Optional[str]] = []

    def confirm_cb(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
        captured_warnings.append(warning)
        return True

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )

    agent.chat("Try reading doc then writing")

    assert len(captured_warnings) == 1
    # Taint should NOT be set because the untrusted tool errored
    assert captured_warnings[0] is None

    records = audit.read_all()
    assert len(records) == 2
    assert records[0]["tool"] == "failing_untrusted_doc"
    assert records[0]["outcome"] == "error"
    assert records[0]["tainted"] is False

    assert records[1]["tool"] == "write_file"
    assert records[1]["tainted"] is False


def test_docs_block_via_extra_system_text_never_in_agent_history(taint_registry: ToolRegistry):
    """The docs block passed via /ask-style extra text never appears in agent.history."""
    docs_block = (
        "=== UNTRUSTED PASSAGES ===\n"
        "[1] Sensitive passage from indexed document\n"
        "=== END UNTRUSTED PASSAGES ==="
    )

    provider = FakeProvider([
        LLMResponse(text="According to the document, here is the answer."),
    ])

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        system_prompt="You are ORBIT.",
    )

    agent.chat("What does the document say?", extra_system_text=docs_block)

    # 1. Provider messages MUST contain the docs_block in the system message
    assert len(provider.call_history) == 1
    sent_messages = provider.call_history[0]
    system_messages = [m for m in sent_messages if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert "Sensitive passage from indexed document" in system_messages[0]["content"]

    # 2. agent.history MUST NOT contain the docs_block in ANY role or content
    for msg in agent.history:
        content = str(msg.get("content", ""))
        assert "Sensitive passage from indexed document" not in content
        assert "UNTRUSTED PASSAGES" not in content

    # 3. Only the user and assistant turns are stored in agent.history
    roles = [m["role"] for m in agent.history]
    assert roles == ["user", "assistant"]
    assert agent.history[0]["content"] == "What does the document say?"
    assert agent.history[1]["content"] == "According to the document, here is the answer."


def test_multiple_extra_system_text_blocks_never_in_agent_history(taint_registry: ToolRegistry):
    """Multiple extra system text blocks are sent to provider but never appear in agent.history."""
    blocks = [
        "Extra Block 1: Memory notes",
        "Extra Block 2: Document passages",
    ]

    provider = FakeProvider([
        LLMResponse(text="Combined answer"),
    ])

    agent = Agent(
        provider=provider,
        registry=taint_registry,
        system_prompt="Base prompt.",
    )

    agent.chat("Question with multiple extras", extra_system_text=blocks)

    assert len(provider.call_history) == 1
    system_content = provider.call_history[0][0]["content"]
    assert "Extra Block 1: Memory notes" in system_content
    assert "Extra Block 2: Document passages" in system_content

    for msg in agent.history:
        content = str(msg.get("content", ""))
        assert "Extra Block 1" not in content
        assert "Extra Block 2" not in content
