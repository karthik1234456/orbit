"""Tests for computer sleep power tool."""

from config import settings
from security.permissions import PermissionLevel
from tools.power_tools import make_power_tools


def test_sleep_computer_confirmation_and_execution(monkeypatch):
    """Verify sleep_computer requires confirmation, describes intent, and calls suspend_fn."""
    monkeypatch.setattr(settings, "SLEEP_ENABLED", True)

    suspended = []
    fake_clock = [1000.0]

    tools = {
        t.name: t
        for t in make_power_tools(
            suspend_fn=lambda h, f, d: suspended.append((h, f, d)) or True,
            clock=lambda: fake_clock[0],
        )
    }

    t_sleep = tools["sleep_computer"]
    assert t_sleep.permission == PermissionLevel.CONFIRM
    assert t_sleep.block_when_tainted is True
    assert t_sleep.model_visible is False
    assert t_sleep.describe({}) == "Put this computer to sleep now?"

    res = t_sleep.func()
    assert res == "Putting the computer to sleep."
    assert suspended == [(False, False, False)]

    # Rate limiting: 2 per 10 minutes
    res2 = t_sleep.func()
    assert res2 == "Putting the computer to sleep."

    res3 = t_sleep.func()
    assert "Rate limit reached" in res3

    # Reset after 601s
    fake_clock[0] += 601.0
    res4 = t_sleep.func()
    assert res4 == "Putting the computer to sleep."


def test_sleep_computer_disabled_by_default(monkeypatch):
    """Verify sleep_computer is disabled when SLEEP_ENABLED is False."""
    monkeypatch.setattr(settings, "SLEEP_ENABLED", False)

    tools = {t.name: t for t in make_power_tools()}
    t_sleep = tools["sleep_computer"]

    assert t_sleep.precheck({}) == "Sleep is off. Turn on SLEEP_ENABLED in Settings."
    res = t_sleep.func()
    assert "Sleep is off. Turn on SLEEP_ENABLED in Settings." in res
