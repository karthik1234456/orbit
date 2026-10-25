"""Tests for Agent permission enforcement, confirmation callbacks, and audit logging."""

from pathlib import Path
from typing import Optional
import pytest

from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from security.audit import AuditLogger
from security.permissions import PermissionLevel, PermissionPolicy
from tools.registry import Tool, ToolRegistry


class FakeProvider(LLMProvider):
    """Fake provider returning scripted responses."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
    ) -> LLMResponse:
        self.call_history.append(list(messages))
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="Fallback response")


@pytest.fixture
def secured_registry() -> ToolRegistry:
    """Fixture providing a registry with tools across various permission levels."""
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="safe_tool",
            description="Safe read-only tool",
            parameters={"type": "object", "properties": {}},
            func=lambda: "safe content",
            permission=PermissionLevel.SAFE,
        )
    )
    reg.register(
        Tool(
            name="confirm_tool",
            description="Tool requiring confirmation",
            parameters={"type": "object", "properties": {"val": {"type": "string"}}, "required": ["val"]},
            func=lambda val: f"written: {val}",
            permission=PermissionLevel.CONFIRM,
        )
    )
    reg.register(
        Tool(
            name="restricted_tool",
            description="High risk restricted tool",
            parameters={"type": "object", "properties": {}},
            func=lambda: "deleted",
            permission=PermissionLevel.RESTRICTED,
        )
    )
    return reg


def test_1_safe_tool_runs_without_calling_confirm_callback(secured_registry: ToolRegistry, tmp_path: Path):
    """(1) A SAFE tool runs without calling confirm_callback."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="safe_tool", args={})]),
        LLMResponse(text="Read finished."),
    ])

    callback_called = False

    def confirm_callback(name: str, args: dict) -> bool:
        nonlocal callback_called
        callback_called = True
        return False

    agent = Agent(
        provider=provider,
        registry=secured_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Please read safe data")
    assert reply == "Read finished."
    assert callback_called is False, "confirm_callback must not be invoked for SAFE tools"
    assert agent.last_tool_events == [("safe_tool", {}, "safe content")]

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "safe_tool"
    assert records[0]["decision"] == "allow"
    assert records[0]["outcome"] == "success"
    assert records[0]["result"] == "safe content"


def test_2_confirm_tool_runs_when_confirm_callback_returns_true(secured_registry: ToolRegistry, tmp_path: Path):
    """(2) A CONFIRM tool runs when confirm_callback returns True."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="confirm_tool", args={"val": "important_note"})]),
        LLMResponse(text="Note successfully saved."),
    ])

    callback_called = False

    def confirm_callback(name: str, args: dict) -> bool:
        nonlocal callback_called
        callback_called = True
        assert name == "confirm_tool"
        assert args == {"val": "important_note"}
        return True

    agent = Agent(
        provider=provider,
        registry=secured_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Save a note")
    assert reply == "Note successfully saved."
    assert callback_called is True
    assert agent.last_tool_events == [("confirm_tool", {"val": "important_note"}, "written: important_note")]

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "confirm_tool"
    assert records[0]["decision"] == "user_approved"
    assert records[0]["outcome"] == "success"
    assert records[0]["result"] == "written: important_note"


def test_3_confirm_tool_does_not_run_when_callback_returns_false_and_llm_receives_error(secured_registry: ToolRegistry, tmp_path: Path):
    """(3) A CONFIRM tool does NOT run when confirm_callback returns False, and the LLM receives an ERROR result."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="confirm_tool", args={"val": "data"})]),
        LLMResponse(text="I understand you denied the write operation."),
    ])

    callback_called = False

    def confirm_callback(name: str, args: dict) -> bool:
        nonlocal callback_called
        callback_called = True
        return False  # User denies

    agent = Agent(
        provider=provider,
        registry=secured_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Write data")
    assert reply == "I understand you denied the write operation."
    assert callback_called is True

    # Tool returned ERROR to agent
    expected_error = "ERROR: user denied permission for confirm_tool"
    assert agent.last_tool_events == [("confirm_tool", {"val": "data"}, expected_error)]

    # The LLM receives the ERROR result in the subsequent generate call messages
    assert len(provider.call_history) == 2
    tool_messages = [m for m in provider.call_history[1] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["name"] == "confirm_tool"
    assert tool_messages[0]["content"].startswith(expected_error)

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "confirm_tool"
    assert records[0]["decision"] == "user_denied"
    assert records[0]["outcome"] == "blocked"
    assert records[0]["result"] == expected_error


def test_4_restricted_tool_never_runs_even_if_confirm_callback_would_approve(secured_registry: ToolRegistry, tmp_path: Path):
    """(4) A RESTRICTED tool never runs, even if confirm_callback would approve."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="restricted_tool", args={})]),
        LLMResponse(text="Restricted action was denied."),
    ])

    callback_called = False

    def confirm_callback(name: str, args: dict) -> bool:
        nonlocal callback_called
        callback_called = True
        return True  # Would approve, but must NEVER be called

    agent = Agent(
        provider=provider,
        registry=secured_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Run dangerous command")
    assert reply == "Restricted action was denied."
    assert callback_called is False, "confirm_callback must not be called for RESTRICTED tools"

    expected_error = "ERROR: tool restricted_tool is not permitted"
    assert agent.last_tool_events == [("restricted_tool", {}, expected_error)]

    # Verify LLM received the error string
    assert len(provider.call_history) == 2
    tool_messages = [m for m in provider.call_history[1] if m.get("role") == "tool"]
    assert tool_messages[0]["content"].startswith(expected_error)

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "restricted_tool"
    assert records[0]["decision"] == "deny"
    assert records[0]["outcome"] == "blocked"
    assert records[0]["result"] == expected_error


def test_5_tool_with_unknown_permission_level_is_denied(tmp_path: Path):
    """(5) A tool with an unknown permission level is denied."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()

    tool_executed = False

    def rogue_func():
        nonlocal tool_executed
        tool_executed = True
        return "rogue output"

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="unknown_tool",
            description="Tool with invalid or unrecognized permission level",
            parameters={"type": "object", "properties": {}},
            func=rogue_func,
            permission="UNKNOWN_LEVEL",  # type: ignore - testing anomalous permission value
        )
    )

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="unknown_tool", args={})]),
        LLMResponse(text="Unknown tool was blocked."),
    ])

    callback_called = False

    def confirm_callback(name: str, args: dict) -> bool:
        nonlocal callback_called
        callback_called = True
        return True  # Would approve, but must not be called

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Run unknown tool")
    assert reply == "Unknown tool was blocked."
    assert tool_executed is False, "Tool with unknown permission must not execute"
    assert callback_called is False, "confirm_callback must not be called for denied tool"

    expected_error = "ERROR: tool unknown_tool is not permitted"
    assert agent.last_tool_events == [("unknown_tool", {}, expected_error)]

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "unknown_tool"
    assert records[0]["decision"] == "deny"
    assert records[0]["outcome"] == "blocked"
    assert records[0]["result"] == expected_error


@pytest.mark.parametrize(
    "tool_name,tool_level,policy_override,user_approval,expected_decision,expected_outcome",
    [
        ("safe_tool", PermissionLevel.SAFE, None, False, "allow", "success"),
        ("confirm_tool", PermissionLevel.CONFIRM, None, True, "user_approved", "success"),
        ("confirm_tool", PermissionLevel.CONFIRM, None, False, "user_denied", "blocked"),
        ("restricted_tool", PermissionLevel.RESTRICTED, None, True, "deny", "blocked"),
        ("unknown_tool", "INVALID_LEVEL", None, True, "deny", "blocked"),
        ("safe_tool", PermissionLevel.SAFE, PermissionLevel.RESTRICTED, True, "deny", "blocked"),
    ],
)
def test_6_every_case_above_produces_exactly_one_audit_entry_with_right_decision_and_outcome(
    tmp_path: Path,
    tool_name: str,
    tool_level,
    policy_override,
    user_approval: bool,
    expected_decision: str,
    expected_outcome: str,
):
    """(6) Every case above produces exactly one audit entry with the right decision and outcome."""
    audit_file = tmp_path / f"audit_{tool_name}_{expected_decision}.jsonl"
    audit = AuditLogger(path=str(audit_file))

    overrides = {tool_name: policy_override} if policy_override else {}
    policy = PermissionPolicy(overrides=overrides)

    reg = ToolRegistry()
    reg.register(
        Tool(
            name=tool_name,
            description="Test tool",
            parameters={"type": "object", "properties": {}},
            func=lambda: "result",
            permission=tool_level,
        )
    )

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name=tool_name, args={})]),
        LLMResponse(text="Done"),
    ])

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=lambda name, args: user_approval,
    )

    agent.chat("Execute test")

    records = audit.read_all()
    assert len(records) == 1, f"Expected exactly one audit record, got {len(records)}"
    assert records[0]["tool"] == tool_name
    assert records[0]["decision"] == expected_decision
    assert records[0]["outcome"] == expected_outcome


def test_7_override_can_downgrade_tool_to_restricted(secured_registry: ToolRegistry, tmp_path: Path):
    """(7) An override can downgrade a tool to RESTRICTED."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    # Downgrade safe_tool (normally SAFE) to RESTRICTED via policy override
    policy = PermissionPolicy(overrides={"safe_tool": PermissionLevel.RESTRICTED})

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="safe_tool", args={})]),
        LLMResponse(text="Safe tool was blocked by override."),
    ])

    callback_called = False

    def confirm_callback(name: str, args: dict) -> bool:
        nonlocal callback_called
        callback_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=secured_registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Read safe tool")
    assert reply == "Safe tool was blocked by override."
    assert callback_called is False, "Downgraded RESTRICTED tool must not prompt user"

    expected_error = "ERROR: tool safe_tool is not permitted"
    assert agent.last_tool_events == [("safe_tool", {}, expected_error)]

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "safe_tool"
    assert records[0]["decision"] == "deny"
    assert records[0]["outcome"] == "blocked"
    assert records[0]["result"] == expected_error


# ---------------------------------------------------------------------------
# Precheck security, confirmation avoidance, and audit tests
# ---------------------------------------------------------------------------


def test_open_application_typo_precheck_failed_skips_confirmation(tmp_path: Path):
    """Calling open_application for 'whatsappp' (typo) triggers no confirm_callback and records precheck_failed."""
    from config.apps import ALLOWED_APPS
    from tools.apps import make_app_tools

    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    reg = ToolRegistry()
    for t in make_app_tools(ALLOWED_APPS):
        reg.register(t)

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="open_application", args={"name": "whatsappp"})]),
        LLMResponse(text="The application could not be opened because it is not allowed."),
    ])

    confirm_called = False

    def confirm_callback(name: str, args: dict, warning=None) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Open whatsappp")
    assert reply == "The application could not be opened because it is not allowed."
    assert confirm_called is False, "confirm_callback must NOT be called when precheck fails"

    # Verify tool result returned to model starts with ERROR
    assert len(agent.last_tool_events) == 1
    tool_name, tool_args, tool_result = agent.last_tool_events[0]
    assert tool_name == "open_application"
    assert tool_result.startswith("ERROR:")
    assert "not in the allowed list" in tool_result.lower()

    # Verify audit record
    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "open_application"
    assert records[0]["decision"] == "precheck_failed"
    assert records[0]["outcome"] == "blocked"
    assert records[0]["result"].startswith("ERROR:")

    # Verify run trace
    assert agent.last_run is not None
    assert len(agent.last_run.steps) == 1
    assert agent.last_run.steps[0].tool == "open_application"
    assert agent.last_run.steps[0].status == "blocked"
    assert agent.last_run.steps[0].result.startswith("ERROR:")


def test_valid_app_still_asks_for_confirmation(tmp_path: Path):
    """A valid application passes precheck and still asks the user for confirmation."""
    from unittest.mock import patch
    from config.apps import ALLOWED_APPS
    from tools.apps import make_app_tools

    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    reg = ToolRegistry()
    for t in make_app_tools(ALLOWED_APPS):
        reg.register(t)

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="open_application", args={"name": "notepad"})]),
        LLMResponse(text="Notepad opened."),
    ])

    confirm_called = False

    def confirm_callback(name: str, args: dict, warning=None) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    with patch("shutil.which", return_value="C:\\Windows\\notepad.exe"), patch("subprocess.Popen"):
        reply = agent.chat("Open notepad")
        assert reply == "Notepad opened."
        assert confirm_called is True, "Valid application must ask for confirmation"

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["decision"] == "user_approved"
    assert records[0]["outcome"] == "success"


def test_precheck_that_raises_is_treated_as_failure(tmp_path: Path):
    """A precheck that raises an exception must be safely trapped and treated as precheck_failed."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    reg = ToolRegistry()

    def exploding_precheck(args: dict):
        raise RuntimeError("Unexpected precheck explosion")

    reg.register(
        Tool(
            name="exploding_tool",
            description="Tool with buggy precheck",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
            func=lambda x: f"ran {x}",
            permission=PermissionLevel.CONFIRM,
            precheck=exploding_precheck,
        )
    )

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="exploding_tool", args={"x": "val"})]),
        LLMResponse(text="Tool precheck failed gracefully."),
    ])

    confirm_called = False

    def confirm_callback(name: str, args: dict, warning=None) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Run exploding tool")
    assert reply == "Tool precheck failed gracefully."
    assert confirm_called is False, "confirm_callback must NOT be called if precheck raises"

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["decision"] == "precheck_failed"
    assert records[0]["outcome"] == "blocked"
    assert "Unexpected precheck explosion" in records[0]["result"]


def test_precheck_passes_but_execution_validation_fails(tmp_path: Path):
    """A tool whose precheck passes but whose execution validation fails is still refused and logged."""
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    reg = ToolRegistry()

    # Precheck passes (returns None), but func detects runtime issue and returns ERROR
    reg.register(
        Tool(
            name="runtime_fail_tool",
            description="Tool that fails during execution",
            parameters={"type": "object", "properties": {"file": {"type": "string"}}},
            func=lambda file: "ERROR: File was locked by another process.",
            permission=PermissionLevel.CONFIRM,
            precheck=lambda args: None,  # Precheck says okay
        )
    )

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="runtime_fail_tool", args={"file": "doc.txt"})]),
        LLMResponse(text="Handled runtime failure."),
    ])

    confirm_called = False

    def confirm_callback(name: str, args: dict, warning=None) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Run runtime fail tool")
    assert reply == "Handled runtime failure."
    assert confirm_called is True  # Precheck passed so confirmation was requested

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["decision"] == "user_approved"
    assert records[0]["outcome"] == "error"
    assert "locked" in records[0]["result"]


def test_open_website_invalid_scheme_skips_confirmation(tmp_path: Path):
    """Calling open_website with javascript: scheme triggers no confirm_callback and logs precheck_failed."""
    from tools.browser import make_browser_tools

    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    reg = ToolRegistry()
    for t in make_browser_tools():
        reg.register(t)

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="open_website", args={"url": "javascript:alert(1)"})]),
        LLMResponse(text="Cannot open dangerous URL."),
    ])

    confirm_called = False

    def confirm_callback(name: str, args: dict, warning=None) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Open alert url")
    assert reply == "Cannot open dangerous URL."
    assert confirm_called is False

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "open_website"
    assert records[0]["decision"] == "precheck_failed"
    assert records[0]["outcome"] == "blocked"
    assert "not permitted" in records[0]["result"].lower()


def test_create_file_traversal_skips_confirmation(tmp_path: Path):
    """Calling create_file with path traversal triggers no confirm_callback and logs precheck_failed."""
    from tools.files import make_file_tools

    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    reg = ToolRegistry()
    for t in make_file_tools(tmp_path):
        reg.register(t)

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="create_file", args={"path": "../escape.txt", "content": "evil"})]),
        LLMResponse(text="Path traversal was blocked."),
    ])

    confirm_called = False

    def confirm_callback(name: str, args: dict, warning=None) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    agent = Agent(
        provider=provider,
        registry=reg,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_callback,
    )

    reply = agent.chat("Create file outside")
    assert reply == "Path traversal was blocked."
    assert confirm_called is False

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "create_file"
    assert records[0]["decision"] == "precheck_failed"
    assert records[0]["outcome"] == "blocked"
    assert "traversal" in records[0]["result"].lower()

