"""Privacy and PII tests for ORBIT messaging and contacts.

Verifies that phone numbers, email addresses, and message bodies NEVER appear in:
- agent.history
- run.steps and RunLogger entries
- audit.log
- SessionState context block
- diagnostics report
- doctor checks
- LLM prompts (model visibility)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from config import settings
from contacts.store import ContactStore
from core.agent import Agent
from core.diagnostics import build_diagnostics_text
from core.doctor import check_messaging
from core.llm import LLMProvider, LLMResponse
from core.session_state import SessionState
from core.task import RunLogger
from security.audit import AuditLogger
from tools.messaging_tools import make_messaging_tools
from tools.registry import Tool, ToolRegistry


SECRET_PHONE = "15550100123"
SECRET_EMAIL = "testuser@example.com"
SECRET_BODY = "SECRET_MESSAGE_BODY_XYZ"
CONTACT_NAME = "Alice Secret"


class FakeProvider(LLMProvider):
    """LLM provider recording last prompt messages and tools."""

    def __init__(self, fixed_response: str = "Test response"):
        self.fixed_response = fixed_response
        self.last_messages: list[dict] = []
        self.last_tools: Optional[list[Tool]] = None

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
        on_delta: Optional[Any] = None,
        should_stop: Optional[Any] = None,
    ) -> LLMResponse:
        self.last_messages = list(messages)
        self.last_tools = list(tools) if tools else None
        return LLMResponse(text=self.fixed_response)


def _assert_no_pii_or_body(text: str, label: str):
    """Assert that none of the secret tokens appear in the text."""
    assert SECRET_PHONE not in text, f"Found phone in {label}"
    assert SECRET_EMAIL not in text, f"Found email in {label}"
    assert SECRET_BODY not in text, f"Found body in {label}"


def _setup_agent(
    tmp_path: Path,
    provider: Optional[FakeProvider] = None,
) -> tuple[Agent, ContactStore, AuditLogger, RunLogger, SessionState, FakeProvider]:
    contacts_path = tmp_path / "contacts.db"
    contact_store = ContactStore(db_path=contacts_path)
    contact_store.add(
        display_name=CONTACT_NAME,
        phone=SECRET_PHONE,
        email=SECRET_EMAIL,
        aliases=["Alice"],
    )

    audit_path = tmp_path / "logs" / "audit.log"
    audit_logger = AuditLogger(path=audit_path)

    run_path = tmp_path / "logs" / "runs.jsonl"
    run_logger = RunLogger(path=run_path)

    state = SessionState(clock=lambda: 1000.0)

    tools = make_messaging_tools(
        contacts=contact_store,
        state_getter=lambda: state,
        opener=lambda uri: True,
        key_sender=lambda: None,
        clock=lambda: 1000.0,
    )
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)

    prov = provider or FakeProvider()
    agent = Agent(
        provider=prov,
        registry=registry,
        audit=audit_logger,
        run_logger=run_logger,
        session_state=state,
        contact_store=contact_store,
        confirm_callback=lambda tool, args, **kw: True,
        command_shortcuts=True,
    )
    return agent, contact_store, audit_logger, run_logger, state, prov


def test_whatsapp_privacy_guarantees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify phone, email, and message body never leak during WhatsApp interactions."""
    monkeypatch.setattr(settings, "MESSAGING_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_SEND_MODE", "draft")

    agent, contact_store, audit_logger, run_logger, state, prov = _setup_agent(tmp_path)

    # Execute WhatsApp shortcut turn
    user_input = f"text {CONTACT_NAME} saying {SECRET_BODY}"
    reply = agent.chat(user_input)
    assert "WhatsApp" in reply

    # 1. Check history: user turn and assistant turn
    for h in agent.history:
        content = h.get("content", "")
        _assert_no_pii_or_body(content, "agent.history")
    assert agent.history[-1]["content"] == f"Prepared a WhatsApp message to {CONTACT_NAME}."
    assert agent.history[-2]["content"] == f"Send a WhatsApp message to {CONTACT_NAME}."

    # 2. Check audit log file
    audit_file = tmp_path / "logs" / "audit.log"
    assert audit_file.exists()
    audit_text = audit_file.read_text(encoding="utf-8")
    _assert_no_pii_or_body(audit_text, "audit.log file")
    audit_entries = [json.loads(line) for line in audit_text.strip().splitlines()]
    assert len(audit_entries) >= 1
    last_audit = audit_entries[-1]
    assert last_audit["tool"] == "send_whatsapp"
    assert "contact_id" in last_audit["args"]
    assert "text" not in last_audit["args"]

    # 3. Check run traces (RunLogger / run.steps)
    runs_file = tmp_path / "logs" / "runs.jsonl"
    assert runs_file.exists()
    runs_text = runs_file.read_text(encoding="utf-8")
    _assert_no_pii_or_body(runs_text, "runs.jsonl file")

    # 4. Check SessionState context block
    ctx = state.context_block()
    _assert_no_pii_or_body(ctx, "session_state.context_block")

    # 5. Check diagnostics text
    diag_text = build_diagnostics_text(agent=agent, audit_logger=audit_logger)
    _assert_no_pii_or_body(diag_text, "build_diagnostics_text")
    assert CONTACT_NAME not in diag_text

    # 6. Check doctor check
    from core.doctor import Probes
    chk = check_messaging(probes=Probes())
    _assert_no_pii_or_body(chk.detail, "check_messaging.detail")
    assert CONTACT_NAME not in chk.detail


def test_email_privacy_guarantees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify phone, email, and message body never leak during email drafting interactions."""
    monkeypatch.setattr(settings, "MESSAGING_ENABLED", True)

    agent, contact_store, audit_logger, run_logger, state, prov = _setup_agent(tmp_path)

    # Execute Email shortcut turn
    user_input = f"email {CONTACT_NAME} about {SECRET_BODY}"
    reply = agent.chat(user_input)
    assert "email draft" in reply.lower()

    # 1. Check history
    for h in agent.history:
        content = h.get("content", "")
        _assert_no_pii_or_body(content, "agent.history")
    assert agent.history[-1]["content"] == f"Prepared an email draft to {CONTACT_NAME}."
    assert agent.history[-2]["content"] == f"Send an email to {CONTACT_NAME}."

    # 2. Check audit log
    audit_file = tmp_path / "logs" / "audit.log"
    audit_text = audit_file.read_text(encoding="utf-8")
    _assert_no_pii_or_body(audit_text, "audit.log file")

    # 3. Check run traces
    runs_file = tmp_path / "logs" / "runs.jsonl"
    runs_text = runs_file.read_text(encoding="utf-8")
    _assert_no_pii_or_body(runs_text, "runs.jsonl file")


def test_model_prompt_privacy_contacts_strictly_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that contacts, phone numbers, and messaging tools are invisible to the model."""
    monkeypatch.setattr(settings, "MESSAGING_ENABLED", True)

    agent, contact_store, audit_logger, run_logger, state, prov = _setup_agent(tmp_path)

    # Chat with a non-shortcut query that routes to the LLM
    agent.chat("explain general relativity in one short sentence")

    # The model tools offered must not contain send_whatsapp or open_email_draft
    tool_names = [t.name for t in (prov.last_tools or [])]
    assert "send_whatsapp" not in tool_names
    assert "open_email_draft" not in tool_names

    # The messages sent to the LLM must not contain contact info or PII
    for msg in prov.last_messages:
        content = str(msg.get("content", ""))
        _assert_no_pii_or_body(content, "model prompt")
        assert CONTACT_NAME not in content
