"""Tests for the planner module and open_in_vscode tool integration."""

import json
from pathlib import Path
from unittest.mock import patch
import pytest

from core.agent import Agent
from core.llm import LLMProvider, LLMResponse
from core.planner import Plan, PlanError, format_plan, make_plan
from security.permissions import PermissionLevel
from tools.registry import Tool
from tools.vscode import make_vscode_tool


class FakeProvider(LLMProvider):
    """Scripted LLM provider for testing planner responses."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(self, messages: list[dict], tools=None) -> LLMResponse:
        self.call_history.append(list(messages))
        if not self.responses:
            return LLMResponse(text="Default fallback response")
        return self.responses.pop(0)


# ===========================================================================
# Planner tests
# ===========================================================================


def test_make_plan_valid_json():
    """Verify make_plan successfully parses clean JSON without tools."""
    provider = FakeProvider([
        LLMResponse(text='{"steps": ["1. Create directory project", "2. Create main.py in project"]}')
    ])
    sample_tools = [
        Tool(
            name="create_folder",
            description="Create folder",
            parameters={},
            func=lambda path: "ok",
            permission=PermissionLevel.CONFIRM,
        ),
        Tool(
            name="create_file",
            description="Create file",
            parameters={},
            func=lambda path, content: "ok",
            permission=PermissionLevel.CONFIRM,
        ),
    ]

    plan = make_plan(provider, "Set up project", sample_tools)
    assert plan.goal == "Set up project"
    assert len(plan.steps) == 2
    assert "Create directory project" in plan.steps[0]
    assert "Create main.py" in plan.steps[1]

    # Verify provider was called exactly once WITHOUT tools
    assert len(provider.call_history) == 1
    assert "create_folder" in provider.call_history[0][0]["content"]
    assert "create_file" in provider.call_history[0][0]["content"]


def test_make_plan_json_inside_code_fences():
    """Verify make_plan robustly strips markdown code fences."""
    raw = '```json\n{\n  "steps": [\n    "Open calculator",\n    "Multiply 42 by 7"\n  ]\n}\n```'
    provider = FakeProvider([LLMResponse(text=raw)])

    plan = make_plan(provider, "Calculate math")
    assert len(plan.steps) == 2
    assert plan.steps[0] == "Open calculator"
    assert plan.steps[1] == "Multiply 42 by 7"


def test_make_plan_garbage_raises_plan_error():
    """Verify non-JSON responses or garbage text raise PlanError."""
    # Complete garbage text
    provider1 = FakeProvider([LLMResponse(text="I cannot create a plan for this request.")])
    with pytest.raises(PlanError) as exc1:
        make_plan(provider1, "Do impossible thing")
    assert "no json object found" in str(exc1.value).lower()

    # Malformed JSON syntax
    provider2 = FakeProvider([LLMResponse(text='{"steps": ["incomplete step" }')])
    with pytest.raises(PlanError) as exc2:
        make_plan(provider2, "Broken json")
    assert "failed to parse json" in str(exc2.value).lower()

    # Empty response
    provider3 = FakeProvider([LLMResponse(text="")])
    with pytest.raises(PlanError) as exc3:
        make_plan(provider3, "Empty")
    assert "empty response" in str(exc3.value).lower()


def test_make_plan_more_than_8_steps_capped():
    """Verify plans with more than 8 steps are capped at 8."""
    ten_steps = [f"Step {i}" for i in range(1, 11)]
    provider = FakeProvider([LLMResponse(text=json.dumps({"steps": ten_steps}))])

    plan = make_plan(provider, "Long task")
    assert len(plan.steps) == 8
    assert plan.steps == [f"Step {i}" for i in range(1, 9)]


def test_make_plan_character_capping():
    """Verify steps exceeding 200 characters are capped at 200 characters."""
    long_step = "x" * 250
    provider = FakeProvider([LLMResponse(text=f'{{"steps": ["{long_step}"]}}')])

    plan = make_plan(provider, "Task with long step")
    assert len(plan.steps) == 1
    assert len(plan.steps[0]) == 200


def test_make_plan_non_string_items_rejected():
    """Verify non-string or empty items in steps raise PlanError."""
    # List containing integers
    provider1 = FakeProvider([LLMResponse(text='{"steps": [123, "valid step"]}')])
    with pytest.raises(PlanError) as exc1:
        make_plan(provider1, "Invalid item type")
    assert "must be a string" in str(exc1.value).lower()

    # List containing empty string
    provider2 = FakeProvider([LLMResponse(text='{"steps": ["   ", "valid step"]}')])
    with pytest.raises(PlanError) as exc2:
        make_plan(provider2, "Empty step string")
    assert "cannot be empty" in str(exc2.value).lower()

    # Empty list
    provider3 = FakeProvider([LLMResponse(text='{"steps": []}')])
    with pytest.raises(PlanError) as exc3:
        make_plan(provider3, "Empty steps list")
    assert "cannot be empty" in str(exc3.value).lower()


def test_format_plan():
    """Verify format_plan produces clear human-readable numbered lines."""
    plan = Plan(goal="Build app", steps=["1. Clone repo", "2. Run tests", "Deploy to prod"])
    formatted = format_plan(plan)
    assert "Plan: Build app" in formatted
    assert "1. Clone repo" in formatted
    assert "2. Run tests" in formatted
    assert "3. Deploy to prod" in formatted


def test_agent_run_plan():
    """Verify Agent.run_plan invokes chat() with numbered steps and instructions."""
    provider = FakeProvider([LLMResponse(text="Plan completed.")])
    agent = Agent(provider=provider)

    plan = Plan(goal="Write report", steps=["Gather data", "Draft document"])
    reply = agent.run_plan(plan)

    assert reply == "Plan completed."
    assert len(provider.call_history) == 1
    user_msg = provider.call_history[0][0]["content"]
    assert "Goal: Write report" in user_msg
    assert "1. Gather data" in user_msg
    assert "2. Draft document" in user_msg
    assert "Follow this plan step by step using tools. If a step fails, stop and report." in user_msg


# ===========================================================================
# open_in_vscode tool tests
# ===========================================================================


def test_open_in_vscode_path_outside_workspace_refused(tmp_path: Path):
    """Verify open_in_vscode refuses paths outside the workspace boundary."""
    tool = make_vscode_tool(tmp_path)
    res = tool.func(path="../outside.txt")
    assert res.startswith("ERROR:")
    assert "escapes workspace" in res.lower() or "traversal" in res.lower() or "invalid path" in res.lower()

    res_abs = tool.func(path="C:/Windows/win.ini")
    assert res_abs.startswith("ERROR:")


def test_open_in_vscode_missing_path_refused(tmp_path: Path):
    """Verify open_in_vscode refuses non-existent files or directories."""
    tool = make_vscode_tool(tmp_path)
    res = tool.func(path="nonexistent.py")
    assert res.startswith("ERROR:")
    assert "does not exist" in res.lower()


def test_open_in_vscode_popen_called_with_list(tmp_path: Path):
    """Verify open_in_vscode launches VS Code using subprocess.Popen with an argument list."""
    target_file = tmp_path / "script.py"
    target_file.write_text("print('hello')", encoding="utf-8")

    tool = make_vscode_tool(tmp_path)
    mock_code_path = "C:/Program Files/Microsoft VS Code/bin/code.cmd"

    with patch("shutil.which", return_value=mock_code_path) as mock_which, \
         patch("subprocess.Popen") as mock_popen:
        res = tool.func(path="script.py")

        assert "opened 'script.py' in vs code" in res.lower()
        mock_which.assert_called_once_with("code")
        mock_popen.assert_called_once()

        # Verify called with a list of args (never shell=True)
        call_args, call_kwargs = mock_popen.call_args
        cmd_list = call_args[0]
        assert isinstance(cmd_list, list), f"Expected list of arguments, got {type(cmd_list)}"
        assert cmd_list[0] == mock_code_path
        assert cmd_list[1] == str(target_file.resolve())
        assert call_kwargs.get("shell") is not True


def test_open_in_vscode_missing_executable_returns_friendly_error(tmp_path: Path):
    """Verify open_in_vscode returns a friendly error if VS Code is not on PATH."""
    target_file = tmp_path / "notes.txt"
    target_file.write_text("notes", encoding="utf-8")

    tool = make_vscode_tool(tmp_path)

    with patch("shutil.which", return_value=None):
        res = tool.func(path="notes.txt")
        assert res.startswith("ERROR:")
        assert "not found" in res.lower()
