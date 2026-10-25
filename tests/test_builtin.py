"""Tests for built-in tools: get_current_time, calculate, system_info."""

import pytest
from tools.builtin import calculate, get_current_time, register_builtin_tools, system_info
from tools.registry import ToolRegistry


def test_calculate_valid_expressions():
    """Verify calculate computes standard arithmetic with operator precedence."""
    assert calculate("2+3*4") == "14"
    assert calculate("(2 + 3) * 4") == "20"
    assert calculate("10 / 2") == "5.0"
    assert calculate("17 // 3") == "5"
    assert calculate("17 % 3") == "2"
    assert calculate("2 ** 8") == "256"
    assert calculate("-5 + 12") == "7"
    assert calculate("-(3 + 2)") == "-5"


def test_calculate_rejects_import():
    """Verify calculate rejects arbitrary code injection like __import__('os')."""
    with pytest.raises(ValueError, match="Unsupported syntax"):
        calculate("__import__('os')")


def test_calculate_rejects_huge_exponent():
    """Verify calculate guards against excessive exponentiation."""
    with pytest.raises(ValueError, match="Exponent exceeds maximum allowed limit"):
        calculate("2**99999999")


def test_calculate_handles_division_by_zero():
    """Verify calculate handles division by zero by raising ZeroDivisionError."""
    with pytest.raises(ZeroDivisionError):
        calculate("1 / 0")

    with pytest.raises(ZeroDivisionError):
        calculate("10 // 0")

    with pytest.raises(ZeroDivisionError):
        calculate("10 % 0")


def test_calculate_rejects_variables_and_function_calls():
    """Verify calculate rejects names and non-whitelisted constructs."""
    with pytest.raises(ValueError, match="Unsupported syntax"):
        calculate("x + 1")

    with pytest.raises(ValueError, match="Unsupported syntax"):
        calculate("abs(-5)")


def test_get_current_time_returns_readable_string():
    """Verify get_current_time returns a non-empty human-readable time string."""
    result = get_current_time()
    assert isinstance(result, str)
    assert len(result) > 0
    # Should contain day, month or year elements
    assert any(str(year) in result for year in range(2025, 2030))


def test_system_info_returns_metrics():
    """Verify system_info returns CPU, RAM, Disk, and Battery metrics."""
    result = system_info()
    assert isinstance(result, str)
    assert "CPU Usage:" in result
    assert "RAM Usage:" in result
    assert "Disk Usage:" in result
    assert "Battery:" in result


def test_register_builtin_tools_integration():
    """Verify register_builtin_tools registers all 3 tools in a ToolRegistry."""
    registry = ToolRegistry()
    register_builtin_tools(registry)

    assert registry.get("get_current_time") is not None
    assert registry.get("calculate") is not None
    assert registry.get("system_info") is not None
    assert registry.get("write_note") is not None

    # Verify assigned permission levels
    from security.permissions import PermissionLevel
    assert registry.get("get_current_time").permission == PermissionLevel.SAFE
    assert registry.get("calculate").permission == PermissionLevel.SAFE
    assert registry.get("system_info").permission == PermissionLevel.SAFE
    assert registry.get("write_note").permission == PermissionLevel.CONFIRM

    # Test execution through the registry
    calc_res = registry.execute("calculate", {"expression": "2+3*4"})
    assert calc_res == "14"

    error_import = registry.execute("calculate", {"expression": "__import__('os')"})
    assert error_import.startswith("ERROR:")

    error_exponent = registry.execute("calculate", {"expression": "2**99999999"})
    assert error_exponent.startswith("ERROR:")

    error_zero_div = registry.execute("calculate", {"expression": "10 / 0"})
    assert error_zero_div.startswith("ERROR:")
    assert "division by zero" in error_zero_div


def test_write_note_appends_to_file():
    """Verify write_note appends text to workspace/notes.txt with timestamp."""
    from tools.builtin import write_note, _NOTES_FILE_PATH

    res = write_note("Buy milk and eggs")
    assert "Note saved successfully" in res
    assert _NOTES_FILE_PATH.exists()

    content = _NOTES_FILE_PATH.read_text(encoding="utf-8")
    assert "Buy milk and eggs" in content

    with pytest.raises(ValueError, match="cannot be empty"):
        write_note("   ")
