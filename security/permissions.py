"""Security permissions and policy decisions for tool execution."""

from enum import Enum, auto
from typing import Optional


class PermissionLevel(Enum):
    """Categorizes the risk level associated with executing a tool."""
    SAFE = auto()        # Read-only or benign operations with no side-effects
    CONFIRM = auto()     # Actions that alter state or access sensitive data, needing user consent
    RESTRICTED = auto()  # High-risk or forbidden operations that should not run automatically


class Decision(Enum):
    """The enforcement outcome determined by the permission policy."""
    ALLOW = auto()  # Tool execution may proceed automatically
    ASK = auto()    # Prompt the user for explicit confirmation before running
    DENY = auto()   # Block execution unconditionally


class PermissionPolicy:
    """Evaluates whether tool executions are permitted, require confirmation, or are blocked.

    Adheres to the principle of least privilege and 'deny by default': any missing,
    unrecognized, or unconfigured permission level results in DENY. This safeguards
    against accidental execution of unvetted or newly added tools.
    """

    def __init__(self, overrides: Optional[dict[str, PermissionLevel]] = None) -> None:
        # User-configured overrides take precedence over the tool's intrinsic classification
        self.overrides: dict[str, PermissionLevel] = dict(overrides or {})

    def level_for(
        self,
        tool_name: str,
        tool_level: Optional[PermissionLevel] = None,
    ) -> Optional[PermissionLevel]:
        """Return the effective permission level, applying user overrides first."""
        if tool_name in self.overrides:
            return self.overrides[tool_name]
        return tool_level

    def decide(
        self,
        tool_name: str,
        tool_level: Optional[PermissionLevel] = None,
    ) -> Decision:
        """Determine policy decision: SAFE->ALLOW, CONFIRM->ASK, RESTRICTED->DENY.

        Any unrecognized or None level defaults to DENY to prevent unauthorized
        execution if security metadata is omitted or corrupted.
        """
        effective_level = self.level_for(tool_name, tool_level)

        if effective_level == PermissionLevel.SAFE:
            return Decision.ALLOW
        if effective_level == PermissionLevel.CONFIRM:
            return Decision.ASK
        if effective_level == PermissionLevel.RESTRICTED:
            return Decision.DENY

        # Fail closed: default to DENY for any undefined or anomalous permission level
        return Decision.DENY
