"""Tests for action claim detection, verification heuristics, and agent integration."""

from pathlib import Path
from typing import Optional

from core.agent import Agent
from core.claims import Claim, FAMILIES, find_action_claims, unverified_claims
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.task import StepRecord
from evals.checks import RunObservation
from evals.compare import parse_run_data, render_comparison_markdown
from evals.metrics import CaseResult, aggregate
from evals.report import render_markdown
from evals.schema import EvalCase, compute_cases_sha256
from security.audit import AuditLogger
from security.permissions import PermissionLevel
from tools.registry import Tool, ToolRegistry


class ScriptedProvider(LLMProvider):
    """Test provider returning pre-scripted LLMResponse items."""

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


# ---------------------------------------------------------------------------
# Unit tests for core/claims.py
# ---------------------------------------------------------------------------

def test_claims_families_structure():
    """Verify claim families map to expected tool sets."""
    assert FAMILIES["memory"] == {"remember_fact"}
    assert FAMILIES["file"] == {"create_file", "create_folder", "move_file", "write_note"}
    assert FAMILIES["app"] == {
        "open_application",
        "close_application",
        "open_website",
        "search_web",
        "open_workspace_folder",
        "open_in_vscode",
    }


def test_find_action_claims_memory():
    """Verify memory action claims are detected."""
    c1 = find_action_claims("I'll remember that.")
    assert len(c1) == 1
    assert c1[0].kind == "memory"
    assert "remember" in c1[0].phrase

    c2 = find_action_claims("I've saved that.")
    assert len(c2) == 1
    assert c2[0].kind == "memory"

    c3 = find_action_claims("I will store your preference in memory.")
    assert len(c3) == 1
    assert c3[0].kind == "memory"

    c4 = find_action_claims("I have noted that for future reference.")
    assert len(c4) == 1
    assert c4[0].kind == "memory"

    c5 = find_action_claims("I'll keep that in mind.")
    assert len(c5) == 1
    assert c5[0].kind == "memory"


def test_find_action_claims_file():
    """Verify file action claims are detected."""
    c1 = find_action_claims("I've created the file notes.txt for you.")
    assert len(c1) == 1
    assert c1[0].kind == "file"

    c2 = find_action_claims("I have wrote a note.")
    assert len(c2) == 1
    assert c2[0].kind == "file"

    c3 = find_action_claims("I've deleted the old folder.")
    assert len(c3) == 1
    assert c3[0].kind == "file"

    c4 = find_action_claims("I have moved the file to backup.")
    assert len(c4) == 1
    assert c4[0].kind == "file"


def test_find_action_claims_app():
    """Verify app action claims are detected."""
    c1 = find_action_claims("I've opened YouTube.")
    assert len(c1) == 1
    assert c1[0].kind == "app"

    c2 = find_action_claims("I have launched Notepad.")
    assert len(c2) == 1
    assert c2[0].kind == "app"

    c3 = find_action_claims("I've closed the browser.")
    assert len(c3) == 1
    assert c3[0].kind == "app"


def test_find_action_claims_negations_and_inability_produce_no_claims():
    """Sentences with negations or inability before verb must NOT produce claims."""
    assert find_action_claims("I can't remember that.") == []
    assert find_action_claims("I cannot remember that.") == []
    assert find_action_claims("I won't save passwords.") == []
    assert find_action_claims("I will not save passwords.") == []
    assert find_action_claims("I don't remember that.") == []
    assert find_action_claims("I do not store passwords.") == []
    assert find_action_claims("I am unable to remember that.") == []
    assert find_action_claims("I never remember sensitive information.") == []
    assert find_action_claims("I am not able to save that.") == []
    assert find_action_claims("I've not created the file.") == []
    assert find_action_claims("I couldn't open YouTube.") == []


def test_unverified_claims_tool_status_checks():
    """Claims must be verified only if the matching family had a 'success' event."""
    claim_mem = Claim(kind="memory", phrase="I'll remember that.")
    claim_app = Claim(kind="app", phrase="I've opened YouTube.")

    # No events -> unverified
    assert unverified_claims([claim_mem], []) == [claim_mem]

    # Errored tool event -> unverified
    err_step = StepRecord(index=1, tool="remember_fact", args={}, status="error", result="Failed", duration_ms=10)
    assert unverified_claims([claim_mem], [err_step]) == [claim_mem]

    # Denied tool event -> unverified
    denied_step = StepRecord(index=1, tool="remember_fact", args={}, status="denied", result="Denied", duration_ms=10)
    assert unverified_claims([claim_mem], [denied_step]) == [claim_mem]

    # Blocked tool event -> unverified
    blocked_step = StepRecord(index=1, tool="remember_fact", args={}, status="blocked", result="Blocked", duration_ms=10)
    assert unverified_claims([claim_mem], [blocked_step]) == [claim_mem]

    # Successful remember_fact -> verified
    ok_mem_step = StepRecord(index=1, tool="remember_fact", args={}, status="success", result="OK", duration_ms=10)
    assert unverified_claims([claim_mem], [ok_mem_step]) == []

    # Successful open_website verifies app claim
    ok_app_step = StepRecord(index=1, tool="open_website", args={"url": "https://youtube.com"}, status="success", result="OK", duration_ms=10)
    assert unverified_claims([claim_app], [ok_app_step]) == []

    # Cross-family tool does not verify: ok_app does not verify memory claim
    assert unverified_claims([claim_mem], [ok_app_step]) == [claim_mem]


# ---------------------------------------------------------------------------
# Agent end-to-end integration tests
# ---------------------------------------------------------------------------

def test_agent_flagged_claim_without_tool(tmp_path: Path):
    """'I'll remember that.' with no tool is flagged with correction appended, history saved, audit recorded."""
    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(str(audit_file))

    provider = ScriptedProvider([
        LLMResponse(text="I'll remember that.", tool_calls=[]),
    ])

    agent = Agent(provider=provider, registry=ToolRegistry(), audit=audit)
    reply = agent.chat("Remember my name is Alice")

    expected_warning = "\n\n[ORBIT check] I said I did something, but no tool actually did it, so nothing was changed."
    assert expected_warning in reply
    assert reply == "I'll remember that." + expected_warning

    # Check history
    assert len(agent.history) == 2
    assert agent.history[1]["role"] == "assistant"
    assert agent.history[1]["content"] == reply

    # Check run trace
    assert agent.last_run is not None
    assert agent.last_run.unverified_claims == 1
    run_dict = agent.last_run.to_dict()
    assert run_dict["unverified_claims"] == 1

    # Check audit log
    entries = audit.read_all()
    claim_entries = [e for e in entries if e.get("decision") == "claim_check"]
    assert len(claim_entries) == 1
    assert claim_entries[0]["outcome"] == "flagged"
    assert claim_entries[0]["args"]["count"] == 1


def test_agent_claim_with_successful_tool_not_flagged(tmp_path: Path):
    """'I'll remember that.' after successful remember_fact is NOT flagged."""
    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(str(audit_file))
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="remember_fact",
            description="Save fact",
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            func=lambda fact: "Fact remembered",
            permission=PermissionLevel.SAFE,
        )
    )

    provider = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="remember_fact", args={"fact": "Alice"})]),
        LLMResponse(text="I'll remember that.", tool_calls=[]),
    ])

    agent = Agent(provider=provider, registry=registry, audit=audit)
    reply = agent.chat("Remember Alice")

    assert "[ORBIT check]" not in reply
    assert reply == "I'll remember that."
    assert agent.last_run.unverified_claims == 0

    # No claim_check audit entry
    entries = audit.read_all()
    assert not any(e.get("decision") == "claim_check" for e in entries)


def test_agent_negation_sentences_not_flagged():
    """'I can't remember that' and 'I won't save passwords' are not flagged."""
    provider1 = ScriptedProvider([
        LLMResponse(text="I can't remember that.", tool_calls=[]),
    ])
    agent1 = Agent(provider=provider1, registry=ToolRegistry())
    reply1 = agent1.chat("Do you remember?")
    assert "[ORBIT check]" not in reply1
    assert reply1 == "I can't remember that."
    assert agent1.last_run.unverified_claims == 0

    provider2 = ScriptedProvider([
        LLMResponse(text="I won't save passwords.", tool_calls=[]),
    ])
    agent2 = Agent(provider=provider2, registry=ToolRegistry())
    reply2 = agent2.chat("Save my password")
    assert "[ORBIT check]" not in reply2
    assert reply2 == "I won't save passwords."
    assert agent2.last_run.unverified_claims == 0


def test_agent_app_claim_with_and_without_tool():
    """'I've opened YouTube' without tool is flagged; after successful open_website is not."""
    # Without tool -> flagged
    p_no_tool = ScriptedProvider([
        LLMResponse(text="I've opened YouTube.", tool_calls=[]),
    ])
    agent_no_tool = Agent(provider=p_no_tool, registry=ToolRegistry())
    reply_no_tool = agent_no_tool.chat("Open YouTube")
    assert "[ORBIT check]" in reply_no_tool
    assert agent_no_tool.last_run.unverified_claims == 1

    # With tool -> not flagged
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="open_website",
            description="Open site",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            func=lambda url: "Opened URL",
            permission=PermissionLevel.SAFE,
        )
    )

    p_with_tool = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="open_website", args={"url": "https://youtube.com"})]),
        LLMResponse(text="I've opened YouTube.", tool_calls=[]),
    ])
    agent_with_tool = Agent(provider=p_with_tool, registry=registry, command_shortcuts=False)
    reply_ok = agent_with_tool.chat("Open YouTube")
    assert "[ORBIT check]" not in reply_ok
    assert reply_ok == "I've opened YouTube."
    assert agent_with_tool.last_run.unverified_claims == 0


def test_agent_denied_or_errored_tool_does_not_verify_claim():
    """A denied or errored tool does not count as verification for action claims."""
    # 1. Errored tool
    registry_err = ToolRegistry()
    registry_err.register(
        Tool(
            name="create_file",
            description="Create file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            func=lambda path: "ERROR: disk full",
            permission=PermissionLevel.SAFE,
        )
    )

    p_err = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="create_file", args={"path": "a.txt"})]),
        LLMResponse(text="I've created the file.", tool_calls=[]),
    ])
    agent_err = Agent(provider=p_err, registry=registry_err)
    reply_err = agent_err.chat("Create file")
    assert "[ORBIT check]" in reply_err
    assert agent_err.last_run.unverified_claims == 1

    # 2. Denied tool
    registry_confirm = ToolRegistry()
    registry_confirm.register(
        Tool(
            name="create_file",
            description="Create file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            func=lambda path: "File created",
            permission=PermissionLevel.CONFIRM,
        )
    )

    p_denied = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="create_file", args={"path": "b.txt"})]),
        LLMResponse(text="I've created the file.", tool_calls=[]),
    ])
    from security.permissions import PermissionPolicy

    # confirm_callback returning False denies permission
    agent_denied = Agent(
        provider=p_denied,
        registry=registry_confirm,
        policy=PermissionPolicy(),
        confirm_callback=lambda tool, args, **kwargs: False,
    )
    reply_denied = agent_denied.chat("Create file")
    assert "[ORBIT check]" in reply_denied
    assert agent_denied.last_run.unverified_claims == 1


def test_agent_streaming_emits_correction():
    """When streaming, the correction string is emitted through on_delta."""
    deltas = []

    def on_delta(delta: str):
        deltas.append(delta)

    provider = ScriptedProvider([
        LLMResponse(text="I'll remember that.", tool_calls=[]),
    ])
    agent = Agent(provider=provider, registry=ToolRegistry())
    reply = agent.chat("Remember this", on_delta=on_delta)

    full_streamed = "".join(deltas)
    assert "[ORBIT check]" in full_streamed
    assert reply == "I'll remember that.\n\n[ORBIT check] I said I did something, but no tool actually did it, so nothing was changed."


def test_agent_run_without_claims_unchanged():
    """A response without claims passes through completely unchanged."""
    provider = ScriptedProvider([
        LLMResponse(text="Paris is the capital of France.", tool_calls=[]),
    ])
    agent = Agent(provider=provider, registry=ToolRegistry())
    reply = agent.chat("What is the capital of France?")
    assert reply == "Paris is the capital of France."
    assert agent.last_run.unverified_claims == 0


# ---------------------------------------------------------------------------
# Evals metric, report & compare tests
# ---------------------------------------------------------------------------

def test_evals_metrics_and_report_headline(tmp_path: Path):
    """RunObservation and CaseResult store unverified_claims; headline reflects them."""
    obs = RunObservation(
        final_texts=["I'll remember that."],
        events=[],
        launches=[],
        llm_calls=1,
        latency_s=0.5,
        workspace=tmp_path,
        unverified_claims=2,
    )
    assert obs.unverified_claims == 2

    c1 = CaseResult(id="c1", category="memory", repeat=0, status="passed", unverified_claims=1)
    c2 = CaseResult(id="c2", category="chat", repeat=0, status="passed", unverified_claims=0)

    agg = aggregate([c1, c2], [EvalCase(id="c1", category="memory", turns=["t1"]), EvalCase(id="c2", category="chat", turns=["t2"])])
    assert agg["overall"]["unverified_claims_headline"] == "1 of 2 runs"

    # If evaluated results is empty -> "n/a"
    agg_empty = aggregate([], [])
    assert agg_empty["overall"]["unverified_claims_headline"] == "n/a"

    # Report markdown includes the headline
    report_data = {
        "meta": {"mode": "mock", "provider": "mock"},
        "results": [c1.to_dict(), c2.to_dict()],
    }
    report_md = render_markdown(report_data, [EvalCase(id="c1", category="memory", turns=["t1"]), EvalCase(id="c2", category="chat", turns=["t2"])])
    assert "- **Unverified action claims**: 1 of 2 runs" in report_md


def test_compare_warns_when_cases_sha256_differs():
    """--compare warns when cases_sha256 in run A and run B differ."""
    data_a = {
        "meta": {"cases_sha256": "aaaa11112222", "repeats": 1},
        "results": [{"id": "c1", "status": "passed", "category": "chat"}],
    }
    data_b = {
        "meta": {"cases_sha256": "bbbb33334444", "repeats": 1},
        "results": [{"id": "c1", "status": "passed", "category": "chat"}],
    }

    run_a = parse_run_data(data_a, "run_a")
    run_b = parse_run_data(data_b, "run_b")

    md = render_comparison_markdown(run_a, run_b)
    assert "Warning: Case definition hash mismatch" in md
    assert "aaaa1111" in md or "bbbb3333" in md


def test_compute_cases_sha256_deterministic(tmp_path: Path):
    """compute_cases_sha256 returns a deterministic hash over case files."""
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "chat.json").write_text('[{"id": "test"}]', encoding="utf-8")

    h1 = compute_cases_sha256(cases_dir)
    h2 = compute_cases_sha256(cases_dir)
    assert h1 == h2
    assert len(h1) == 64
