"""Tool definition and registry for agent tool calling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from security.permissions import PermissionLevel


@dataclass
class Tool:
    """Represents a callable tool with JSON-schema parameter specifications."""
    name: str
    description: str
    parameters: dict
    func: Callable
    permission: PermissionLevel = PermissionLevel.CONFIRM
    untrusted_output: bool = False
    precheck: Callable[[dict], str | None] | None = None
    escalate_when_tainted: bool = False


class ToolRegistry:
    """Registry managing tool lookup, registration, validation, and safe execution."""

    def __init__(self) -> None:
        # Use a dictionary for O(1) tool lookups by name
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a new tool. Rejects duplicate registrations to prevent ambiguity."""
        if tool.name in self._tools:
            raise ValueError(f"Tool with name '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """Retrieve a registered tool by name or return None if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """Return all currently registered tools."""
        return list(self._tools.values())

    @staticmethod
    def _is_valid_type(val: Any, expected_type: str) -> bool:
        """Check if an argument value matches the expected JSON-schema primitive type."""
        if expected_type == "string":
            return isinstance(val, str)
        if expected_type == "integer":
            # In Python, bool is a subclass of int, so exclude bool explicitly
            return isinstance(val, int) and not isinstance(val, bool)
        if expected_type == "number":
            # Numbers can be integers or floats, but not booleans
            return isinstance(val, (int, float)) and not isinstance(val, bool)
        if expected_type == "boolean":
            return isinstance(val, bool)
        return True

    def validate_arguments(self, name: str, args: dict) -> Optional[str]:
        """Validate tool presence and arguments against schema. Returns error string or None."""
        tool = self._tools.get(name)
        if not tool:
            return f"ERROR: Unknown tool '{name}'"

        if not isinstance(args, dict):
            return f"ERROR: Arguments must be a dictionary, got {type(args).__name__}"

        # Validate that all required parameters are present
        required_params = tool.parameters.get("required", [])
        for param in required_params:
            if param not in args:
                return f"ERROR: Missing required argument '{param}'"

        # Check for arguments not defined in the schema properties
        properties = tool.parameters.get("properties", {})
        for key in args:
            if key not in properties:
                return f"ERROR: Unexpected extra argument '{key}'"

        # Verify parameter types against the schema
        for key, val in args.items():
            expected_type = properties.get(key, {}).get("type")
            if expected_type and not self._is_valid_type(val, expected_type):
                return f"ERROR: Argument '{key}' has wrong type, expected '{expected_type}' but got '{type(val).__name__}'"

        return None

    def execute(self, name: str, args: dict) -> str:
        """Safely execute a tool by name with arguments.

        Guaranteed to never raise an exception, returning a string starting with
        'ERROR:' on any validation or runtime error.
        """
        try:
            val_error = self.validate_arguments(name, args)
            if val_error:
                return val_error

            tool = self._tools[name]
            result = tool.func(**args)
            return str(result)
        except Exception as exc:
            # Trap any error raised by the tool function so the assistant remains operational
            return f"ERROR: Tool '{name}' execution failed: {exc}"

