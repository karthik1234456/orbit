"""Unit and integration tests for ORBIT native Windows system controls.

All tests strictly use fakes and tmp_path only:
- Fake key sender, fake lock function, fake URI opener, fake clock.
- No test calls ctypes.windll, os.startfile, subprocess, real .env, real data/, logs/, or workspace/.
- Zero live key presses, zero real screen locks, zero real settings pages opened.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from core.agent import Agent, TAINT_WARNING
from core.bootstrap import AppPaths, build_app
from core.claims import Claim, FAMILIES, find_action_claims, unverified_claims
from core.diagnostics import build_diagnostics_text
from core.doctor import Probes, check_system_controls
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.shortcuts import parse_shortcut
from routines.validate import RoutineValidationError, validate_routine_steps
from security.audit import AuditLogger
from security.permissions import PermissionLevel, PermissionPolicy
from system.winkeys import (
    MEDIA_NEXT,
    MEDIA_PLAY_PAUSE,
    MEDIA_PREV,
    MEDIA_STOP,
    VOLUME_DOWN,
    VOLUME_MUTE,
    VOLUME_UP,
    SystemControlUnavailable,
    send_key,
    set_key_sender,
    set_lock_fn,
    set_uri_opener,
)
from tools.registry import Tool, ToolRegistry
from tools.system_tools import (
    SETTINGS_PAGES,
    get_rate_limit_refusal_count,
    make_system_tools,
    reset_rate_limits,
)


class ScriptedProvider(LLMProvider):
    """Test provider returning pre-scripted LLMResponse items."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
        on_delta=None,
        should_stop=None,
    ) -> LLMResponse:
        self.call_history.append(list(messages))
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="Fallback response")


@pytest.fixture(autouse=True)
def clean_system_state(monkeypatch):
    """Reset rate limits, clear injected callables, and ensure clean system controls state."""
    reset_rate_limits()
    set_key_sender(None)
    set_lock_fn(None)
    set_uri_opener(None)
    monkeypatch.setenv("SYSTEM_CONTROLS_ENABLED", "1")
    yield
    reset_rate_limits()
    set_key_sender(None)
    set_lock_fn(None)
    set_uri_opener(None)


# ---------------------------------------------------------------------------
# 1. Source code scanner (no subprocess / os.system)
# ---------------------------------------------------------------------------

def test_source_code_contains_no_subprocess_or_os_system():
    """Verify that system/ and tools/system_tools.py contain zero subprocess or os.system calls."""
    repo_root = Path(__file__).resolve().parent.parent
    checked_files = [
        repo_root / "system" / "winkeys.py",
        repo_root / "system" / "__init__.py",
        repo_root / "tools" / "system_tools.py",
    ]
    for file_path in checked_files:
        assert file_path.is_file(), f"File {file_path} not found"
        content = file_path.read_text(encoding="utf-8")
        assert "subprocess" not in content, f"Forbidden 'subprocess' found in {file_path}"
        assert "os.system" not in content, f"Forbidden 'os.system' found in {file_path}"
        assert "shell=True" not in content, f"Forbidden 'shell=True' found in {file_path}"


# ---------------------------------------------------------------------------
# 2. Tool invocation, valid args, prechecks, step caps
# ---------------------------------------------------------------------------

def test_volume_change_tool_invocation_and_precheck():
    """Verify volume_change tool invocation, step limits, and precheck validation."""
    sent_keys = []
    tools = make_system_tools(sender=sent_keys.append)
    tool_map = {t.name: t for t in tools}
    tool = tool_map["volume_change"]

    # Valid invocation up (default 3 steps)
    res_up = tool.func(direction="up")
    assert res_up == "Sent volume up x3 (each press is roughly 2% on Windows)."
    assert sent_keys == [VOLUME_UP, VOLUME_UP, VOLUME_UP]

    # Valid invocation down (2 steps)
    sent_keys.clear()
    res_down = tool.func(direction="down", steps=2)
    assert res_down == "Sent volume down x2 (each press is roughly 2% on Windows)."
    assert sent_keys == [VOLUME_DOWN, VOLUME_DOWN]

    # Invalid direction
    res_err_dir = tool.func(direction="left")
    assert "ERROR: direction must be 'up' or 'down'." in res_err_dir

    # Invalid steps (out of range 1-10)
    assert "ERROR: steps must be an integer between 1 and 10." in tool.func(direction="up", steps=0)
    assert "ERROR: steps must be an integer between 1 and 10." in tool.func(direction="up", steps=11)
    assert "ERROR: steps must be an integer between 1 and 10." in tool.func(direction="up", steps="not_num")

    # Precheck tests
    assert tool.precheck({"direction": "up", "steps": 5}) is None
    assert tool.precheck({"direction": "invalid"}) == "ERROR: direction must be 'up' or 'down'."
    assert tool.precheck({"direction": "up", "steps": 15}) == "ERROR: steps must be an integer between 1 and 10."
    assert tool.precheck({"direction": "up", "steps": "five"}) == "ERROR: steps must be an integer between 1 and 10."


def test_volume_mute_toggle_tool_invocation():
    """Verify volume_mute_toggle tool invocation and honest result string."""
    sent_keys = []
    tools = make_system_tools(sender=sent_keys.append)
    tool_map = {t.name: t for t in tools}
    tool = tool_map["volume_mute_toggle"]

    res = tool.func()
    assert res == "Sent the mute toggle; I can't see whether sound is now muted."
    assert sent_keys == [VOLUME_MUTE]


def test_media_key_tool_invocation_and_precheck():
    """Verify media_key tool actions, precheck validation, and honest result strings."""
    sent_keys = []
    tools = make_system_tools(sender=sent_keys.append)
    tool_map = {t.name: t for t in tools}
    tool = tool_map["media_key"]

    actions_map = {
        "play_pause": (MEDIA_PLAY_PAUSE, "play/pause"),
        "next": (MEDIA_NEXT, "next"),
        "previous": (MEDIA_PREV, "previous"),
        "stop": (MEDIA_STOP, "stop"),
    }

    for action, (expected_vk, display_name) in actions_map.items():
        sent_keys.clear()
        res = tool.func(action=action)
        assert res == f"Sent {display_name} to Windows; whichever app has media focus will react, I can't confirm which."
        assert sent_keys == [expected_vk]

    # Invalid action
    res_inv = tool.func(action="shuffle")
    assert "ERROR: action must be one of: play_pause, next, previous, stop." in res_inv

    # Precheck
    assert tool.precheck({"action": "next"}) is None
    assert tool.precheck({"action": "rewind"}) == "ERROR: action must be one of: play_pause, next, previous, stop."


def test_lock_screen_tool_invocation():
    """Verify lock_screen tool invocation via fake lock function."""
    lock_called = []
    tools = make_system_tools(lock_fn=lambda: lock_called.append(True))
    tool_map = {t.name: t for t in tools}
    tool = tool_map["lock_screen"]

    res = tool.func()
    assert res == "Locking the screen."
    assert lock_called == [True]


def test_open_settings_page_tool_invocation_and_mapping():
    """Verify open_settings_page maps keys strictly from SETTINGS_PAGES and rejects unknown keys."""
    opened = []
    tools = make_system_tools(opener=opened.append)
    tool_map = {t.name: t for t in tools}
    tool = tool_map["open_settings_page"]

    # Every key in SETTINGS_PAGES opens its exact ms-settings: URI
    for page_key, expected_uri in SETTINGS_PAGES.items():
        opened.clear()
        res = tool.func(page=page_key)
        assert res == f"Opened {page_key.capitalize()} settings."
        assert opened == [expected_uri]
        assert expected_uri.startswith("ms-settings:")

    # Unknown key is rejected
    opened.clear()
    res_unknown = tool.func(page="mouse")
    assert "ERROR: Unknown settings page 'mouse'" in res_unknown
    assert not opened

    # Model string never in URI (security test)
    res_malicious = tool.func(page="wifi; calc.exe")
    assert "ERROR: Unknown settings page" in res_malicious
    assert not opened

    # Precheck
    assert tool.precheck({"page": "bluetooth"}) is None
    assert "ERROR: Unknown settings page 'printer'" in (tool.precheck({"page": "printer"}) or "")


# ---------------------------------------------------------------------------
# 3. Rate limiter with fake clock
# ---------------------------------------------------------------------------

def test_rate_limiter_volume_and_media_keys():
    """Verify rate limits: max 30 volume keys/min, max 10 media keys/min with fake clock."""
    cur_time = datetime(2026, 10, 30, 12, 0, 0)
    fake_clock = lambda: cur_time

    sent_keys = []
    tools = make_system_tools(sender=sent_keys.append, clock=fake_clock)
    tool_map = {t.name: t for t in tools}
    vol_tool = tool_map["volume_change"]
    media_tool = tool_map["media_key"]

    # 1. Volume rate limit: 30 presses/min
    # Send 3 presses x 10 times = 30 presses
    for _ in range(10):
        res = vol_tool.func(direction="up", steps=3)
        assert not res.startswith("ERROR")

    assert len(sent_keys) == 30
    assert get_rate_limit_refusal_count() == 0

    # 31st press fails within the same minute
    res_fail = vol_tool.func(direction="up", steps=1)
    assert res_fail == "ERROR: too many key presses, try again in a minute."
    assert get_rate_limit_refusal_count() == 1

    # Advance fake clock by 61 seconds -> bucket resets
    cur_time += timedelta(seconds=61)
    res_ok = vol_tool.func(direction="down", steps=2)
    assert res_ok == "Sent volume down x2 (each press is roughly 2% on Windows)."
    assert get_rate_limit_refusal_count() == 1

    # 2. Media rate limit: 10 presses/min
    sent_keys.clear()
    for _ in range(10):
        res = media_tool.func(action="play_pause")
        assert not res.startswith("ERROR")
    assert len(sent_keys) == 10

    # 11th press fails
    res_media_fail = media_tool.func(action="next")
    assert res_media_fail == "ERROR: too many key presses, try again in a minute."
    assert get_rate_limit_refusal_count() == 2

    # Advance fake clock by 61 seconds
    cur_time += timedelta(seconds=61)
    res_media_ok = media_tool.func(action="next")
    assert "Sent next to Windows" in res_media_ok


# ---------------------------------------------------------------------------
# 4. Non-Windows platform defense
# ---------------------------------------------------------------------------

def test_non_windows_platform_defense():
    """On non-Windows platforms without test callables, tools return empty list or raise SystemControlUnavailable."""
    with patch("sys.platform", "linux"):
        # make_system_tools without test fakes returns []
        tools = make_system_tools()
        assert tools == []

        # Direct invocation of system/winkeys.py callables raises SystemControlUnavailable
        with pytest.raises(SystemControlUnavailable) as exc_info:
            send_key(0xAF)
        assert "System controls are only available on Windows" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. Taint escalation security policy
# ---------------------------------------------------------------------------

def test_taint_escalation_security_policy(tmp_path: Path):
    """Safe system tools with escalate_when_tainted=True escalate to CONFIRM in tainted turns."""
    audit_file = tmp_path / "audit.jsonl"
    audit = AuditLogger(str(audit_file))
    registry = ToolRegistry()

    sent_keys = []
    sys_tools = make_system_tools(sender=sent_keys.append)
    for t in sys_tools:
        registry.register(t)

    # Verify tool metadata
    assert registry.get("volume_change").escalate_when_tainted is True
    assert registry.get("volume_mute_toggle").escalate_when_tainted is True
    assert registry.get("media_key").escalate_when_tainted is True
    assert registry.get("lock_screen").escalate_when_tainted is False
    assert registry.get("open_settings_page").escalate_when_tainted is False

    # Untainted turn: SAFE volume_change executes without asking confirmation
    confirm_called = []
    def confirm_cb(tool, args, warning=None):
        confirm_called.append((tool, warning))
        return True

    provider_untainted = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="volume_change", args={"direction": "up", "steps": 3})]),
        LLMResponse(text="Volume adjusted.", tool_calls=[]),
    ])
    policy = PermissionPolicy()
    agent = Agent(
        provider=provider_untainted,
        registry=registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )
    reply1 = agent.chat("Turn it up")
    assert "Volume adjusted." in reply1
    assert len(confirm_called) == 0  # Untainted turn did NOT ask confirmation
    assert len(sent_keys) == 3

    # Tainted turn: tainted turn with volume_change triggers Decision.ASK with TAINT_WARNING
    sent_keys.clear()
    confirm_called.clear()

    # Register an untrusted tool to taint the agent
    registry.register(
        Tool(
            name="read_untrusted_doc",
            description="Reads doc",
            parameters={"type": "object", "properties": {}},
            func=lambda: "Suspicious doc content",
            permission=PermissionLevel.SAFE,
            untrusted_output=True,
        )
    )

    provider_tainted = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="read_untrusted_doc", args={})]),
        LLMResponse(text="", tool_calls=[ToolCall(name="volume_change", args={"direction": "down", "steps": 2})]),
        LLMResponse(text="Turned down.", tool_calls=[]),
    ])

    agent_tainted = Agent(
        provider=provider_tainted,
        registry=registry,
        policy=policy,
        audit=audit,
        confirm_callback=confirm_cb,
    )
    reply2 = agent_tainted.chat("Read doc and adjust volume")
    assert "Turned down." in reply2

    # Confirmation callback WAS called with TAINT_WARNING!
    assert len(confirm_called) == 1
    assert confirm_called[0][0] == "volume_change"
    assert confirm_called[0][1] == TAINT_WARNING

    # Audit log records escalated_tainted
    audit_entries = audit.read_all()
    esc_entries = [e for e in audit_entries if e.get("decision") == "escalated_tainted"]
    assert len(esc_entries) == 1
    assert esc_entries[0]["tool"] == "volume_change"
    assert esc_entries[0]["outcome"] == "success"

    # Run trace records escalated_tainted
    run_dict = agent_tainted.last_run.to_dict()
    steps = run_dict["steps"]
    vol_step = [s for s in steps if s["tool"] == "volume_change"][0]
    assert vol_step["decision"] == "escalated_tainted"

    # User denial on escalated tainted action behaves like standard denial
    confirm_called.clear()
    provider_denied = ScriptedProvider([
        LLMResponse(text="", tool_calls=[ToolCall(name="read_untrusted_doc", args={})]),
        LLMResponse(text="", tool_calls=[ToolCall(name="volume_change", args={"direction": "up", "steps": 3})]),
        LLMResponse(text="Cancelled.", tool_calls=[]),
    ])
    agent_denied = Agent(
        provider=provider_denied,
        registry=registry,
        policy=policy,
        audit=audit,
        confirm_callback=lambda tool, args, warning=None: False,  # User denies
    )
    _ = agent_denied.chat("Read doc and volume up")
    audit_entries_denied = audit.read_all()
    user_denied_entries = [e for e in audit_entries_denied if e.get("decision") == "user_denied" and e.get("tool") == "volume_change"]
    assert len(user_denied_entries) == 1


# ---------------------------------------------------------------------------
# 6. Action claims family "system"
# ---------------------------------------------------------------------------

def test_claims_family_system():
    """Verify family 'system' detects first-person claims and respects negation awareness."""
    assert "system" in FAMILIES
    assert FAMILIES["system"] == {
        "volume_change",
        "volume_mute_toggle",
        "media_key",
        "lock_screen",
        "open_settings_page",
    }

    # First-person claims detected
    c1 = find_action_claims("I've turned the volume down.")
    assert len(c1) == 1 and c1[0].kind == "system"

    c2 = find_action_claims("I muted the sound.")
    assert len(c2) == 1 and c2[0].kind == "system"

    c3 = find_action_claims("I locked your screen.")
    assert len(c3) == 1 and c3[0].kind == "system"

    c4 = find_action_claims("I opened settings.")
    assert len(c4) == 1 and c4[0].kind == "system"

    c5 = find_action_claims("I paused the music.")
    assert len(c5) == 1 and c5[0].kind == "system"

    c6 = find_action_claims("I skipped the song.")
    assert len(c6) == 1 and c6[0].kind == "system"

    # Negations produce no claims
    assert find_action_claims("I can't turn the volume down.") == []
    assert find_action_claims("I cannot mute the sound.") == []
    assert find_action_claims("I won't lock your screen.") == []
    assert find_action_claims("I am unable to open settings.") == []
    assert find_action_claims("I couldn't pause the music.") == []

    # Verification with successful tool vs unverified without tool
    claim_sys = Claim(kind="system", phrase="I locked your screen.")
    assert unverified_claims([claim_sys], []) == [claim_sys]

    # Errored tool does not verify
    assert unverified_claims([claim_sys], [("lock_screen", {}, "error")]) == [claim_sys]

    # Successful tool verifies
    assert unverified_claims([claim_sys], [("lock_screen", {}, "success")]) == []


# ---------------------------------------------------------------------------
# 7. Deterministic shortcuts, near-misses, conflicts, and chains
# ---------------------------------------------------------------------------

def test_system_control_shortcuts_and_near_misses():
    """Verify all system control shortcuts parse correctly, and near-misses return None."""
    # Volume up / down
    s1 = parse_shortcut("volume up")
    assert s1.name == "volume_change" and s1.args == {"direction": "up", "steps": 3}

    s2 = parse_shortcut("volume down")
    assert s2.name == "volume_change" and s2.args == {"direction": "down", "steps": 3}

    s3 = parse_shortcut("turn the volume up a bit")
    assert s3.name == "volume_change" and s3.args == {"direction": "up", "steps": 2}

    s4 = parse_shortcut("turn volume down a lot")
    assert s4.name == "volume_change" and s4.args == {"direction": "down", "steps": 5}

    s5 = parse_shortcut("volume up by 4")
    assert s5.name == "volume_change" and s5.args == {"direction": "up", "steps": 4}

    s6 = parse_shortcut("volume down by seven")
    assert s6.name == "volume_change" and s6.args == {"direction": "down", "steps": 7}

    s7 = parse_shortcut("louder")
    assert s7.name == "volume_change" and s7.args == {"direction": "up", "steps": 3}

    s8 = parse_shortcut("quieter")
    assert s8.name == "volume_change" and s8.args == {"direction": "down", "steps": 3}

    s9 = parse_shortcut("softer")
    assert s9.name == "volume_change" and s9.args == {"direction": "down", "steps": 3}

    # Mute toggle
    s10 = parse_shortcut("mute the sound")
    assert s10.name == "volume_mute_toggle"

    s11 = parse_shortcut("unmute the speakers")
    assert s11.name == "volume_mute_toggle"

    s12 = parse_shortcut("toggle mute")
    assert s12.name == "volume_mute_toggle"

    # Media keys
    s13 = parse_shortcut("pause the music")
    assert s13.name == "media_key" and s13.args == {"action": "play_pause"}

    s14 = parse_shortcut("play the song")
    assert s14.name == "media_key" and s14.args == {"action": "play_pause"}

    s15 = parse_shortcut("resume the track")
    assert s15.name == "media_key" and s15.args == {"action": "play_pause"}

    s16 = parse_shortcut("stop the audio")
    assert s16.name == "media_key" and s16.args == {"action": "stop"}

    s17 = parse_shortcut("next song")
    assert s17.name == "media_key" and s17.args == {"action": "next"}

    s18 = parse_shortcut("skip this track")
    assert s18.name == "media_key" and s18.args == {"action": "next"}

    s19 = parse_shortcut("previous song")
    assert s19.name == "media_key" and s19.args == {"action": "previous"}

    s20 = parse_shortcut("last track")
    assert s20.name == "media_key" and s20.args == {"action": "previous"}

    s21 = parse_shortcut("go back a song")
    assert s21.name == "media_key" and s21.args == {"action": "previous"}

    # Lock screen
    s22 = parse_shortcut("lock the screen")
    assert s22.name == "lock_screen"

    s23 = parse_shortcut("lock my pc")
    assert s23.name == "lock_screen"

    s24 = parse_shortcut("lock computer")
    assert s24.name == "lock_screen"

    # Settings pages
    s25 = parse_shortcut("open bluetooth settings")
    assert s25.name == "open_settings_page" and s25.args == {"page": "bluetooth"}

    s26 = parse_shortcut("open the wifi settings")
    assert s26.name == "open_settings_page" and s26.args == {"page": "wifi"}

    s27 = parse_shortcut("open update settings")
    assert s27.name == "open_settings_page" and s27.args == {"page": "updates"}

    # Near misses that MUST return None
    assert parse_shortcut("volume 100") is None
    assert parse_shortcut("volume up by 15") is None
    assert parse_shortcut("lock") is None
    assert parse_shortcut("open mouse settings") is None
    assert parse_shortcut("open settings") is None


def test_browser_shortcuts_conflict_resolution():
    """Bare browser shortcuts (mute, unmute, pause, play, next video) behave exactly as before."""
    # When browser is enabled:
    # Bare "mute" maps to browser_media (mute)
    b_mute = parse_shortcut("mute", browser_enabled=True)
    assert b_mute.name == "browser_media" and b_mute.args == {"action": "mute"}

    b_unmute = parse_shortcut("unmute", browser_enabled=True)
    assert b_unmute.name == "browser_media" and b_unmute.args == {"action": "unmute"}

    b_pause = parse_shortcut("pause", browser_enabled=True)
    assert b_pause.name == "browser_media" and b_pause.args == {"action": "pause"}

    b_play = parse_shortcut("play", browser_enabled=True)
    assert b_play.name == "browser_media" and b_play.args == {"action": "play"}

    b_next_vid = parse_shortcut("next video", browser_enabled=True)
    assert b_next_vid.name == "browser_click_result" and b_next_vid.args.get("next_video") is True

    b_back = parse_shortcut("go back", browser_enabled=True)
    assert b_back.name == "browser_history" and b_back.args == {"action": "back"}


def test_chains_and_routines_refuse_system_controls():
    """parse_chain returns None for any system control segment, and routines strictly refuse them."""
    from core.chains import parse_chain

    # Chains containing system controls return None
    assert parse_chain("volume up and then open notepad") is None
    assert parse_chain("open notepad and then mute the sound") is None
    assert parse_chain("pause the music and wait 5 seconds") is None
    assert parse_chain("lock the screen, then open calculator") is None

    # Routine validation refuses system control tools
    raw_steps = [
        {"tool": "open_application", "args": {"name": "notepad"}},
        {"tool": "volume_change", "args": {"direction": "up", "steps": 3}},
    ]
    with pytest.raises(RoutineValidationError) as exc_info:
        validate_routine_steps(raw_steps)
    assert "Tool 'volume_change' is not allowed in routines" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8. Settings toggle: SYSTEM_CONTROLS_ENABLED=0
# ---------------------------------------------------------------------------

def test_settings_toggle_disabled(tmp_path: Path):
    """When SYSTEM_CONTROLS_ENABLED=0, no system tools are registered and no shortcuts match."""
    # Shortcuts disabled
    assert parse_shortcut("volume up", system_controls_enabled=False) is None
    assert parse_shortcut("lock the screen", system_controls_enabled=False) is None

    # build_app with system_controls_enabled=False
    paths = AppPaths.under(tmp_path)
    app = build_app(
        provider=ScriptedProvider([]),
        paths=paths,
        system_controls_enabled=False,
    )
    registered_tools = [t.name for t in app.registry.list_tools()]
    assert "volume_change" not in registered_tools
    assert "volume_mute_toggle" not in registered_tools
    assert "media_key" not in registered_tools
    assert "lock_screen" not in registered_tools
    assert "open_settings_page" not in registered_tools


# ---------------------------------------------------------------------------
# 9. Doctor & Diagnostics reports
# ---------------------------------------------------------------------------

def test_doctor_and_diagnostics_system_controls():
    """Verify check_system_controls reports platform and no key sent; diagnostics reports enabled flag & refusals."""
    # Doctor check on Windows enabled
    probes_win_on = Probes(env=lambda k, d=None: "1" if k == "SYSTEM_CONTROLS_ENABLED" else d)
    with patch("sys.platform", "win32"):
        c_on = check_system_controls(probes_win_on)
        assert c_on.status == "OK"
        assert "no key sent" in c_on.detail
        assert "win32" in c_on.detail

    # Doctor check disabled
    probes_win_off = Probes(env=lambda k, d=None: "0" if k == "SYSTEM_CONTROLS_ENABLED" else d)
    with patch("sys.platform", "win32"):
        c_off = check_system_controls(probes_win_off)
        assert c_off.status == "OK"
        assert "disabled via SYSTEM_CONTROLS_ENABLED=0" in c_off.detail
        assert "no key sent" in c_off.detail

    # Diagnostics report contains === SYSTEM CONTROLS === section
    diag_text = build_diagnostics_text()
    assert "=== SYSTEM CONTROLS ===" in diag_text
    assert "SYSTEM_CONTROLS_ENABLED:" in diag_text
    assert "Rate Limit Refusals:" in diag_text
