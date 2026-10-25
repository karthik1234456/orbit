"""Tests for application launching and process management tools."""

from unittest.mock import MagicMock, patch
import pytest

from config.apps import ALLOWED_APPS
from security.permissions import PermissionLevel
from tools.apps import make_app_tools
from tools.registry import ToolRegistry


@pytest.fixture
def app_tools():
    """Fixture providing tools configured with ALLOWED_APPS."""
    tools = make_app_tools(ALLOWED_APPS)
    return {t.name: t for t in tools}


def test_tool_metadata_and_permissions(app_tools):
    """Verify tool permissions (CONFIRM for open/close, SAFE for list) and schema."""
    assert app_tools["open_application"].permission == PermissionLevel.CONFIRM
    assert app_tools["close_application"].permission == PermissionLevel.CONFIRM
    assert app_tools["list_allowed_apps"].permission == PermissionLevel.SAFE


# ---------------------------------------------------------------------------
# Allowlist enforcement & injection defense
# ---------------------------------------------------------------------------


def test_unknown_app_is_refused(app_tools):
    """Verify unknown apps are rejected by open_application and close_application."""
    with patch("subprocess.Popen") as mock_popen, patch("psutil.process_iter") as mock_iter:
        open_res = app_tools["open_application"].func("unknown_app")
        assert open_res.startswith("ERROR:")
        assert "not in the allowed list" in open_res.lower()
        # Verify allowed names are listed in the error
        assert "notepad" in open_res.lower()
        mock_popen.assert_not_called()

        close_res = app_tools["close_application"].func("unknown_app")
        assert close_res.startswith("ERROR:")
        assert "not in the allowed list" in close_res.lower()
        mock_iter.assert_not_called()


@pytest.mark.parametrize(
    "attack_name",
    [
        "notepad; calc",
        "..\\evil",
        "cmd.exe",
        "powershell",
        "notepad && calc",
        "/bin/sh",
        "notepad.exe",  # Not a registered alias key
        "calc.exe",
        "../../system32/cmd",
    ],
)
def test_attack_strings_and_arbitrary_commands_refused(app_tools, attack_name: str):
    """Verify that command injections, paths, and unapproved aliases are strictly rejected."""
    with patch("subprocess.Popen") as mock_popen:
        res = app_tools["open_application"].func(attack_name)
        assert res.startswith("ERROR:")
        assert "not in the allowed list" in res.lower()
        mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Case-insensitivity & execution parameters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_name,expected_alias",
    [
        ("notepad", "notepad"),
        ("NOTEPAD", "notepad"),
        ("  Notepad  ", "notepad"),
        ("CaLcUlAtOr", "calculator"),
        ("  calculator\t", "calculator"),
    ],
)
def test_case_insensitive_name_matching(app_tools, input_name: str, expected_alias: str):
    """Verify that app aliases match case-insensitively and ignore surrounding whitespace."""
    with patch("shutil.which", return_value="C:\\fake\\path.exe"), patch("subprocess.Popen") as mock_popen:
        res = app_tools["open_application"].func(input_name)
        assert res == f"Successfully opened {expected_alias}."

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        # Popen must be called with a list of args
        cmd = args[0]
        assert isinstance(cmd, list)
        assert cmd[0] == "C:\\fake\\path.exe"
        # Must never use shell=True
        assert kwargs.get("shell") is not True


def test_popen_never_uses_shell_and_passes_list(app_tools):
    """Verify that open_application uses a command list and never executes via shell."""
    with patch("shutil.which", return_value="C:\\Windows\\notepad.exe"), patch("subprocess.Popen") as mock_popen:
        res = app_tools["open_application"].func("notepad")
        assert "successfully opened notepad" in res.lower()

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert isinstance(args[0], list)
        assert args[0] == ["C:\\Windows\\notepad.exe"]
        assert kwargs.get("shell") is False or "shell" not in kwargs or kwargs.get("shell") is not True


def test_open_application_handles_missing_executable_and_oserror(app_tools):
    """Verify open_application catches FileNotFoundError and OSError gracefully."""
    with patch("shutil.which", return_value=None):
        res = app_tools["open_application"].func("notepad")
        assert res.startswith("ERROR:")
        assert "not be found" in res.lower()

    with patch("shutil.which", return_value="C:\\fake\\notepad.exe"), patch(
        "subprocess.Popen", side_effect=OSError("Access denied")
    ):
        res2 = app_tools["open_application"].func("notepad")
        assert res2.startswith("ERROR:")
        assert "access denied" in res2.lower()


# ---------------------------------------------------------------------------
# close_application
# ---------------------------------------------------------------------------


def test_close_application_terminates_gracefully(app_tools):
    """Verify close_application terminates matching processes gracefully without kill()."""
    proc_matching = MagicMock()
    proc_matching.name.return_value = "notepad.exe"

    proc_other = MagicMock()
    proc_other.name.return_value = "chrome.exe"

    with patch("psutil.process_iter", return_value=[proc_matching, proc_other]):
        res = app_tools["close_application"].func("Notepad")
        assert res == "Closed 1 running instance(s) of notepad."

        # Terminate must be called, never kill
        proc_matching.terminate.assert_called_once()
        proc_matching.kill.assert_not_called()
        proc_other.terminate.assert_not_called()


def test_close_application_none_running(app_tools):
    """Verify close_application handles cases where no matching processes are active."""
    proc_other = MagicMock()
    proc_other.name.return_value = "explorer.exe"

    with patch("psutil.process_iter", return_value=[proc_other]):
        res = app_tools["close_application"].func("calculator")
        assert res == "No running instances of calculator were found."
        proc_other.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# list_allowed_apps
# ---------------------------------------------------------------------------


def test_list_allowed_apps(app_tools):
    """Verify list_allowed_apps outputs all configured application names."""
    res = app_tools["list_allowed_apps"].func()
    for name in ALLOWED_APPS.keys():
        assert name in res


# ---------------------------------------------------------------------------
# Schema integrity and regression tests
# ---------------------------------------------------------------------------


def test_allowed_apps_structure():
    """Verify every ALLOWED_APPS entry has a non-empty launch list of strings and a process that is str or None."""
    assert len(ALLOWED_APPS) > 0
    for name, entry in ALLOWED_APPS.items():
        assert isinstance(name, str) and name.strip() != ""
        assert isinstance(entry, dict), f"Entry '{name}' must be a dict"
        assert "launch" in entry, f"Entry '{name}' missing 'launch'"
        assert isinstance(entry["launch"], list), f"Entry '{name}' launch must be a list"
        assert len(entry["launch"]) > 0, f"Entry '{name}' launch list is empty"
        assert all(isinstance(arg, str) and arg.strip() != "" for arg in entry["launch"]), (
            f"Entry '{name}' launch list elements must be non-empty strings"
        )
        assert "process" in entry, f"Entry '{name}' missing 'process'"
        process = entry["process"]
        assert process is None or (isinstance(process, str) and process.strip() != ""), (
            f"Entry '{name}' process must be None or a non-empty string"
        )


def test_regression_no_entry_has_process_explorer_exe():
    """Regression test ensuring explorer.exe is never set as a target process to prevent killing taskbar."""
    for name, entry in ALLOWED_APPS.items():
        process = entry.get("process")
        if process is not None:
            assert process.strip().lower() != "explorer.exe", (
                f"Entry '{name}' must not have process 'explorer.exe' as terminating it kills the taskbar/desktop"
            )


def test_close_refused_when_process_is_none(app_tools):
    """Verify close_application refuses apps configured with process=None (e.g. explorer, whatsapp)."""
    with patch("psutil.process_iter") as mock_iter:
        res_explorer = app_tools["close_application"].func("explorer")
        assert res_explorer == "ORBIT is not allowed to close explorer."
        mock_iter.assert_not_called()

        res_whatsapp = app_tools["close_application"].func("whatsapp")
        assert res_whatsapp == "ORBIT is not allowed to close whatsapp."
        mock_iter.assert_not_called()


def test_close_application_targets_only_configured_process(app_tools):
    """Verify closing 'notepad' targets only notepad.exe and not other processes (mock psutil)."""
    proc_notepad = MagicMock()
    proc_notepad.name.return_value = "notepad.exe"

    proc_notepad_caps = MagicMock()
    proc_notepad_caps.name.return_value = "NOTEPAD.EXE"

    proc_calc = MagicMock()
    proc_calc.name.return_value = "CalculatorApp.exe"

    proc_explorer = MagicMock()
    proc_explorer.name.return_value = "explorer.exe"

    with patch("psutil.process_iter", return_value=[proc_notepad, proc_notepad_caps, proc_calc, proc_explorer]):
        res = app_tools["close_application"].func("notepad")
        assert res == "Closed 2 running instance(s) of notepad."

        proc_notepad.terminate.assert_called_once()
        proc_notepad_caps.terminate.assert_called_once()
        proc_calc.terminate.assert_not_called()
        proc_explorer.terminate.assert_not_called()


def test_whatsapp_launch_calls_popen_with_resolved_list_without_shell(app_tools):
    """Verify whatsapp launch calls Popen with ['explorer.exe', 'whatsapp:'] resolved as list without shell=True."""
    def fake_which(cmd):
        if cmd == "explorer.exe":
            return "C:\\Windows\\explorer.exe"
        return None

    with patch("shutil.which", side_effect=fake_which), patch("subprocess.Popen") as mock_popen:
        res = app_tools["open_application"].func("whatsapp")
        assert res == "Successfully opened whatsapp."

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert args[0] == ["C:\\Windows\\explorer.exe", "whatsapp:"]
        assert kwargs.get("shell") is not True
        assert kwargs.get("shell") is False or "shell" not in kwargs


# ---------------------------------------------------------------------------
# ToolRegistry integration
# ---------------------------------------------------------------------------


def test_app_tools_registry_integration(app_tools):
    """Verify app tools can be registered and safely executed via ToolRegistry."""
    registry = ToolRegistry()
    for t in app_tools.values():
        registry.register(t)

    with patch("shutil.which", return_value="C:\\fake\\calc.exe"), patch("subprocess.Popen") as mock_popen:
        exec_res = registry.execute("open_application", {"name": "calculator"})
        assert "successfully opened calculator" in exec_res.lower()
        mock_popen.assert_called_once()

    # Rejected injection via registry
    injection_res = registry.execute("open_application", {"name": "notepad; calc"})
    assert injection_res.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Precheck pure validation tests
# ---------------------------------------------------------------------------


def test_open_application_precheck(app_tools):
    """Verify open_application precheck returns None for allowed apps, error for typos and attacks."""
    open_tool = app_tools["open_application"]
    assert open_tool.precheck is not None

    # Valid apps return None
    assert open_tool.precheck({"name": "notepad"}) is None
    assert open_tool.precheck({"name": "calculator"}) is None
    assert open_tool.precheck({"name": "Notepad"}) is None

    # Typo or unallowed app returns error
    err_typo = open_tool.precheck({"name": "whatsappp"})
    assert err_typo is not None and "not in the allowed list" in err_typo

    # Empty or invalid types return error
    err_none = open_tool.precheck({})
    assert err_none is not None and "valid string" in err_none


def test_close_application_precheck(app_tools):
    """Verify close_application precheck returns None for closeable apps, error for uncloseable or unknown."""
    close_tool = app_tools["close_application"]
    assert close_tool.precheck is not None

    # Closeable apps return None
    assert close_tool.precheck({"name": "notepad"}) is None
    assert close_tool.precheck({"name": "calculator"}) is None

    # Uncloseable apps (process=None) return error
    err_explorer = close_tool.precheck({"name": "explorer"})
    assert err_explorer is not None and "not allowed to close explorer" in err_explorer

    err_whatsapp = close_tool.precheck({"name": "whatsapp"})
    assert err_whatsapp is not None and "not allowed to close whatsapp" in err_whatsapp

    # Unknown app returns error
    err_unknown = close_tool.precheck({"name": "unknown_app"})
    assert err_unknown is not None and "not in the allowed list" in err_unknown

