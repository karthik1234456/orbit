"""Tests for new weather and 'look up' shortcuts, teaching error messages, and sandbox preservation.

Ensures that all tested phrases are scanned against scripts/intent_probe.py and
evals/commands/dev.json to guarantee 0 overlap.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from unittest.mock import MagicMock
import pytest

from core.chains import parse_chain
from core.shortcuts import ToolCallSpec, parse_shortcut
from tools.desktop import make_open_workspace_folder_tool
from tools.files import make_file_tools, resolve_safe


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = PROJECT_ROOT / "scripts" / "intent_probe.py"
DEV_JSON_PATH = PROJECT_ROOT / "evals" / "commands" / "dev.json"

TEACHING_MSG = "ERROR: use a path relative to the workspace, or '.' for the workspace root (for example 'notes')."


def _get_forbidden_phrases() -> set[str]:
    """Extract all phrase strings from intent_probe.py and evals/commands/dev.json."""
    forbidden: set[str] = set()

    # Scan scripts/intent_probe.py without executing it
    probe_content = PROBE_PATH.read_text(encoding="utf-8")
    for match in re.finditer(r'\(\s*"([^"]+)"\s*,\s*\[', probe_content):
        phrase = match.group(1).strip().lower()
        if phrase:
            forbidden.add(phrase)

    # Scan evals/commands/dev.json
    dev_data = json.loads(DEV_JSON_PATH.read_text(encoding="utf-8"))
    for item in dev_data:
        text = str(item.get("text", "")).strip().lower()
        if text:
            forbidden.add(text)

    return forbidden


ALL_TEST_PHRASES: list[str] = [
    # Look up queries
    "look up quantum computing",
    "please look up rust async programming",
    "look up recipe for pasta carbonara",
    # Weather queries
    "what's the weather in mumbai",
    "what is the weather in seattle",
    "weather in london",
    "weather chicago",
    "please what's the weather in boston",
    "weather bangalore please",
    # Chained
    "open notepad and then look up quantum computing",
    "weather chicago and then open calculator",
    # Near-misses
    "i look up to my teacher",
    "look up to him",
    "look up to my mentor",
    "look up",
    "weather",
    "look up and delete my files",
    "the weather is nice today",
    "weather in",
    "look up to the sky",
    "look up to",
]


def test_test_phrases_not_in_intent_probe_or_dev_json():
    """Verify that none of our test phrases appear in intent_probe.py or dev.json."""
    forbidden = _get_forbidden_phrases()

    # Ensure scanner is working by asserting known probe / dev phrases are present in forbidden
    assert "look up the weather in delhi" in forbidden
    assert "open folders" in forbidden
    assert "opn notepad please" in forbidden
    assert "go to youtube" in forbidden

    # Assert 0 overlap with our test phrases
    overlap = {p.strip().lower() for p in ALL_TEST_PHRASES}.intersection(forbidden)
    assert not overlap, f"Forbidden phrases found in test phrases: {overlap}"


# =============================================================================
# 1. New Shortcut Patterns: "look up <query>" and Weather Forms
# =============================================================================


def test_look_up_shortcut():
    """Verify 'look up <query>' routes to search_web with exact query."""
    res1 = parse_shortcut("look up quantum computing")
    assert isinstance(res1, ToolCallSpec)
    assert res1.name == "search_web"
    assert res1.args == {"query": "quantum computing"}

    res2 = parse_shortcut("please look up rust async programming")
    assert isinstance(res2, ToolCallSpec)
    assert res2.name == "search_web"
    assert res2.args == {"query": "rust async programming"}

    res3 = parse_shortcut("look up recipe for pasta carbonara")
    assert isinstance(res3, ToolCallSpec)
    assert res3.name == "search_web"
    assert res3.args == {"query": "recipe for pasta carbonara"}


def test_weather_shortcuts():
    """Verify weather forms produce search_web with query='weather in <place>'."""
    # what's the weather in <place>
    res1 = parse_shortcut("what's the weather in mumbai")
    assert isinstance(res1, ToolCallSpec)
    assert res1.name == "search_web"
    assert res1.args == {"query": "weather in mumbai"}

    # what is the weather in <place>
    res2 = parse_shortcut("what is the weather in seattle")
    assert isinstance(res2, ToolCallSpec)
    assert res2.name == "search_web"
    assert res2.args == {"query": "weather in seattle"}

    # weather in <place>
    res3 = parse_shortcut("weather in london")
    assert isinstance(res3, ToolCallSpec)
    assert res3.name == "search_web"
    assert res3.args == {"query": "weather in london"}

    # weather <place>
    res4 = parse_shortcut("weather chicago")
    assert isinstance(res4, ToolCallSpec)
    assert res4.name == "search_web"
    assert res4.args == {"query": "weather in chicago"}

    # with leading / trailing fillers
    res5 = parse_shortcut("please what's the weather in boston")
    assert isinstance(res5, ToolCallSpec)
    assert res5.name == "search_web"
    assert res5.args == {"query": "weather in boston"}

    res6 = parse_shortcut("weather bangalore please")
    assert isinstance(res6, ToolCallSpec)
    assert res6.name == "search_web"
    assert res6.args == {"query": "weather in bangalore"}


def test_chained_with_new_patterns():
    """Verify new patterns work cleanly in command chains."""
    plan1 = parse_chain("open notepad and then look up quantum computing")
    assert plan1 is not None
    assert len(plan1.steps) == 2
    assert plan1.steps[0].tool == "open_application"
    assert plan1.steps[0].args == {"name": "notepad"}
    assert plan1.steps[1].tool == "search_web"
    assert plan1.steps[1].args == {"query": "quantum computing"}

    plan2 = parse_chain("weather chicago and then open calculator")
    assert plan2 is not None
    assert len(plan2.steps) == 2
    assert plan2.steps[0].tool == "search_web"
    assert plan2.steps[0].args == {"query": "weather in chicago"}
    assert plan2.steps[1].tool == "open_application"
    assert plan2.steps[1].args == {"name": "calculator"}


# =============================================================================
# 2. Near-Misses: Strictly Return None
# =============================================================================


@pytest.mark.parametrize(
    "phrase",
    [
        "i look up to my teacher",
        "look up to him",
        "look up to my mentor",
        "look up",
        "weather",
        "look up and delete my files",
        "the weather is nice today",
        "weather in",
        "look up to the sky",
        "look up to",
    ],
)
def test_near_misses_stay_none(phrase: str):
    """Verify near-miss phrases and idioms remain unparsed (return None)."""
    assert parse_shortcut(phrase) is None


# =============================================================================
# 3. Teaching Error Messages & Path Prechecks
# =============================================================================


def test_open_workspace_folder_teaching_errors(tmp_path: Path):
    """Verify open_workspace_folder precheck returns the teaching message on absolute paths."""
    tool = make_open_workspace_folder_tool(tmp_path)
    assert tool.precheck is not None

    # Absolute Windows drive path
    err_win = tool.precheck({"path": "C:\\Windows"})
    assert err_win == TEACHING_MSG

    # Absolute forward slash path
    err_slash = tool.precheck({"path": "/etc/passwd"})
    assert err_slash == TEACHING_MSG

    # Direct execution call also returns teaching error without executing
    mock_popen = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("subprocess.Popen", mock_popen)
        res = tool.func(path="C:/Windows")
        assert res == TEACHING_MSG
        mock_popen.assert_not_called()


def test_file_tools_precheck_teaching_errors(tmp_path: Path):
    """Verify file prechecks return the teaching message when given absolute paths."""
    tool_map = {t.name: t for t in make_file_tools(tmp_path)}

    # list_files
    err_list = tool_map["list_files"].precheck({"path": "C:\\Users"})
    assert err_list == TEACHING_MSG
    err_list_slash = tool_map["list_files"].precheck({"path": "/var/log"})
    assert err_list_slash == TEACHING_MSG

    # read_file
    err_read = tool_map["read_file"].precheck({"path": "C:\\notes.txt"})
    assert err_read == TEACHING_MSG

    # create_folder
    err_fld = tool_map["create_folder"].precheck({"path": "C:\\newdir"})
    assert err_fld == TEACHING_MSG

    # create_file
    err_file = tool_map["create_file"].precheck({"path": "C:\\newfile.txt", "content": "hi"})
    assert err_file == TEACHING_MSG

    # move_file (source and destination)
    err_mv_src = tool_map["move_file"].precheck({"source": "C:\\src.txt", "destination": "dest.txt"})
    assert err_mv_src == TEACHING_MSG
    err_mv_dst = tool_map["move_file"].precheck({"source": "src.txt", "destination": "C:\\dst.txt"})
    assert err_mv_dst == TEACHING_MSG


def test_missing_or_empty_path_defaults_to_root(tmp_path: Path):
    """Verify missing or empty path defaults to '.' for open_workspace_folder and list_files."""
    # open_workspace_folder
    ws_tool = make_open_workspace_folder_tool(tmp_path)
    assert ws_tool.precheck({}) is None
    assert ws_tool.precheck({"path": ""}) is None
    assert ws_tool.precheck({"path": "   "}) is None
    assert ws_tool.precheck({"path": None}) is None

    mock_popen = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("subprocess.Popen", mock_popen)
        res = ws_tool.func("")
        assert res == "Opened folder '.' in Windows Explorer."
        assert mock_popen.call_count == 1

    # list_files
    file_tools = {t.name: t for t in make_file_tools(tmp_path)}
    assert file_tools["list_files"].precheck({}) is None
    assert file_tools["list_files"].precheck({"path": ""}) is None
    assert file_tools["list_files"].precheck({"path": "  "}) is None
    assert file_tools["list_files"].precheck({"path": None}) is None

    # Should list root empty directory
    assert file_tools["list_files"].func("") == "(empty directory)"


def test_tool_descriptions_teaching_and_search_web():
    """Verify tool descriptions contain required teaching and usage sentences."""
    tmp_path = Path("d:/projects/orbit")
    ws_tool = make_open_workspace_folder_tool(tmp_path)
    file_tools = {t.name: t for t in make_file_tools(tmp_path)}

    # open_workspace_folder description
    assert "If the user does not name a folder, pass '.'." in ws_tool.description

    # list_files description
    assert "If the user does not name a folder, pass '.'." in file_tools["list_files"].description

    # search_web description in browser_tools
    from tools.browser_tools import make_browser_tools as make_controller_tools
    from tools.browser import make_browser_tools as make_classic_tools
    mock_controller = MagicMock()
    bt_tools = {t.name: t for t in make_controller_tools(mock_controller)}
    classic_tools = {t.name: t for t in make_classic_tools()}

    for sw_tool in (bt_tools["search_web"], classic_tools["search_web"]):
        assert "weather, news, prices, scores, schedules and any current information" in sw_tool.description


# =============================================================================
# 4. Sandbox Integrity: Absolute Paths & Traversals Strictly Refused
# =============================================================================


def test_sandbox_rules_not_relaxed(tmp_path: Path):
    """Verify resolve_safe still strictly refuses absolute paths and traversal attacks."""
    with pytest.raises(ValueError) as exc1:
        resolve_safe(tmp_path, "C:\\Windows\\system32")
    assert "drive letters" in str(exc1.value).lower()

    with pytest.raises(ValueError) as exc2:
        resolve_safe(tmp_path, "/etc/shadow")
    assert "absolute" in str(exc2.value).lower()

    with pytest.raises(ValueError) as exc3:
        resolve_safe(tmp_path, "../outside.txt")
    assert "traversal" in str(exc3.value).lower()
