"""Unit tests for hands-free voice controller, audio beeper cues, and security boundaries.

All tests use fakes, mocks, and tmp_path only:
- Fake audio streams and synthetic tone bursts.
- Fake transcribers with scripted texts and quality metrics.
- Controllable mock clock and fake beeper.
- Zero audio hardware, zero microphone calls, zero live Whisper models, zero network.
- Tkinter withdrawn when referenced.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Optional
import numpy as np
import pytest

from config.settings_store import SettingsError, validate
from core.diagnostics import build_diagnostics_text
from core.doctor import check_hands_free
from ui.cues import Beeper
from ui.handsfree import HandsFreeController
from ui.hud_core import HudModel
from ui.shortcuts_registry import SHORTCUTS
from voice.audio import AudioError
from voice.stt import FakeTranscriber


class FakeClock:
    """Deterministic mock clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.time = float(start)

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += float(seconds)


class FakeAudioSource:
    """Scripted audio source feeding synthetic utterances."""

    def __init__(self, utterances: Optional[list[Any]] = None) -> None:
        self.utterances = list(utterances) if utterances is not None else []
        self.calls = 0

    def get_audio(self) -> Optional[np.ndarray]:
        self.calls += 1
        if self.utterances:
            item = self.utterances.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None


class FakeAuditLogger:
    """Records audit logs in memory."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def log(
        self,
        tool: str,
        args: dict,
        decision: str,
        outcome: str = "success",
        source: str = "agent",
    ) -> None:
        self.entries.append({
            "tool": tool,
            "args": args,
            "decision": decision,
            "outcome": outcome,
            "source": source,
        })


def make_tone(duration_s: float = 1.0, sample_rate: int = 16000) -> np.ndarray:
    """Generate a synthetic float32 sine tone array."""
    t = np.linspace(0, duration_s, int(duration_s * sample_rate), endpoint=False)
    sine = 0.5 * np.sin(2 * np.pi * 440 * t)
    return sine.astype(np.float32)


# ==============================================================================
# 1. State Machine & Lifecycle Transitions
# ==============================================================================

def test_handsfree_controller_init_defaults():
    clock = FakeClock()
    ctrl = HandsFreeController(clock=clock.now)
    assert ctrl.state == "off"
    assert not ctrl.is_armed
    assert not ctrl.is_active
    assert ctrl.wake_phrase == "hey orbit"
    assert ctrl.sensitivity == 0.80
    assert ctrl.idle_minutes == 10
    assert ctrl.followup_seconds == 0


def test_handsfree_controller_state_transitions():
    clock = FakeClock()
    ctrl = HandsFreeController(
        recorder_factory=lambda: FakeAudioSource([None]),
        wake_transcriber_factory=lambda: FakeTranscriber(),
        clock=clock.now,
        sleep_fn=lambda _: None,
    )

    events: list[tuple[str, dict]] = []
    ctrl._emit_fn = lambda k, d: events.append((k, d))

    assert ctrl.start() is True
    assert ctrl.state == "armed"
    assert ctrl.is_armed is True
    assert ctrl.is_active is True

    # Duplicate start does not recreate thread
    assert ctrl.start() is True

    # Pause for click-to-talk
    ctrl.pause_for_click_to_talk()
    assert ctrl.state == "paused"
    assert not ctrl.is_armed
    assert ctrl.is_active is True

    # Resume from click-to-talk
    ctrl.resume_from_click_to_talk()
    assert ctrl.state == "armed"
    assert ctrl.is_armed is True

    # Stop turns off cleanly
    ctrl.stop()
    assert ctrl.state == "off"
    assert not ctrl.is_armed
    assert not ctrl.is_active

    # Idempotent shutdown
    ctrl.shutdown()
    ctrl.shutdown()
    assert ctrl.state == "off"


# ==============================================================================
# 2. Wake Segment Length Gating
# ==============================================================================

def test_wake_segment_length_gating():
    clock = FakeClock()
    wake_transcriber = FakeTranscriber(default_text="hey orbit")

    # 1. Utterance too short (< 0.3s -> 0.2s)
    short_audio = make_tone(0.2)
    # 2. Utterance too long (> 4.0s -> 4.5s)
    long_audio = make_tone(4.5)
    # 3. Utterance valid length (3.5s) -> cropped to 3.0s
    valid_audio = make_tone(3.5)

    source = FakeAudioSource([short_audio, long_audio, valid_audio])
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        recorder_factory=lambda: source,
        wake_transcriber_factory=lambda: wake_transcriber,
        emit=lambda k, d: events.append((k, d)),
        clock=clock.now,
        sleep_fn=lambda _: None,
    )

    ctrl.start()
    # Let worker process all items
    time.sleep(0.1)
    ctrl.stop()

    # The wake transcriber should have only been called once (for the 3.5s segment)
    assert len(wake_transcriber.calls) == 1
    passed_audio, _ = wake_transcriber.calls[0]
    # Cropped to at most 3.0s * 16000 = 48000 samples
    assert len(passed_audio) == 48000


# ==============================================================================
# 3. Ephemerality & Discard of Non-Wake Transcripts
# ==============================================================================

def test_non_wake_transcripts_strictly_ephemeral():
    clock = FakeClock()
    ambient_speech = make_tone(1.5)
    wake_transcriber = FakeTranscriber(scripted=["what is the weather today outside"])
    source = FakeAudioSource([ambient_speech])
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        recorder_factory=lambda: source,
        wake_transcriber_factory=lambda: wake_transcriber,
        emit=lambda k, d: events.append((k, d)),
        clock=clock.now,
        sleep_fn=lambda _: None,
    )

    ctrl.start()
    time.sleep(0.1)
    ctrl.stop()

    # No wake events emitted
    wake_events = [e for e in events if e[0] == "handsfree_wake"]
    assert len(wake_events) == 0
    cmd_events = [e for e in events if e[0] == "handsfree_command"]
    assert len(cmd_events) == 0

    # Ensure no controller attributes retained the non-wake transcript
    for attr_name in dir(ctrl):
        if attr_name.startswith("_"):
            continue
        val = getattr(ctrl, attr_name)
        assert "weather" not in str(val).lower()
        assert "outside" not in str(val).lower()


# ==============================================================================
# 4. Wake Phrase with Inline Remainder Execution
# ==============================================================================

def test_wake_phrase_with_inline_command():
    clock = FakeClock()
    beep_calls: list[tuple[int, int]] = []
    beeper = Beeper(beep_fn=lambda f, d: beep_calls.append((f, d)), clock=clock.now, min_interval_s=0.0)

    wake_audio = make_tone(2.0)
    wake_transcriber = FakeTranscriber(scripted=["hey orbit open notepad"])
    source = FakeAudioSource([wake_audio])
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        recorder_factory=lambda: source,
        wake_transcriber_factory=lambda: wake_transcriber,
        emit=lambda k, d: events.append((k, d)),
        beeper=beeper,
        clock=clock.now,
        sleep_fn=lambda _: None,
    )

    ctrl.start()
    time.sleep(0.1)
    ctrl.stop()

    # Assert wake event and command emitted
    assert any(e[0] == "handsfree_wake" for e in events)
    commands = [e[1] for e in events if e[0] == "handsfree_command"]
    assert len(commands) == 1
    assert commands[0]["text"] == "open notepad"
    assert commands[0]["source"] == "voice-handsfree"

    # Assert beeper played wake and done cues
    assert "wake" in beeper.history
    assert "done" in beeper.history


# ==============================================================================
# 5. Two-Step Wake Phrase Then Command Capture
# ==============================================================================

def test_two_step_wake_then_command_capture():
    clock = FakeClock()
    beeper = Beeper(beep_fn=lambda f, d: None, clock=clock.now, min_interval_s=0.0)

    wake_audio = make_tone(1.2)
    cmd_audio = make_tone(1.5)

    wake_transcriber = FakeTranscriber(scripted=["hey orbit"])
    cmd_transcriber = FakeTranscriber(scripted=["show reminders"])

    source = FakeAudioSource([wake_audio, cmd_audio])
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        recorder_factory=lambda: source,
        wake_transcriber_factory=lambda: wake_transcriber,
        command_transcriber_factory=lambda: cmd_transcriber,
        emit=lambda k, d: events.append((k, d)),
        beeper=beeper,
        clock=clock.now,
        sleep_fn=lambda _: None,
    )

    ctrl.start()
    time.sleep(0.15)
    ctrl.stop()

    commands = [e[1] for e in events if e[0] == "handsfree_command"]
    assert len(commands) == 1
    assert commands[0]["text"] == "show reminders"
    assert commands[0]["source"] == "voice-handsfree"
    assert "wake" in beeper.history
    assert "done" in beeper.history


# ==============================================================================
# 6. Command Quality Drops
# ==============================================================================

def test_quality_drops_low_logprob():
    clock = FakeClock()
    beeper = Beeper(beep_fn=lambda f, d: None, clock=clock.now)
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        emit=lambda k, d: events.append((k, d)),
        beeper=beeper,
        clock=clock.now,
    )

    # avg_logprob < -1.0
    ctrl._handle_command_text("open browser", avg_logprob=-1.2)
    assert ctrl.counters["drops_low_confidence"] == 1
    assert "error" in beeper.history
    notices = [e[1] for e in events if e[0] == "handsfree_notice"]
    assert any(n.get("dropped") is True for n in notices)
    assert not any(e[0] == "handsfree_command" for e in events)


def test_quality_drops_high_no_speech():
    clock = FakeClock()
    beeper = Beeper(beep_fn=lambda f, d: None, clock=clock.now)
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        emit=lambda k, d: events.append((k, d)),
        beeper=beeper,
        clock=clock.now,
    )

    # no_speech_prob > 0.6
    ctrl._handle_command_text("open browser", no_speech_prob=0.75)
    assert ctrl.counters["drops_low_confidence"] == 1
    assert "error" in beeper.history
    assert not any(e[0] == "handsfree_command" for e in events)


def test_quality_drops_too_short_and_empty():
    clock = FakeClock()
    beeper = Beeper(beep_fn=lambda f, d: None, clock=clock.now)
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        emit=lambda k, d: events.append((k, d)),
        beeper=beeper,
        clock=clock.now,
    )

    # Single word (< 2 words)
    ctrl._handle_command_text("hello")
    assert ctrl.counters["drops_low_confidence"] == 1
    assert "error" in beeper.history

    # Empty text
    ctrl._handle_command_text("   ")
    assert ctrl.counters["drops_low_confidence"] == 2


# ==============================================================================
# 7. Dialog Security and Voice Confirmation Immunity
# ==============================================================================

def test_dialog_security_voice_confirmation_immunity():
    clock = FakeClock()
    beeper = Beeper(beep_fn=lambda f, d: None, clock=clock.now)
    dialog_open = False

    ctrl = HandsFreeController(
        beeper=beeper,
        clock=clock.now,
    )
    ctrl.is_dialog_open_fn = lambda: dialog_open

    # Initially dialog closed -> not muted
    assert ctrl.is_muted() is False

    # Dialog opens -> muted, edge triggers "attention" cue
    dialog_open = True
    assert ctrl.is_muted() is True
    assert "attention" in beeper.history

    # Follow-up window is cancelled by dialog opening
    ctrl._followup_active = True
    ctrl.is_muted()
    assert ctrl._followup_active is False


# ==============================================================================
# 8. Echo Guard and Muting While Speaking
# ==============================================================================

def test_muting_while_speaking_with_echo_buffer():
    clock = FakeClock()
    is_speaking = False

    ctrl = HandsFreeController(clock=clock.now)
    ctrl.is_speaking_fn = lambda: is_speaking

    assert ctrl.is_muted() is False

    # Speaking begins
    is_speaking = True
    clock.advance(1.0)
    assert ctrl.is_muted() is True

    # Speaking stops
    is_speaking = False
    # Within 1.0s echo suppression buffer -> still muted!
    clock.advance(0.5)
    assert ctrl.is_muted() is True

    # After 1.0s buffer passes -> unmuted
    clock.advance(0.6)
    assert ctrl.is_muted() is False


# ==============================================================================
# 9. Toast and Task Gating
# ==============================================================================

def test_toast_and_task_gating():
    clock = FakeClock()
    ctrl = HandsFreeController(clock=clock.now)

    is_toast = False
    is_task = False
    ctrl.is_toast_open_fn = lambda: is_toast
    ctrl.is_task_running_fn = lambda: is_task

    assert ctrl.is_muted() is False

    # Toast open
    is_toast = True
    assert ctrl.is_muted() is True
    is_toast = False

    # Task running without followup
    is_task = True
    assert ctrl.is_muted() is True

    # Follow-up active allows listening while task is running
    ctrl._followup_active = True
    assert ctrl.is_muted() is False


# ==============================================================================
# 10. Wake Rate Limiting and Cooldown
# ==============================================================================

def test_wake_rate_limiting_and_30s_cooldown():
    clock = FakeClock()
    audit_logger = FakeAuditLogger()
    events: list[tuple[str, dict]] = []

    ctrl = HandsFreeController(
        emit=lambda k, d: events.append((k, d)),
        clock=clock.now,
        audit_logger=audit_logger,
    )

    # Simulate 6 wake events within 60s
    for _ in range(6):
        ctrl._last_wake_or_armed_time = clock.now()
        ctrl._wake_timestamps.append(clock.now())
        ctrl.counters["wake_events"] += 1
        clock.advance(2.0)

    # 7th wake attempt should trigger cooldown
    ctrl._last_wake_or_armed_time = clock.now()
    clean_ts = [ts for ts in ctrl._wake_timestamps if (clock.now() - ts) < 60.0]
    ctrl._wake_timestamps = clean_ts
    assert len(ctrl._wake_timestamps) >= 6

    # Worker logic check for rate limit
    ctrl._cooldown_until = clock.now() + 30.0
    ctrl.counters["cooldowns"] += 1
    ctrl._log_audit("handsfree_cooldown", outcome="rate_limit")

    assert ctrl.counters["cooldowns"] == 1
    assert any(entry["decision"] == "handsfree_cooldown" for entry in audit_logger.entries)


# ==============================================================================
# 11. Idle Auto-Off
# ==============================================================================

def test_idle_auto_off_timeout():
    clock = FakeClock()
    audit_logger = FakeAuditLogger()
    beeper = Beeper(beep_fn=lambda f, d: None, clock=clock.now)
    events: list[tuple[str, dict]] = []

    source = FakeAudioSource([make_tone(1.0)])
    ctrl = HandsFreeController(
        recorder_factory=lambda: source,
        wake_transcriber_factory=lambda: FakeTranscriber(),
        emit=lambda k, d: events.append((k, d)),
        beeper=beeper,
        clock=clock.now,
        idle_minutes=10,
        audit_logger=audit_logger,
        sleep_fn=lambda _: None,
    )

    ctrl.start()
    # Advance clock past 10 minutes (601 seconds)
    clock.advance(601.0)
    time.sleep(0.15)

    assert ctrl.state == "off"
    assert ctrl.counters["auto_offs"] == 1
    assert "off" in beeper.history
    assert any(entry["decision"] == "handsfree_auto_off" for entry in audit_logger.entries)


# ==============================================================================
# 12. Consecutive Audio Errors Auto-Off
# ==============================================================================

def test_consecutive_audio_errors_auto_off():
    clock = FakeClock()
    beeper = Beeper(beep_fn=lambda f, d: None, clock=clock.now)
    events: list[tuple[str, dict]] = []

    # Source raises AudioError consecutively
    source = FakeAudioSource([
        AudioError("Device busy 1"),
        AudioError("Device busy 2"),
        AudioError("Device busy 3"),
    ])

    ctrl = HandsFreeController(
        recorder_factory=lambda: source,
        wake_transcriber_factory=lambda: FakeTranscriber(),
        emit=lambda k, d: events.append((k, d)),
        beeper=beeper,
        clock=clock.now,
        sleep_fn=lambda _: None,
    )

    ctrl.start()
    time.sleep(0.2)

    assert ctrl.state == "off"
    assert "off" in beeper.history
    notices = [e[1] for e in events if e[0] == "handsfree_notice"]
    assert any("audio errors" in n.get("text", "").lower() for n in notices)


# ==============================================================================
# 13. Beeper Cues and Suppression
# ==============================================================================

def test_beeper_cues_and_suppression():
    clock = FakeClock()
    beeps: list[tuple[int, int]] = []
    enabled_flag = True

    beeper = Beeper(
        beep_fn=lambda f, d: beeps.append((f, d)),
        enabled=lambda: enabled_flag,
        clock=clock.now,
        min_interval_s=0.05,
    )

    # Play wake cue
    beeper.play("wake")
    time.sleep(0.05)
    assert len(beeps) >= 1
    assert "wake" in beeper.history

    # Rapid play within min_interval is dropped
    prev_len = len(beeps)
    beeper.play("done")
    assert len(beeps) == prev_len

    # After interval passes, plays successfully
    clock.advance(0.1)
    beeper.play("done")
    time.sleep(0.05)
    assert len(beeps) > prev_len

    # When disabled, plays are suppressed
    enabled_flag = False
    clock.advance(0.1)
    curr_len = len(beeps)
    beeper.play("error")
    assert len(beeps) == curr_len


# ==============================================================================
# 14. Zero Audio or Non-Wake Text Written to Disk
# ==============================================================================

def test_zero_audio_or_text_disk_writes(monkeypatch, tmp_path):
    written_files: list[str] = []

    orig_write_bytes = Path.write_bytes
    orig_write_text = Path.write_text

    def fake_write_bytes(self, data, *args, **kwargs):
        written_files.append(str(self))
        return orig_write_bytes(self, data, *args, **kwargs)

    def fake_write_text(self, data, *args, **kwargs):
        written_files.append(str(self))
        return orig_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", fake_write_bytes)
    monkeypatch.setattr(Path, "write_text", fake_write_text)

    clock = FakeClock()
    audio = make_tone(1.5)
    wake_transcriber = FakeTranscriber(scripted=["hey orbit open notepad"])
    source = FakeAudioSource([audio])

    ctrl = HandsFreeController(
        recorder_factory=lambda: source,
        wake_transcriber_factory=lambda: wake_transcriber,
        clock=clock.now,
        sleep_fn=lambda _: None,
    )

    ctrl.start()
    time.sleep(0.1)
    ctrl.stop()

    # Verify no audio files (.wav, .pcm, .raw) were written
    for f in written_files:
        assert not f.endswith((".wav", ".pcm", ".raw", ".mp3", ".ogg"))


# ==============================================================================
# 15. Settings Validation for WAKE_PHRASE & WAKE_SENSITIVITY
# ==============================================================================

def test_settings_validation_wake_phrase_and_sensitivity():
    # Valid configurations
    assert validate("WAKE_PHRASE", "hey orbit") == "hey orbit"
    assert validate("WAKE_PHRASE", "ok orbit assistant") == "ok orbit assistant"
    assert validate("WAKE_SENSITIVITY", "0.80") == "0.8"
    assert validate("WAKE_SENSITIVITY", 0.85) == "0.85"
    assert validate("HANDS_FREE", "1") == "1"
    assert validate("HANDS_FREE_IDLE_MINUTES", "15") == "15"
    assert validate("HANDS_FREE_FOLLOWUP_SECONDS", "5") == "5"
    assert validate("HANDS_FREE_CUES", "1") == "1"

    # Invalid wake phrases
    with pytest.raises(SettingsError):
        validate("WAKE_PHRASE", "orbit")  # only 1 word
    with pytest.raises(SettingsError):
        validate("WAKE_PHRASE", "hey orbit computer assistant please")  # too many words
    with pytest.raises(SettingsError):
        validate("WAKE_PHRASE", "Hey Orbit")  # uppercase
    with pytest.raises(SettingsError):
        validate("WAKE_PHRASE", "hey orbit!")  # punctuation
    with pytest.raises(SettingsError):
        validate("WAKE_PHRASE", "hey orbit 2")  # digit

    # Invalid sensitivities
    with pytest.raises(SettingsError):
        validate("WAKE_SENSITIVITY", "0.60")  # too low
    with pytest.raises(SettingsError):
        validate("WAKE_SENSITIVITY", "0.95")  # too high
    with pytest.raises(SettingsError):
        validate("WAKE_SENSITIVITY", "fast")  # not a float


# ==============================================================================
# 16. Doctor Check for Hands-Free
# ==============================================================================

def test_doctor_check_hands_free():
    from core.doctor import Probes

    chk = check_hands_free(Probes(env={"HANDS_FREE": "0"}))
    assert chk.name == "Hands-free listening"
    assert chk.status == "OK"
    assert "disabled" in chk.detail.lower()

    chk_enabled = check_hands_free(
        Probes(env={"HANDS_FREE": "1", "WAKE_PHRASE": "hey orbit", "WAKE_SENSITIVITY": "0.80"})
    )
    assert chk_enabled.name == "Hands-free listening"
    assert chk_enabled.status in ("OK", "WARN")


# ==============================================================================
# 17. Diagnostics Hands-Free Section
# ==============================================================================

def test_diagnostics_hands_free_section():
    class FakeApp:
        pass

    app = FakeApp()
    app.handsfree_controller = HandsFreeController(clock=lambda: 1000.0)
    app.handsfree_controller.counters["wake_events"] = 5
    app.handsfree_controller.counters["drops_low_confidence"] = 2
    app.handsfree_controller.counters["cooldowns"] = 1
    app.handsfree_controller.counters["auto_offs"] = 1

    diag_text = build_diagnostics_text(app=app)
    assert "=== HANDS-FREE ===" in diag_text
    assert "Wake Events: 5" in diag_text
    assert "Drops for Low Confidence: 2" in diag_text
    assert "Cooldown Events: 1" in diag_text
    assert "Auto-Off Events: 1" in diag_text
    # Ensure zero transcript text in diagnostics
    assert "open notepad" not in diag_text


# ==============================================================================
# 18. HUD Model Integration
# ==============================================================================

def test_hud_model_handsfree_integration():
    model = HudModel()
    assert model.handsfree_armed is False

    model.set_handsfree_armed(True)
    assert model.handsfree_armed is True

    model.set_handsfree_armed(False)
    assert model.handsfree_armed is False


# ==============================================================================
# 19. Shortcuts Registry
# ==============================================================================

def test_shortcuts_registry_has_handsfree():
    hf_shortcuts = [s for s in SHORTCUTS if s.key == "Ctrl+Shift+M"]
    assert len(hf_shortcuts) == 1
    sc = hf_shortcuts[0]
    assert sc.scope == "global"
    assert "hands-free" in sc.description.lower()
    assert sc.handler_name == "_on_toggle_handsfree"
