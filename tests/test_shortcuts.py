"""Tests for command shortcuts parsing, execution pipeline, security checks, and eval runner integration."""

import os
from pathlib import Path


from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.shortcuts import ToolCallSpec, parse_shortcut
from evals.report import render_markdown
from evals.runner import run_suite
from evals.schema import EvalCase
from security.audit import AuditLogger
from security.permissions import PermissionLevel, PermissionPolicy
from tools.registry import Tool, ToolRegistry


class ExplodingProvider(LLMProvider):
    """Provider that raises an error if generate() is called."""

    def __init__(self):
        self.call_count = 0

    def generate(self, messages, tools=None, on_delta=None, should_stop=None) -> LLMResponse:
        self.call_count += 1
        raise AssertionError("LLMProvider.generate was called when shortcuts should have bypassed it!")


class CountingMockProvider(LLMProvider):
    """Provider that counts calls and returns a canned response."""

    def __init__(self, reply: str = "Model response"):
        self.reply = reply
        self.call_count = 0

    def generate(self, messages, tools=None, on_delta=None, should_stop=None) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(text=self.reply)


def test_each_pattern_maps_to_the_right_tool():
    """Verify that each supported pattern correctly maps to the expected tool and arguments."""
    apps = {"notepad": {"launch": ["notepad.exe"], "process": "notepad.exe"}}
    sites = {
        "youtube": {"url": "https://www.youtube.com", "search": "https://www.youtube.com/results?search_query={query}"},
        "google": {"url": "https://www.google.com", "search": "https://www.google.com/search?q={query}"},
    }

    # 1. Application: open|launch|start <name> [app|website|site]
    spec = parse_shortcut("open notepad", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"
    assert spec.args == {"name": "notepad"}
    assert spec.target_name == "notepad"

    spec = parse_shortcut("launch notepad app", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"
    assert spec.args == {"name": "notepad"}

    spec = parse_shortcut("start notepad", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"
    assert spec.args == {"name": "notepad"}

    # Case-insensitivity & trailing punctuation
    spec = parse_shortcut("Open NOTEPAD!", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"
    assert spec.args == {"name": "notepad"}

    # Politeness prefixes
    spec = parse_shortcut("please open notepad", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"

    spec = parse_shortcut("can you launch notepad?", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"

    spec = parse_shortcut("can u start notepad...", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"

    spec = parse_shortcut("could you open notepad", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"

    # 2. Website: open|launch|start <name> [app|website|site]
    spec = parse_shortcut("open youtube", apps, sites)
    assert spec is not None
    assert spec.name == "open_website"
    assert spec.args == {"url": "https://www.youtube.com"}
    assert spec.target_name == "youtube"

    spec = parse_shortcut("launch youtube site", apps, sites)
    assert spec is not None
    assert spec.name == "open_website"
    assert spec.args == {"url": "https://www.youtube.com"}

    spec = parse_shortcut("start youtube website", apps, sites)
    assert spec is not None
    assert spec.name == "open_website"
    assert spec.args == {"url": "https://www.youtube.com"}

    # 3. Workspace folder: open (my )?(workspace )?folders?
    spec = parse_shortcut("open folder", apps, sites)
    assert spec is not None
    assert spec.name == "open_workspace_folder"
    assert spec.args == {"path": "."}

    spec = parse_shortcut("open my folder", apps, sites)
    assert spec is not None
    assert spec.name == "open_workspace_folder"
    assert spec.args == {"path": "."}

    spec = parse_shortcut("open workspace folder", apps, sites)
    assert spec is not None
    assert spec.name == "open_workspace_folder"
    assert spec.args == {"path": "."}

    spec = parse_shortcut("open my workspace folders", apps, sites)
    assert spec is not None
    assert spec.name == "open_workspace_folder"
    assert spec.args == {"path": "."}

    spec = parse_shortcut("please open my workspace folder.", apps, sites)
    assert spec is not None
    assert spec.name == "open_workspace_folder"
    assert spec.args == {"path": "."}

    # 4. Search: search <site> for <query> and <query> on <site>
    spec = parse_shortcut("search youtube for funny cats", apps, sites)
    assert spec is not None
    assert spec.name == "search_site"
    assert spec.args == {"site": "youtube", "query": "funny cats"}
    assert spec.target_name == "youtube"

    spec = parse_shortcut("funny cats on youtube", apps, sites)
    assert spec is not None
    assert spec.name == "search_site"
    assert spec.args == {"site": "youtube", "query": "funny cats"}

    spec = parse_shortcut("search google for python tips", apps, sites)
    assert spec is not None
    assert spec.name == "search_site"
    assert spec.args == {"site": "google", "query": "python tips"}

    spec = parse_shortcut("python tips on google", apps, sites)
    assert spec is not None
    assert spec.name == "search_site"
    assert spec.args == {"site": "google", "query": "python tips"}


def test_app_priority_over_site():
    """Verify that if a name is in both apps and sites, open_application is chosen."""
    apps = {"youtube": {"launch": ["youtube-app.exe"], "process": "youtube.exe"}}
    sites = {"youtube": {"url": "https://www.youtube.com", "search": "https://www.youtube.com/{query}"}}

    spec = parse_shortcut("open youtube", apps, sites)
    assert spec is not None
    assert spec.name == "open_application"
    assert spec.args == {"name": "youtube"}


def test_near_misses_return_none():
    """Verify that near-misses, extra words, complex phrases, shell metachars, newlines, and URLs return None."""
    apps = {"notepad": {"launch": ["notepad.exe"], "process": "notepad.exe"}}
    sites = {
        "youtube": {"url": "https://www.youtube.com", "search": "https://www.youtube.com/results?search_query={query}"},
    }

    near_misses = [
        "open youtube and delete my files",
        "open the youtube video about cats",
        "open youtube.com/watch?v=x",
        "open",
        "open notepad; calc",
        "open notepad & calc",
        "open notepad | calc",
        "open notepad `calc`",
        "open notepad $PATH",
        "open notepad > output.txt",
        "open notepad < input.txt",
        "open notepad\ncalc",
        "open notepad\r\ncalc",
        "open https://www.youtube.com",
        "open www.youtube.com",
        "open youtube.com",
        "search youtube for",  # 0 query words
        "search youtube for one two three four five six seven eight nine",  # 9 words (max 8)
        "one two three four five six seven eight nine on youtube",  # 9 words
        "can you please also open youtube",  # extra words
        "open youtube later",  # extra word
        "launch notepad quickly",  # extra word
    ]

    for phrase in near_misses:
        assert parse_shortcut(phrase, apps, sites) is None, f"Expected None for near-miss: {phrase!r}"


def test_unknown_site_or_app_returns_none():
    """Verify that unknown applications and websites return None."""
    apps = {"notepad": {"launch": ["notepad.exe"], "process": "notepad.exe"}}
    sites = {"youtube": {"url": "https://www.youtube.com", "search": "https://www.youtube.com/{query}"}}

    assert parse_shortcut("open unknownapp", apps, sites) is None
    assert parse_shortcut("launch unknownapp app", apps, sites) is None
    assert parse_shortcut("start unknownsite website", apps, sites) is None
    assert parse_shortcut("search unknownsite for cats", apps, sites) is None
    assert parse_shortcut("cats on unknownsite", apps, sites) is None


def test_shortcut_turn_does_not_call_model_and_records_audit(tmp_path: Path):
    """Verify that model provider is never called on a shortcut turn, and audit has decision 'shortcut'."""
    registry = ToolRegistry()

    def fake_open_app(name: str) -> str:
        return f"Successfully opened {name}."

    registry.register(
        Tool(
            name="open_application",
            description="Launch an app",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            func=fake_open_app,
            permission=PermissionLevel.SAFE,
        )
    )

    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(path=str(audit_file))
    provider = ExplodingProvider()

    apps = {"notepad": {"launch": ["notepad.exe"], "process": "notepad.exe"}}
    agent = Agent(
        provider=provider,
        registry=registry,
        audit=audit,
        allowed_apps=apps,
        sites={},
        command_shortcuts=True,
    )

    reply = agent.chat("open notepad")
    assert reply == "Opened notepad."
    assert provider.call_count == 0

    # Verify history
    assert len(agent.history) == 2
    assert agent.history[0] == {"role": "user", "content": "open notepad"}
    assert agent.history[1] == {"role": "assistant", "content": "Opened notepad."}

    # Verify task run
    assert agent.last_run is not None
    assert agent.last_run.llm_calls == 0
    assert len(agent.last_run.steps) == 1
    assert agent.last_run.steps[0].tool == "open_application"
    assert agent.last_run.steps[0].status == "success"
    assert agent.last_run.steps[0].decision == "shortcut"

    # Verify audit log
    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "open_application"
    assert records[0]["decision"] == "shortcut"
    assert records[0]["outcome"] == "success"


def test_confirm_tool_calls_confirm_callback_and_denial_executes_nothing(tmp_path: Path):
    """Verify that a CONFIRM tool still triggers confirm_callback and denial executes nothing."""
    registry = ToolRegistry()
    executed = []

    def fake_open_workspace_folder(path: str = ".") -> str:
        executed.append(path)
        return f"Opened folder '{path}' in Windows Explorer."

    registry.register(
        Tool(
            name="open_workspace_folder",
            description="Open workspace folder",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
            func=fake_open_workspace_folder,
            permission=PermissionLevel.CONFIRM,
        )
    )

    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(path=str(audit_file))
    provider = ExplodingProvider()
    policy = PermissionPolicy()  # CONFIRM tools produce Decision.ASK

    # 1. Deny scenario
    confirm_calls = []

    def deny_callback(tool_name: str, args: dict, warning=None) -> bool:
        confirm_calls.append((tool_name, args, warning))
        return False

    agent = Agent(
        provider=provider,
        registry=registry,
        policy=policy,
        audit=audit,
        confirm_callback=deny_callback,
        command_shortcuts=True,
    )

    reply = agent.chat("open workspace folder")
    assert "ERROR: user denied permission for open_workspace_folder" in reply
    assert len(confirm_calls) == 1
    assert confirm_calls[0][0] == "open_workspace_folder"
    assert executed == []  # Nothing executed!

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "open_workspace_folder"
    assert records[0]["decision"] == "user_denied"
    assert records[0]["outcome"] == "blocked"

    # 2. Approve scenario
    confirm_calls.clear()

    def approve_callback(tool_name: str, args: dict, warning=None) -> bool:
        confirm_calls.append((tool_name, args, warning))
        return True

    agent.confirm_callback = approve_callback
    reply = agent.chat("open workspace folder")
    assert reply == "Opened workspace folder."
    assert len(confirm_calls) == 1
    assert executed == ["."]  # Executed once!

    records = audit.read_all()
    assert len(records) == 2
    assert records[1]["tool"] == "open_workspace_folder"
    assert records[1]["decision"] == "shortcut"
    assert records[1]["outcome"] == "success"


def test_command_shortcuts_disabled_calls_model():
    """Verify that when COMMAND_SHORTCUTS=0 (or command_shortcuts=False), the model is called."""
    provider = CountingMockProvider(reply="I am thinking about opening notepad.")
    apps = {"notepad": {"launch": ["notepad.exe"], "process": "notepad.exe"}}

    agent = Agent(
        provider=provider,
        allowed_apps=apps,
        command_shortcuts=False,
    )

    reply = agent.chat("open notepad")
    assert reply == "I am thinking about opening notepad."
    assert provider.call_count == 1

    # Also test environment variable override
    os.environ["COMMAND_SHORTCUTS"] = "0"
    try:
        agent_env = Agent(
            provider=provider,
            allowed_apps=apps,
            command_shortcuts=None,
        )
        agent_env.chat("open notepad")
        assert provider.call_count == 2
    finally:
        os.environ.pop("COMMAND_SHORTCUTS", None)


def test_eval_runner_disables_shortcuts_by_default_and_records(tmp_path: Path):
    """Verify that eval runner sets COMMAND_SHORTCUTS=0 by default, records it in meta, and accepts shortcuts."""
    # Ensure environment is clean
    os.environ.pop("COMMAND_SHORTCUTS", None)

    case = EvalCase(
        id="test_shortcut_case",
        category="apps",
        turns=["open notepad"],
        expect_tools=["open_application"],
        checks=[],
    )

    call_records = []

    def mock_factory(c, r=0):
        class RecordingProvider(LLMProvider):
            def generate(self, messages, tools=None, on_delta=None, should_stop=None):
                call_records.append(messages)
                return LLMResponse(
                    text="Opening notepad",
                    tool_calls=[ToolCall(name="open_application", args={"name": "notepad"})],
                )

        return RecordingProvider()

    out_file = tmp_path / "eval_results.json"

    # Default run: shortcuts disabled
    results, quota_hit = run_suite(
        cases=[case],
        provider_factory=mock_factory,
        out_path=out_file,
    )

    # Provider MUST have been called because shortcuts were disabled by default
    assert len(call_records) > 0

    # Check meta recorded COMMAND_SHORTCUTS: False
    import json
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["meta"]["COMMAND_SHORTCUTS"] is False

    # Check report header contains - **COMMAND_SHORTCUTS**: off
    md = render_markdown(data, [case])
    assert "- **COMMAND_SHORTCUTS**: off" in md

    # Now run with --shortcuts enabled
    call_records.clear()
    out_file_shortcuts = tmp_path / "eval_results_shortcuts.json"
    os.environ["COMMAND_SHORTCUTS"] = "1"
    try:
        results2, _ = run_suite(
            cases=[case],
            provider_factory=mock_factory,
            out_path=out_file_shortcuts,
            extra_meta={"COMMAND_SHORTCUTS": True},
        )
        # Model should NOT be called on a shortcut turn when shortcuts are enabled!
        assert len(call_records) == 0

        data2 = json.loads(out_file_shortcuts.read_text(encoding="utf-8"))
        assert data2["meta"]["COMMAND_SHORTCUTS"] is True
        md2 = render_markdown(data2, [case])
        assert "- **COMMAND_SHORTCUTS**: on" in md2
    finally:
        os.environ.pop("COMMAND_SHORTCUTS", None)


def test_eight_required_cases():
    """Verify the 8 exact cases specified in requirements."""
    apps = dict(ALLOWED_APPS)
    # Ensure chrome is in both apps and sites to test priority
    sites = dict(SITES)
    sites["chrome"] = {"url": "https://www.google.com/chrome", "search": None}

    # 1. open youtube -> open_website (url from SITES)
    spec1 = parse_shortcut("open youtube", apps, sites)
    assert spec1 == ToolCallSpec(
        name="open_website",
        args={"url": SITES["youtube"]["url"]},
        target_name="youtube",
    )

    # 2. open notepad -> open_application (name="notepad")
    spec2 = parse_shortcut("open notepad", apps, sites)
    assert spec2 == ToolCallSpec(
        name="open_application",
        args={"name": "notepad"},
        target_name="notepad",
    )

    # 3. open chrome -> open_application (app priority over site if in both)
    spec3 = parse_shortcut("open chrome", apps, sites)
    assert spec3 == ToolCallSpec(
        name="open_application",
        args={"name": "chrome"},
        target_name="chrome",
    )

    # 4. open workspace folder -> open_workspace_folder (path=".")
    spec4 = parse_shortcut("open workspace folder", apps, sites)
    assert spec4 == ToolCallSpec(
        name="open_workspace_folder",
        args={"path": "."},
        target_name="workspace folder",
    )

    # 5. search youtube for lofi hip hop -> search_site (site="youtube", query="lofi hip hop")
    spec5 = parse_shortcut("search youtube for lofi hip hop", apps, sites)
    assert spec5 == ToolCallSpec(
        name="search_site",
        args={"site": "youtube", "query": "lofi hip hop"},
        target_name="youtube",
    )

    # 6. lofi hip hop on youtube -> search_site (site="youtube", query="lofi hip hop")
    spec6 = parse_shortcut("lofi hip hop on youtube", apps, sites)
    assert spec6 == ToolCallSpec(
        name="search_site",
        args={"site": "youtube", "query": "lofi hip hop"},
        target_name="youtube",
    )

    # 7. open youtube and also open notepad -> None (extra text goes to model)
    spec7 = parse_shortcut("open youtube and also open notepad", apps, sites)
    assert spec7 is None

    # 8. open malicious; rm -rf / -> None (shell metacharacters rejected)
    spec8 = parse_shortcut("open malicious; rm -rf /", apps, sites)
    assert spec8 is None


def test_followup_shortcut_patterns():
    """Verify follow-up shortcut patterns (a, b, c, d) with session state."""
    from core.session_state import SessionState

    apps = {
        "notepad": {"launch": ["notepad.exe"], "process": "notepad.exe"},
        "calc": {"launch": ["calc.exe"], "process": "CalculatorApp.exe"},
        "explorer": {"launch": ["explorer.exe"], "process": None},
    }
    sites = {
        "youtube": {"url": "https://www.youtube.com", "search": "https://www.youtube.com/results?search_query={query}"},
        "wikipedia": {"url": "https://www.wikipedia.org", "search": "https://en.wikipedia.org/wiki/Special:Search?search={query}"},
    }

    state = SessionState()
    state.update_from_tool("search_web", {"query": "free fire videos"}, "success")

    # (a) "in yt" after search gives search_site(youtube, last_query)
    spec = parse_shortcut("in yt", apps, sites, state=state)
    assert spec is not None
    assert spec.name == "search_site"
    assert spec.args == {"site": "youtube", "query": "free fire videos"}

    # Variants: "on youtube", "in you tube", "using wiki"
    spec2 = parse_shortcut("on youtube", apps, sites, state=state)
    assert spec2 is not None
    assert spec2.name == "search_site"
    assert spec2.args == {"site": "youtube", "query": "free fire videos"}

    spec3 = parse_shortcut("in you tube", apps, sites, state=state)
    assert spec3 is not None
    assert spec3.name == "search_site"
    assert spec3.args == {"site": "youtube", "query": "free fire videos"}

    spec4 = parse_shortcut("at yt", apps, sites, state=state)
    assert spec4 is not None
    assert spec4.name == "search_site"
    assert spec4.args == {"site": "youtube", "query": "free fire videos"}

    spec_wiki = parse_shortcut("using wiki", apps, sites, state=state)
    assert spec_wiki is not None
    assert spec_wiki.name == "search_site"
    assert spec_wiki.args == {"site": "wikipedia", "query": "free fire videos"}

    # (b) "(search|look up|find|play) (it|that|this) (in|on) <site>"
    spec_b1 = parse_shortcut("search that on youtube", apps, sites, state=state)
    assert spec_b1 is not None
    assert spec_b1.name == "search_site"
    assert spec_b1.args == {"site": "youtube", "query": "free fire videos"}

    spec_b2 = parse_shortcut("play it in yt", apps, sites, state=state)
    assert spec_b2 is not None
    assert spec_b2.name == "search_site"
    assert spec_b2.args == {"site": "youtube", "query": "free fire videos"}

    spec_b3 = parse_shortcut("look up this on wiki", apps, sites, state=state)
    assert spec_b3 is not None
    assert spec_b3.name == "search_site"
    assert spec_b3.args == {"site": "wikipedia", "query": "free fire videos"}

    # With no previous query -> None
    empty_state = SessionState()
    assert parse_shortcut("in yt", apps, sites, state=empty_state) is None
    assert parse_shortcut("search that on youtube", apps, sites, state=empty_state) is None

    # After expiry -> None
    expired_state = SessionState(ttl=10.0, clock=lambda: 100.0)
    expired_state.update_from_tool("search_web", {"query": "hello"}, "success")
    expired_state.clock = lambda: 120.0
    assert parse_shortcut("in yt", apps, sites, state=expired_state) is None
    assert parse_shortcut("search that on youtube", apps, sites, state=expired_state) is None

    # (c) "close (it|that|this)"
    # Closeable app
    state_app = SessionState()
    state_app.update_from_tool("open_application", {"name": "notepad"}, "success")
    spec_c = parse_shortcut("close it", apps, sites, state=state_app)
    assert spec_c is not None
    assert spec_c.name == "close_application"
    assert spec_c.args == {"name": "notepad"}

    spec_c2 = parse_shortcut("close that", apps, sites, state=state_app)
    assert spec_c2 is not None
    assert spec_c2.name == "close_application"
    assert spec_c2.args == {"name": "notepad"}

    # Never after open_website
    state_site = SessionState()
    state_site.update_from_tool("open_website", {"url": "https://www.youtube.com"}, "success")
    assert parse_shortcut("close it", apps, sites, state=state_site) is None

    # Never for uncloseable app like explorer (process is None)
    state_explorer = SessionState()
    state_explorer.update_from_tool("open_application", {"name": "explorer"}, "success")
    assert parse_shortcut("close it", apps, sites, state=state_explorer) is None

    # (d) "close <app>" for an exact allowed app key
    spec_d = parse_shortcut("close notepad", apps, sites, state=state)
    assert spec_d is not None
    assert spec_d.name == "close_application"
    assert spec_d.args == {"name": "notepad"}

    # Near misses return None
    assert parse_shortcut("in yt and delete everything", apps, sites, state=state) is None
    assert parse_shortcut("in the middle of the night", apps, sites, state=state) is None
    assert parse_shortcut("close it; calc", apps, sites, state=state_app) is None
    assert parse_shortcut("close non_existent_app", apps, sites, state=state) is None


