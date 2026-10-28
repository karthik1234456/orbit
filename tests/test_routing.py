"""Tests for optional tool routing in ORBIT v2.7.0."""

import os
from pathlib import Path

import pytest

from browser.backend import FakeBackend
from core.bootstrap import AppPaths, build_app
from core.diagnostics import build_diagnostics_text
from core.llm import LLMResponse, MockProvider, ToolCall
from core.routing import (
    FALLBACK_GROUPS,
    GROUPS,
    TOOL_TO_GROUP,
    matches_inability,
    offered_tools,
    select_groups,
)
from core.session_state import SessionState
from scripts.intent_probe import (
    check_test_split_protection,
    load_probe_phrases,
    record_test_run,
)
from tools.registry import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# Unit tests for core/routing.py
# ---------------------------------------------------------------------------

def test_groups_and_reverse_mapping_integrity():
    """Verify all 9 groups exist, tools map back, and no collisions exist."""
    expected_groups = {
        "system",
        "notes",
        "files",
        "apps",
        "web",
        "browser",
        "memory",
        "docs",
        "reminders",
    }
    assert set(GROUPS.keys()) == expected_groups

    # Check reverse mapping consistency
    for group_name, tools in GROUPS.items():
        for tool_name in tools:
            assert TOOL_TO_GROUP[tool_name] == group_name

    # Check no duplicate tool assignments across different groups
    all_tools = []
    for tools in GROUPS.values():
        all_tools.extend(tools)
    assert len(all_tools) == len(set(all_tools))


def test_select_groups_always_includes_system():
    """select_groups must always include 'system' group."""
    groups = select_groups("random unknown phrase")
    assert "system" in groups


def test_select_groups_for_each_functional_area():
    """select_groups correctly activates each group based on keywords."""
    cases = [
        ("what is the current time", "system"),
        ("create a note about meeting", "notes"),
        ("list files in workspace", "files"),
        ("open notepad application", "apps"),
        ("search google for weather", "web"),
        ("click result 2 on the page", "browser"),
        ("remember that my favorite color is blue", "memory"),
        ("search docs for installation instructions", "docs"),
        ("remind me in 10 minutes to take medicine", "reminders"),
    ]
    for prompt, expected_group in cases:
        selected = select_groups(prompt)
        assert expected_group in selected, f"Expected {expected_group} for prompt: {prompt}"


def test_select_groups_fallback_on_unclassified_chat():
    """General conversation matching no action group must return FALLBACK_GROUPS."""
    chat_phrases = [
        "hello there",
        "how are you doing today?",
        "tell me a funny joke",
        "who are you?",
    ]
    for phrase in chat_phrases:
        selected = select_groups(phrase)
        assert selected == FALLBACK_GROUPS
        assert selected == {"system", "web", "memory", "docs"}


def test_select_groups_prior_tools_continuity():
    """Active groups from prior_tools are carried forward into selected groups."""
    selected = select_groups("now check the second one", prior_tools=["list_files"])
    assert "files" in selected
    assert "system" in selected


def test_select_groups_browser_session_continuity():
    """Controlled open browser in SessionState automatically includes browser and web."""
    state = SessionState()
    state.browser_open = True
    state.browser_controlled = True
    state.last_url = "https://en.wikipedia.org"

    selected = select_groups("next paragraph please", state=state)
    assert "browser" in selected
    assert "web" in selected


def test_offered_tools_filtering():
    """offered_tools returns registered tools matching groups + any ungrouped tools."""
    registry = ToolRegistry()
    t_sys = Tool("get_current_time", "time", {}, lambda: "12:00")
    t_file = Tool("read_file", "read", {}, lambda path: "content")
    t_custom = Tool("custom_ungrouped_tool", "custom", {}, lambda: "ok")

    registry.register(t_sys)
    registry.register(t_file)
    registry.register(t_custom)

    all_tools = registry.list_tools()

    # If only system is selected, read_file is excluded, custom is included
    offered = offered_tools(all_tools, {"system"})
    offered_names = {t.name for t in offered}
    assert "get_current_time" in offered_names
    assert "custom_ungrouped_tool" in offered_names
    assert "read_file" not in offered_names

    # If files is added, read_file is now included
    offered_files = offered_tools(all_tools, {"system", "files"})
    assert "read_file" in {t.name for t in offered_files}


def test_matches_inability_heuristics():
    """matches_inability detects standard refusal and inability phrases."""
    positive_phrases = [
        "I can't access that file right now.",
        "I cannot perform file operations.",
        "I am unable to find any notes.",
        "I don't have the ability to read documents.",
        "I'm not able to open applications.",
        "I am not able to execute calculations.",
        "I do not have access to the file system.",
        "There is no way to reach that resource.",
    ]
    for text in positive_phrases:
        assert matches_inability(text) is True, f"Failed to match inability: {text}"

    negative_phrases = [
        "Sure, I found the file notes.txt in your workspace.",
        "The current time is 14:30.",
        "The calculated result is 42.",
        "Hello! How can I help you today?",
    ]
    for text in negative_phrases:
        assert matches_inability(text) is False, f"False positive inability: {text}"


# ---------------------------------------------------------------------------
# Agent integration tests with MockProvider via build_app
# ---------------------------------------------------------------------------

class TrackingProvider(MockProvider):
    """MockProvider that captures messages and tools for each generate call."""

    def __init__(self, responses=None):
        super().__init__(responses)
        self.calls: list[dict] = []

    def generate(self, messages, tools=None, on_delta=None, should_stop=None):
        self.calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else [],
        })
        return super().generate(messages, tools, on_delta, should_stop)


def test_agent_routing_disabled_by_default(tmp_path):
    """When TOOL_ROUTING is disabled, all registry tools are offered."""
    provider = TrackingProvider()
    app = build_app(provider=provider, paths=AppPaths.under(tmp_path), tool_routing=False)
    agent = app.agent
    registry = app.registry

    assert agent.tool_routing_enabled() is False

    agent.chat("list files")
    last_call = provider.calls[-1]
    offered_names = {t.name for t in last_call["tools"]}
    assert offered_names == {t.name for t in registry.list_tools()}

    # Verify TaskRun trace
    assert agent.last_run.routing_on is False
    assert agent.last_run.tools_offered_count == len(registry.list_tools())
    assert agent.last_run.tools_registered_count == len(registry.list_tools())


def test_agent_routing_enabled_narrows_tools(tmp_path):
    """When TOOL_ROUTING is enabled, offered tools are filtered by select_groups."""
    provider = TrackingProvider()
    app = build_app(provider=provider, paths=AppPaths.under(tmp_path), tool_routing=True)
    agent = app.agent
    registry = app.registry

    assert agent.tool_routing_enabled() is True

    agent.chat("what time is it?")
    last_call = provider.calls[-1]
    offered_names = {t.name for t in last_call["tools"]}

    # Fallback/system groups offered (much fewer than full registry)
    assert len(offered_names) < len(registry.list_tools())
    assert "get_current_time" in offered_names
    assert "create_file" not in offered_names
    assert "open_application" not in offered_names

    # Verify TaskRun trace fields
    assert agent.last_run.routing_on is True
    assert agent.last_run.tools_offered_count == len(offered_names)
    assert agent.last_run.tools_registered_count == len(registry.list_tools())
    assert agent.last_run.off_route_call == 0
    assert agent.last_run.routing_retry == 0


def test_agent_off_route_call_executes_and_logs(tmp_path):
    """If model generates a tool call not in offered set, it executes and records off_route_call."""
    # Note: prompt matches 'system' / 'time', but mock responses call 'list_files'
    provider = TrackingProvider(
        responses=[
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="list_files", args={})],
            ),
            LLMResponse(text="I found 0 files."),
        ]
    )

    app = build_app(provider=provider, paths=AppPaths.under(tmp_path), tool_routing=True)
    agent = app.agent

    agent.chat("what time is it?")

    # Tool executed normally
    assert agent.last_run.steps[0].tool == "list_files"
    assert agent.last_run.steps[0].status == "success"
    # Off route call recorded in trace
    assert agent.last_run.off_route_call == 1


def test_agent_miss_recovery_retries_once_on_inability(tmp_path):
    """When routing narrowed tools and model replies with inability, agent retries once with all tools."""
    # First call: refusal text ("I cannot access files")
    # Second call (retry): model calls list_files, then finishes
    provider = TrackingProvider(
        responses=[
            LLMResponse(text="I am unable to view your files."),
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="list_files", args={})],
            ),
            LLMResponse(text="Here are the files: none."),
        ]
    )

    app = build_app(provider=provider, paths=AppPaths.under(tmp_path), tool_routing=True)
    agent = app.agent
    registry = app.registry

    reply = agent.chat("check my files")

    # Retry occurred
    assert agent.last_run.routing_retry == 1
    # Check that retry offered full tool set
    retry_call = provider.calls[1]
    assert len(retry_call["tools"]) == len(registry.list_tools())
    # Final reply from completed retry
    assert "Here are the files" in reply


def test_agent_miss_recovery_never_triggers_when_routing_off(tmp_path):
    """Miss recovery does not trigger when tool routing is disabled."""
    provider = TrackingProvider(
        responses=[
            LLMResponse(text="I am unable to view your files."),
        ]
    )

    app = build_app(provider=provider, paths=AppPaths.under(tmp_path), tool_routing=False)
    agent = app.agent

    reply = agent.chat("check my files")

    assert agent.last_run.routing_retry == 0
    assert len(provider.calls) == 1
    assert "unable to view" in reply


def test_agent_miss_recovery_never_triggers_after_tool_call(tmp_path):
    """Miss recovery does not trigger if a tool call was already executed in the turn."""
    provider = TrackingProvider(
        responses=[
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="get_current_time", args={})],
            ),
            LLMResponse(text="I cannot perform any further actions."),
        ]
    )

    app = build_app(provider=provider, paths=AppPaths.under(tmp_path), tool_routing=True)
    agent = app.agent

    reply = agent.chat("what time is it?")

    # Tool was executed, so no retry
    assert agent.last_run.routing_retry == 0
    assert len(provider.calls) == 2
    assert "cannot perform" in reply


# ---------------------------------------------------------------------------
# Diagnostics tests
# ---------------------------------------------------------------------------

def test_diagnostics_routing_section_and_zero_message_text(tmp_path):
    """Diagnostics includes === TOOL ROUTING === with zero conversational message text."""
    app = build_app(provider=MockProvider(), paths=AppPaths.under(tmp_path), tool_routing=True)
    agent = app.agent

    secret_prompt = "TOP_SECRET_USER_QUERY_12345"
    agent.chat(secret_prompt)

    report = build_diagnostics_text(agent=agent)
    assert "=== TOOL ROUTING ===" in report
    assert "TOOL_ROUTING: True" in report
    assert "Last Turn Routing Active: True" in report
    assert "Tools Offered:" in report
    assert "Tools Registered:" in report

    # Strictly zero message text
    assert secret_prompt not in report


# ---------------------------------------------------------------------------
# Eval isolation tests
# ---------------------------------------------------------------------------

def test_eval_runner_forces_routing_off(monkeypatch):
    """evals/runner.py _isolated_eval_environment forces TOOL_ROUTING=0."""
    from evals.runner import _isolated_eval_environment

    monkeypatch.delenv("TOOL_ROUTING", raising=False)
    with _isolated_eval_environment():
        assert os.environ.get("TOOL_ROUTING") == "0"


# ---------------------------------------------------------------------------
# Intent probe and holdout split tests
# ---------------------------------------------------------------------------

def test_intent_probe_split_protection(tmp_path):
    """intent_probe refuses test.json without --final-test and blocks duplicate runs."""
    fake_test = tmp_path / "evals" / "routing" / "test.json"
    fake_ran = tmp_path / ".ran"

    # 1. Refuse without --final-test
    with pytest.raises(PermissionError):
        check_test_split_protection(fake_test, final_test=False, force=False, ran_path=fake_ran)

    # 2. First final test allowed
    is_test, warning = check_test_split_protection(fake_test, final_test=True, force=False, ran_path=fake_ran)
    assert is_test is True
    assert warning is None

    # 3. Record test run
    record_test_run(routing_enabled=False, ran_path=fake_ran)
    assert fake_ran.exists()

    # 4. Second final test with same routing setting refused without --force
    with pytest.raises(PermissionError):
        check_test_split_protection(fake_test, final_test=True, force=False, routing_enabled=False, ran_path=fake_ran)

    # 5. Second final test with --force allowed, warning returned
    is_test, warning = check_test_split_protection(fake_test, final_test=True, force=True, routing_enabled=False, ran_path=fake_ran)
    assert is_test is True
    assert warning == "RERUN: NOT UNSEEN"


def test_dev_routing_split_phrases_validation(tmp_path):
    """Validate evals/routing/dev.json structure, vocabulary, and item counts."""
    dev_path = Path(__file__).resolve().parent.parent / "evals" / "routing" / "dev.json"
    assert dev_path.exists(), "evals/routing/dev.json must exist"

    items = load_probe_phrases(dev_path)
    assert len(items) == 36, f"Expected 36 phrases, found {len(items)}"

    app = build_app(
        provider=MockProvider(),
        paths=AppPaths.under(tmp_path),
        browser_backend_factory=lambda: FakeBackend(),
    )
    vocab = {t.name for t in app.registry.list_tools()}

    action_count = 0
    none_count = 0

    for phrase, accept, prior in items:
        assert phrase and phrase.strip()
        assert isinstance(accept, list)

        if len(accept) == 0:
            none_count += 1
        else:
            action_count += 1
            for accepted_tool in accept:
                assert accepted_tool in vocab, f"Unknown tool in dev.json: {accepted_tool}"

    assert action_count == 24, f"Expected 24 action phrases, found {action_count}"
    assert none_count == 12, f"Expected 12 none phrases, found {none_count}"
