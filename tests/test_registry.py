"""Tests for ToolRegistry and Tool definition."""

import pytest
from tools.registry import Tool, ToolRegistry


def add_numbers(a: int, b: int) -> int:
    return a + b


def calculate_ratio(val: float, multiplier: float = 1.0) -> float:
    return val * multiplier


def greet(name: str, shout: bool = False) -> str:
    msg = f"Hello, {name}"
    return msg.upper() if shout else msg


def raise_error(reason: str) -> str:
    raise RuntimeError(f"Failure occurred: {reason}")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="add",
            description="Add two integers",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
            func=add_numbers,
        )
    )
    reg.register(
        Tool(
            name="ratio",
            description="Multiply a number",
            parameters={
                "type": "object",
                "properties": {
                    "val": {"type": "number"},
                    "multiplier": {"type": "number"},
                },
                "required": ["val"],
            },
            func=calculate_ratio,
        )
    )
    reg.register(
        Tool(
            name="greet",
            description="Greet someone",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "shout": {"type": "boolean"},
                },
                "required": ["name"],
            },
            func=greet,
        )
    )
    reg.register(
        Tool(
            name="faulty",
            description="A tool that intentionally throws an error",
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
            func=raise_error,
        )
    )
    return reg


def test_register_and_get(registry: ToolRegistry):
    """Test registering tools and retrieving them by name and listing."""
    tool = registry.get("add")
    assert tool is not None
    assert tool.name == "add"
    assert tool.description == "Add two integers"

    tools = registry.list_tools()
    assert len(tools) == 4
    assert any(t.name == "greet" for t in tools)

    assert registry.get("non_existent") is None


def test_duplicate_registration(registry: ToolRegistry):
    """Test that registering a tool with an already existing name raises ValueError."""
    duplicate_tool = Tool(
        name="add",
        description="Another adder",
        parameters={"type": "object", "properties": {}},
        func=lambda: 0,
    )
    with pytest.raises(ValueError, match="already registered"):
        registry.register(duplicate_tool)


def test_successful_execute(registry: ToolRegistry):
    """Test successful tool execution with valid arguments returning string results."""
    result = registry.execute("add", {"a": 10, "b": 25})
    assert result == "35"

    result_ratio = registry.execute("ratio", {"val": 3.5, "multiplier": 2.0})
    assert result_ratio == "7.0"

    result_greet = registry.execute("greet", {"name": "Orbit", "shout": True})
    assert result_greet == "HELLO, ORBIT"


def test_unknown_tool(registry: ToolRegistry):
    """Test that executing an unknown tool returns an error string starting with ERROR:."""
    result = registry.execute("unknown_cmd", {"arg": "value"})
    assert result.startswith("ERROR:")
    assert "Unknown tool 'unknown_cmd'" in result


def test_missing_required_argument(registry: ToolRegistry):
    """Test that executing with missing required arguments returns ERROR:."""
    result = registry.execute("add", {"a": 5})
    assert result.startswith("ERROR:")
    assert "Missing required argument 'b'" in result


def test_wrong_argument_type(registry: ToolRegistry):
    """Test that passing wrong argument types returns ERROR: for string, integer, number, boolean."""
    # Integer expected, got string
    res_str_for_int = registry.execute("add", {"a": "five", "b": 2})
    assert res_str_for_int.startswith("ERROR:")
    assert "wrong type" in res_str_for_int

    # Integer expected, got bool (bool is a subclass of int in Python)
    res_bool_for_int = registry.execute("add", {"a": True, "b": 2})
    assert res_bool_for_int.startswith("ERROR:")

    # String expected, got int
    res_int_for_str = registry.execute("greet", {"name": 12345})
    assert res_int_for_str.startswith("ERROR:")

    # Boolean expected, got string
    res_str_for_bool = registry.execute("greet", {"name": "Sam", "shout": "yes"})
    assert res_str_for_bool.startswith("ERROR:")

    # Number expected, got string
    res_str_for_num = registry.execute("ratio", {"val": "invalid"})
    assert res_str_for_num.startswith("ERROR:")


def test_unexpected_extra_argument(registry: ToolRegistry):
    """Test that providing arguments not defined in the tool properties returns ERROR:."""
    result = registry.execute("add", {"a": 1, "b": 2, "c": 3})
    assert result.startswith("ERROR:")
    assert "Unexpected extra argument 'c'" in result


def test_tool_function_raises(registry: ToolRegistry):
    """Test that exceptions raised inside tool functions are caught and returned as ERROR:."""
    result = registry.execute("faulty", {"reason": "database unavailable"})
    assert result.startswith("ERROR:")
    assert "Failure occurred: database unavailable" in result
