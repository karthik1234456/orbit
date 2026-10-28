"""Tests for routine execution through the plan machinery in Agent."""

from datetime import datetime
from pathlib import Path
import threading
from typing import Any, Optional


from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.agent import Agent
from core.llm import MockProvider
from routines.store import RoutineStore
from security.audit import AuditLogger
from security.permissions import PermissionLevel, PermissionPolicy
from tools.registry import Tool, ToolRegistry


class ToolRecorder:
    def __init__(self):
        self.calls = []
        self.intercept_fn = None

    def execute_app(self, name: str) -> str:
        self.calls.append(name)
        if self.intercept_fn:
            self.intercept_fn(name)
        return f"Opened {name}"

    def execute_close(self, name: str) -> str:
        self.calls.append(name)
        return f"Closed {name}"

    def execute_web(self, url: str) -> str:
        self.calls.append(url)
        return f"Opened {url}"


class FakeClock:
    def __init__(self, now_iso: str = "2026-10-27T12:00:00Z"):
        self.now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))

    def __call__(self) -> datetime:
        return self.now_dt


def make_test_agent(tmp_path: Path, clock: Optional[Any] = None) -> tuple[Agent, RoutineStore, AuditLogger, dict[str, ToolRecorder]]:
    provider = MockProvider()
    registry = ToolRegistry()

    recorders = {
        "app": ToolRecorder(),
        "close": ToolRecorder(),
        "web": ToolRecorder(),
    }

    registry.register(
        Tool(
            name="open_application",
            description="Open an application",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            func=recorders["app"].execute_app,
            permission=PermissionLevel.CONFIRM,
        )
    )
    registry.register(
        Tool(
            name="close_application",
            description="Close an application",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            func=recorders["close"].execute_close,
            permission=PermissionLevel.CONFIRM,
        )
    )
    registry.register(
        Tool(
            name="open_website",
            description="Open a website",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            func=recorders["web"].execute_web,
            permission=PermissionLevel.CONFIRM,
        )
    )

    db_path = tmp_path / "routines.db"
    store_clock = clock or FakeClock()
    routine_store = RoutineStore(path=str(db_path), clock=store_clock)
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))

    agent = Agent(
        provider=provider,
        registry=registry,
        policy=PermissionPolicy(),
        audit=audit,
        confirm_callback=lambda tool_name, args, warning=None: True,
        plan_callback=lambda labels, permissions, kind="plan", name=None: True,
        allowed_apps=ALLOWED_APPS,
        sites=SITES,
        routine_store=routine_store,
        routines_enabled=True,
    )

    return agent, routine_store, audit, recorders


def test_save_routine_approved_and_denied(tmp_path: Path):
    agent, store, audit, _ = make_test_agent(tmp_path)

    # 1. Save approved
    agent.plan_callback = lambda labels, perms, kind="plan", name=None: True
    reply = agent.chat("create a routine morning: open notepad then open calculator")
    assert "Saved routine 'morning'." in reply
    assert store.get("morning") is not None
    assert agent.history[-1]["content"] == "Saved routine morning."

    records = audit.read_all()
    save_rec = [r for r in records if r.get("decision") == "routine_save" and r.get("outcome") == "success"]
    assert len(save_rec) == 1
    assert save_rec[0]["args"]["name"] == "morning"
    assert save_rec[0]["args"]["step_count"] == 2

    # 2. Save denied
    agent.plan_callback = lambda labels, perms, kind="plan", name=None: False
    reply_denied = agent.chat("create a routine work: open notepad")
    assert reply_denied == "Okay, I didn't save that."
    assert store.get("work") is None
    assert agent.history[-1]["content"] == "Okay, I didn't save that."

    records = audit.read_all()
    denied_rec = [r for r in records if r.get("decision") == "routine_save" and r.get("outcome") == "denied"]
    assert len(denied_rec) == 1


def test_save_routine_no_callback_fails_closed(tmp_path: Path):
    agent, store, _, _ = make_test_agent(tmp_path)
    agent.plan_callback = None
    reply = agent.chat("create a routine morning: open notepad")
    assert reply == "Okay, I didn't save that."
    assert store.get("morning") is None


def test_save_last_plan(tmp_path: Path):
    agent, store, _, tools = make_test_agent(tmp_path)

    # No last plan in session -> refuses
    reply_no_plan = agent.chat("save that as a routine morning")
    assert "There is no recent plan to save as a routine." in reply_no_plan
    assert store.get("morning") is None

    # Run a chain plan
    agent.chat("open notepad then open calculator")
    assert agent.last_plan_steps is not None
    assert len(agent.last_plan_steps) == 2

    # Now save last plan succeeds
    reply_saved = agent.chat("save that as a routine morning")
    assert "Saved routine 'morning'." in reply_saved
    routine = store.get("morning")
    assert routine is not None
    assert len(routine.steps) == 2


def test_run_routine_preview_and_execution(tmp_path: Path):
    clock = FakeClock("2026-10-27T10:00:00Z")
    agent, store, audit, tools = make_test_agent(tmp_path, clock=clock)

    # Save routine directly
    steps = [
        {"tool": "open_application", "args": {"name": "notepad"}},
        {"tool": "open_application", "args": {"name": "calculator"}},
    ]
    store.save(display_name="morning", steps=steps)

    # Preview approved -> runs all steps
    preview_captured = []

    def capturing_plan_cb(labels, perms, kind="plan", name=None):
        preview_captured.append((labels, perms, kind, name))
        return True

    agent.plan_callback = capturing_plan_cb
    reply = agent.chat("run routine morning")

    assert "1." in reply and "2." in reply and "Done" in reply
    assert len(preview_captured) == 1
    labels, perms, kind, name = preview_captured[0]
    assert kind == "routine"
    assert name == "morning"
    assert len(labels) == 2

    # Tools were executed
    assert tools["app"].calls == ["notepad", "calculator"]

    # History redacted to single line
    assert agent.history[-1]["content"] == "Ran routine morning."

    # Audit recorded decision="routine_run"
    records = audit.read_all()
    run_records = [r for r in records if r.get("decision") == "routine_run"]
    assert len(run_records) == 1
    assert run_records[0]["args"]["name"] == "morning"

    # Routine mark_run was called
    r_updated = store.get("morning")
    assert r_updated.run_count == 1
    assert r_updated.last_run_at is not None

    # Model was NEVER called during routine run
    assert len(agent.provider.call_history) == 0


def test_run_routine_preview_denied(tmp_path: Path):
    agent, store, audit, tools = make_test_agent(tmp_path)
    store.save(display_name="morning", steps=[{"tool": "open_application", "args": {"name": "notepad"}}])

    agent.plan_callback = lambda labels, perms, kind="plan", name=None: False
    reply = agent.chat("run routine morning")
    assert "didn't run" in reply.lower() or "denied" in reply.lower() or "cancelled" in reply.lower()
    assert tools["app"].calls == []

    # mark_run not called
    assert store.get("morning").run_count == 0


def test_stale_allowlist_stops_before_step_1(tmp_path: Path):
    agent, store, _, tools = make_test_agent(tmp_path)
    # Stored step contains an app that is no longer in allowed_apps
    store.save(
        display_name="outdated",
        steps=[{"tool": "open_application", "args": {"name": "notepad"}}],
    )

    # Change agent.allowed_apps to empty
    agent.allowed_apps = {}
    reply = agent.chat("run routine outdated")

    assert "no longer in the allowed list" in reply or "not in the allowed list" in reply
    assert tools["app"].calls == []
    assert store.get("outdated").run_count == 0


def test_per_step_denial_stops_plan(tmp_path: Path):
    agent, store, _, tools = make_test_agent(tmp_path)
    store.save(
        display_name="morning",
        steps=[
            {"tool": "open_application", "args": {"name": "notepad"}},
            {"tool": "open_application", "args": {"name": "calculator"}},
        ],
    )

    # Step 1 approved, Step 2 denied
    call_idx = [0]

    def per_step_confirm(tool_name, args, warning=None):
        call_idx[0] += 1
        return call_idx[0] == 1

    agent.confirm_callback = per_step_confirm
    reply = agent.chat("run routine morning")

    assert "stopped" in reply.lower() or "denied" in reply.lower()
    # Step 1 ran, step 2 stopped
    assert tools["app"].calls == ["notepad"]


def test_cancel_event_stops_after_step(tmp_path: Path):
    agent, store, _, tools = make_test_agent(tmp_path)
    store.save(
        display_name="morning",
        steps=[
            {"tool": "open_application", "args": {"name": "notepad"}},
            {"tool": "open_application", "args": {"name": "calculator"}},
        ],
    )

    cancel_evt = threading.Event()

    # Cancel after step 1
    def cancel_on_notepad(name: str):
        if name == "notepad":
            cancel_evt.set()

    tools["app"].intercept_fn = cancel_on_notepad

    reply = agent.chat("run routine morning", cancel_event=cancel_evt)
    assert "stopped" in reply.lower() or "cancelled" in reply.lower()
    assert tools["app"].calls == ["notepad"]


def test_only_one_routine_runs_at_a_time(tmp_path: Path):
    agent, store, _, _ = make_test_agent(tmp_path)
    store.save(display_name="morning", steps=[{"tool": "open_application", "args": {"name": "notepad"}}])

    # Simulate another routine already executing
    agent._routine_running = True
    reply = agent.chat("run routine morning")
    assert "Only one routine can run at a time." in reply
