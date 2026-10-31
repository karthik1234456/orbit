"""Tests for desktop tool registration, integrity checks, and tool metadata validation."""

from pathlib import Path
from unittest.mock import MagicMock

from config.apps import ALLOWED_APPS
from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.version import VERSION
from main import STARTUP_MESSAGE, SYSTEM_PROMPT
from security.audit import AuditLogger
from security.permissions import PermissionLevel, PermissionPolicy
from tools.builtin import register_builtin_tools
from tools.desktop import make_open_workspace_folder_tool, register_desktop_tools
from tools.registry import ToolRegistry


class FakeProvider(LLMProvider):
    """Simple test provider returning pre-canned responses sequentially."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_count = 0

    def generate(self, messages, tools=None, on_delta=None, should_stop=None) -> LLMResponse:
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp


def test_register_all_tools_into_fresh_registry(tmp_path: Path):
    """Verify that registering all tools creates the workspace, has no duplicates, non-empty descriptions, and valid permissions."""
    registry = ToolRegistry()
    workspace_folder = tmp_path / "custom_workspace"

    assert not workspace_folder.exists()

    # Register built-in tools followed by desktop tools
    register_builtin_tools(registry)
    register_desktop_tools(registry, workspace_folder, ALLOWED_APPS)

    # Verify workspace directory is automatically created
    assert workspace_folder.is_dir()

    tools = registry.list_tools()
    tool_names = [t.name for t in tools]

    # 1. No duplicate names
    assert len(tool_names) == len(set(tool_names)), f"Duplicate tool names detected: {tool_names}"

    core_tools = {
        "get_current_time",
        "calculate",
        "system_info",
        "write_note",
        "list_files",
        "read_file",
        "create_file",
        "create_folder",
        "move_file",
        "open_application",
        "close_application",
        "list_allowed_apps",
        "open_website",
        "search_web",
        "search_site",
        "open_in_vscode",
        "open_workspace_folder",
    }
    browser_tools = {
        "browser_click_result",
        "browser_media",
        "browser_scroll",
        "browser_history",
        "browser_read_page",
        "browser_close",
    }
    from config import settings
    if settings.browser_enabled():
        assert set(tool_names) == core_tools | browser_tools
    else:
        assert set(tool_names) == core_tools

    # Verify open_workspace_folder is present with correct attributes
    assert "open_workspace_folder" in tool_names
    folder_tool = registry.get("open_workspace_folder")
    assert folder_tool is not None
    assert folder_tool.permission == PermissionLevel.CONFIRM
    assert folder_tool.precheck is not None
    assert "Opens a folder inside ORBIT's workspace in Windows Explorer" in folder_tool.description

    for tool in tools:
        # 2. Every tool has a non-empty description
        assert tool.description, f"Tool '{tool.name}' has empty description"
        assert len(tool.description.strip()) > 0, f"Tool '{tool.name}' description is whitespace"

        # 3. Every tool's permission is an explicit PermissionLevel (not None)
        assert tool.permission is not None, f"Tool '{tool.name}' has None permission"
        assert isinstance(
            tool.permission, PermissionLevel
        ), f"Tool '{tool.name}' has invalid permission type: {type(tool.permission)}"


def test_main_system_prompt_instructions():
    """Verify that the system prompt in main.py contains mandatory behavioral instructions."""
    prompt_lower = SYSTEM_PROMPT.lower()
    assert "workspace" in prompt_lower
    assert "apps" in prompt_lower
    assert "websites" in prompt_lower
    assert "not claim" in prompt_lower
    assert "error" in prompt_lower
    assert "honestly" in prompt_lower


def test_startup_message_contains_version():
    """Verify that STARTUP_MESSAGE in main.py contains the single VERSION constant."""
    assert VERSION == "2.25.0"
    assert VERSION in STARTUP_MESSAGE
    assert f"ORBIT v{VERSION}" in STARTUP_MESSAGE


def test_no_stale_version_strings_in_non_test_source():
    """Verify that neither '1.2.1' nor 'v1.2' remains in any non-test source files."""
    repo_root = Path(__file__).resolve().parent.parent
    stale_patterns = ["1.2.1", "v1.2"]
    offending: list[str] = []

    for path in repo_root.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or ".venv" in parts or "venv" in parts or ".git" in parts or "__pycache__" in parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for p in stale_patterns:
            if p in content:
                offending.append(f"{path.relative_to(repo_root)} contains '{p}'")

    assert not offending, f"Stale version strings found in source: {offending}"


def test_open_workspace_folder_success(tmp_path: Path, monkeypatch):
    """Verify open_workspace_folder calls subprocess.Popen with a list and shell=False."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subfolder = workspace / "documents"
    subfolder.mkdir()

    tool = make_open_workspace_folder_tool(workspace)

    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    # 1. Opening a subfolder
    res = tool.func(path="documents")
    assert "Opened folder 'documents' in Windows Explorer." in res
    assert mock_popen.call_count == 1
    call_args, call_kwargs = mock_popen.call_args
    assert call_args[0] == ["explorer.exe", str(subfolder.resolve())]
    assert call_kwargs.get("shell") is False

    # 2. Opening default "." (workspace root)
    mock_popen.reset_mock()
    res_root = tool.func()
    assert "Opened folder '.' in Windows Explorer." in res_root
    assert mock_popen.call_count == 1
    call_args, call_kwargs = mock_popen.call_args
    assert call_args[0] == ["explorer.exe", str(workspace.resolve())]
    assert call_kwargs.get("shell") is False


def test_open_workspace_folder_path_outside_refused(tmp_path: Path, monkeypatch):
    """Verify path outside workspace is refused and Popen is never called."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    tool = make_open_workspace_folder_tool(workspace)

    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    res = tool.func(path="../outside")
    assert res.startswith("ERROR:")
    mock_popen.assert_not_called()


def test_open_workspace_folder_file_refused(tmp_path: Path, monkeypatch):
    """Verify file path is refused and Popen is never called."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample_file = workspace / "notes.txt"
    sample_file.write_text("hello", encoding="utf-8")

    tool = make_open_workspace_folder_tool(workspace)

    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    res = tool.func(path="notes.txt")
    assert res.startswith("ERROR:")
    assert "file, not a directory" in res
    mock_popen.assert_not_called()


def test_open_workspace_folder_missing_folder_refused(tmp_path: Path, monkeypatch):
    """Verify missing folder is refused and Popen is never called."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tool = make_open_workspace_folder_tool(workspace)

    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    res = tool.func(path="nonexistent_folder")
    assert res.startswith("ERROR:")
    assert "does not exist in workspace" in res
    mock_popen.assert_not_called()


def test_open_workspace_folder_precheck(tmp_path: Path):
    """Verify pure precheck logic without calling subprocess.Popen."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "valid_dir").mkdir()
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    tool = make_open_workspace_folder_tool(workspace)
    assert tool.precheck is not None

    # Valid folder and default return None (no error)
    assert tool.precheck({"path": "valid_dir"}) is None
    assert tool.precheck({"path": "."}) is None
    assert tool.precheck({}) is None

    # Missing folder returns error
    err_missing = tool.precheck({"path": "nonexistent"})
    assert err_missing is not None
    assert "does not exist" in err_missing

    # File path returns error
    err_file = tool.precheck({"path": "notes.txt"})
    assert err_file is not None
    assert "file, not a directory" in err_file

    # Escaping path returns error
    err_escape = tool.precheck({"path": "../escape"})
    assert err_escape is not None
    assert "ERROR:" in err_escape


def test_open_workspace_folder_precheck_prevents_confirmation(tmp_path: Path):
    """Verify precheck failure on invalid folder path prevents confirmation callback and logs precheck_failed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample_file = workspace / "notes.txt"
    sample_file.write_text("hello", encoding="utf-8")

    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))
    policy = PermissionPolicy()
    reg = ToolRegistry()
    reg.register(make_open_workspace_folder_tool(workspace))

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="open_workspace_folder", args={"path": "notes.txt"})]),
        LLMResponse(text="Cannot open notes.txt as it is a file."),
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

    reply = agent.chat("Open notes.txt in Explorer")
    assert reply == "Cannot open notes.txt as it is a file."
    assert confirm_called is False, "confirm_callback must NOT be called when precheck fails"

    assert len(agent.last_tool_events) == 1
    tool_name, tool_args, tool_result = agent.last_tool_events[0]
    assert tool_name == "open_workspace_folder"
    assert tool_result.startswith("ERROR:")
    assert "file, not a directory" in tool_result

    records = audit.read_all()
    assert len(records) == 1
    assert records[0]["tool"] == "open_workspace_folder"
    assert records[0]["decision"] == "precheck_failed"
    assert records[0]["outcome"] == "blocked"
