"""Built-in tools for ORBIT assistant: time, arithmetic, system status, and note writing."""

import ast
from datetime import datetime
import operator
from pathlib import Path
import psutil

from security.permissions import PermissionLevel
from tools.registry import Tool, ToolRegistry

# Fixed path for user notes
_NOTES_FILE_PATH = Path("workspace/notes.txt")

# Allowed binary and unary arithmetic operators
_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Safety ceiling on exponent power to protect against denial of service
_MAX_EXPONENT = 1000


def _eval_ast_node(node: ast.AST) -> int | float:
    """Recursively evaluate an AST expression with strict node whitelisting."""
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body)

    if isinstance(node, ast.Constant):
        # Allow ints and floats; reject bools and other constant types
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")

        left = _eval_ast_node(node.left)
        right = _eval_ast_node(node.right)

        # Check exponent size before performing pow calculation
        if op_type is ast.Pow:
            if isinstance(right, (int, float)) and (right > _MAX_EXPONENT or right < -_MAX_EXPONENT):
                raise ValueError(f"Exponent exceeds maximum allowed limit of {_MAX_EXPONENT}")

        return _OPERATORS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = _eval_ast_node(node.operand)
        return _UNARY_OPERATORS[op_type](operand)

    # Reject function calls, attribute access, names, imports, etc.
    raise ValueError(f"Unsupported syntax: {type(node).__name__}")


def get_current_time() -> str:
    """Return the current local date and time formatted in a human-readable string."""
    now = datetime.now()
    return now.strftime("%A, %B %d, %Y %I:%M:%S %p")


def calculate(expression: str) -> str:
    """Safely evaluate an arithmetic expression without eval() or exec()."""
    if not expression or not expression.strip():
        raise ValueError("Expression cannot be empty.")

    try:
        parsed = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid arithmetic expression: {exc.msg}") from exc

    result = _eval_ast_node(parsed)
    return str(result)


def system_info() -> str:
    """Return current CPU percent, RAM in GB, disk usage, and battery percent if available."""
    # Sample CPU percent over a short interval
    cpu_percent = psutil.cpu_percent(interval=0.1)

    # Virtual memory metrics converted to gigabytes
    ram = psutil.virtual_memory()
    ram_used_gb = ram.used / (1024 ** 3)
    ram_total_gb = ram.total / (1024 ** 3)

    # Root disk usage
    disk = psutil.disk_usage("/")
    disk_percent = disk.percent

    # Battery status with fallback for desktop machines
    battery = psutil.sensors_battery()
    if battery is not None:
        battery_str = f"{battery.percent:.1f}%"
        if battery.power_plugged:
            battery_str += " (Charging/Plugged in)"
    else:
        battery_str = "Not available (no battery detected)"

    return (
        f"CPU Usage: {cpu_percent}%\n"
        f"RAM Usage: {ram_used_gb:.2f} GB / {ram_total_gb:.2f} GB ({ram.percent}%)\n"
        f"Disk Usage: {disk_percent}%\n"
        f"Battery: {battery_str}"
    )


def write_note(text: str) -> str:
    """Append text plus a timestamp to workspace/notes.txt using a fixed path."""
    if not text or not text.strip():
        raise ValueError("Note text cannot be empty.")

    # Ensure the parent folder exists before writing
    _NOTES_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_entry = f"[{timestamp}] {text.strip()}\n"

    with open(_NOTES_FILE_PATH, "a", encoding="utf-8") as f:
        f.write(formatted_entry)

    return f"Note saved successfully to {_NOTES_FILE_PATH}."


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register all built-in tools with appropriate permission levels."""
    registry.register(
        Tool(
            name="get_current_time",
            description=(
                "Get the current local date and time. Use this tool whenever the user "
                "asks for the current time, today's date, day of the week, month, or year."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            func=get_current_time,
            permission=PermissionLevel.SAFE,
        )
    )

    registry.register(
        Tool(
            name="calculate",
            description=(
                "Safely evaluate a mathematical arithmetic expression. Supports numbers, "
                "parentheses, addition (+), subtraction (-), multiplication (*), division (/), "
                "floor division (//), modulo (%), exponentiation (**), and unary minus. "
                "Use this tool whenever exact mathematical calculations are required."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The arithmetic expression to evaluate, e.g. '2 + 3 * 4'.",
                    }
                },
                "required": ["expression"],
            },
            func=calculate,
            permission=PermissionLevel.SAFE,
        )
    )

    registry.register(
        Tool(
            name="system_info",
            description=(
                "Check the local computer's hardware and system metrics including CPU utilization, "
                "RAM usage (used and total in GB), disk space percent, and battery level. "
                "Use this tool when the user inquires about device performance or resource status."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            func=system_info,
            permission=PermissionLevel.SAFE,
        )
    )

    registry.register(
        Tool(
            name="write_note",
            description=(
                "Appends a quick note to notes.txt. Use ONLY when the user asks for a quick note and names no file. If the user names a file such as summary.txt, use create_file instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The note content to append to the notes file.",
                    }
                },
                "required": ["text"],
            },
            func=write_note,
            permission=PermissionLevel.CONFIRM,
        )
    )
