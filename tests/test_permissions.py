"""Tests for PermissionLevel, Decision, and PermissionPolicy."""

from security.permissions import Decision, PermissionLevel, PermissionPolicy


def test_decision_for_each_level():
    """Verify that SAFE maps to ALLOW, CONFIRM maps to ASK, and RESTRICTED maps to DENY."""
    policy = PermissionPolicy()

    assert policy.decide("read_tool", PermissionLevel.SAFE) == Decision.ALLOW
    assert policy.decide("write_tool", PermissionLevel.CONFIRM) == Decision.ASK
    assert policy.decide("delete_tool", PermissionLevel.RESTRICTED) == Decision.DENY


def test_override_beats_tool_level():
    """Verify that per-tool overrides take priority over the tool's intrinsic permission level."""
    overrides = {
        "read_tool": PermissionLevel.CONFIRM,       # Elevate SAFE to CONFIRM
        "delete_tool": PermissionLevel.CONFIRM,     # Downgrade RESTRICTED to CONFIRM
        "calc_tool": PermissionLevel.RESTRICTED,    # Restrict a SAFE tool
    }
    policy = PermissionPolicy(overrides=overrides)

    # Overridden: SAFE elevated to CONFIRM -> ASK
    assert policy.level_for("read_tool", PermissionLevel.SAFE) == PermissionLevel.CONFIRM
    assert policy.decide("read_tool", PermissionLevel.SAFE) == Decision.ASK

    # Overridden: RESTRICTED downgraded to CONFIRM -> ASK
    assert policy.level_for("delete_tool", PermissionLevel.RESTRICTED) == PermissionLevel.CONFIRM
    assert policy.decide("delete_tool", PermissionLevel.RESTRICTED) == Decision.ASK

    # Overridden: SAFE restricted to RESTRICTED -> DENY
    assert policy.level_for("calc_tool", PermissionLevel.SAFE) == PermissionLevel.RESTRICTED
    assert policy.decide("calc_tool", PermissionLevel.SAFE) == Decision.DENY

    # Un-overridden tool keeps its natural classification
    assert policy.level_for("other_tool", PermissionLevel.SAFE) == PermissionLevel.SAFE
    assert policy.decide("other_tool", PermissionLevel.SAFE) == Decision.ALLOW


def test_unknown_or_none_level_is_denied():
    """Verify deny-by-default behavior: unclassified, None, or unrecognized levels are DENY."""
    policy = PermissionPolicy()

    # None provided as tool_level
    assert policy.level_for("unclassified_tool", None) is None
    assert policy.decide("unclassified_tool", None) == Decision.DENY

    # Arbitrary unrecognized value as tool_level
    assert policy.decide("corrupted_tool", "UNKNOWN_LEVEL") == Decision.DENY
    assert policy.decide("corrupted_tool", 999) == Decision.DENY

    # Override set to None or unrecognized should also deny
    override_policy = PermissionPolicy(overrides={"bad_override": None})
    assert override_policy.decide("bad_override", PermissionLevel.SAFE) == Decision.DENY
