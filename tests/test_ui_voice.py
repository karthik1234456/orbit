"""Unit and integration tests for desktop GUI voice controller and interactions.

All tests use fakes and mocks exclusively: zero network access, zero audio hardware,
and zero live model calls.
"""

import sys
import threading
import time
from typing import Any, Optional

import numpy as np

from tests.conftest import check_tk_available, make_test_root
from ui.controller import ConfirmRequest, UiEvent
from ui.voice_controller import VoiceUiController, strip_page_titles
from voice.audio import AudioError
from voice.stt import Transcript, TranscriptionError
from voice.tts import SpeechError

# Verify Tkinter is available or skip
check_tk_available()

from ui.app import ConfirmDialog, OrbitWindow, PlanDialog


def make_speech_audio(duration_s: float = 1.0, freq: float = 300.0) -> np.ndarray:
    """Generate synthetic non-silent mono audio with speech-like envelope."""
    n_samples = int(16000 * duration_s)
    t = np.linspace(0, duration_s, n_samples, endpoint=False, dtype=np.float32)
    sig = (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    pad = int(n_samples * 0.2)
    if pad > 0 and n_samples > 2 * pad:
        sig[:pad] = 0.0
        sig[-pad:] = 0.0
    return sig


class FakeRecorder:
    """Mock audio recorder returning predefined array or waiting on stop_event."""

    def __init__(
        self,
        audio: Optional[np.ndarray] = None,
        error: Optional[Exception] = None,
        level_callback: Optional[Any] = None,
        block_until_stop: bool = False,
    ) -> None:
        self.audio = audio if audio is not None else make_speech_audio()
        self.error = error
        self.level_callback = level_callback
        self.block_until_stop = block_until_stop
        self.calls: int = 0

    def record(self, stop_event: Optional[threading.Event] = None) -> np.ndarray:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.level_callback is not None:
            self.level_callback(0.25)
        if self.block_until_stop and stop_event is not None:
            stop_event.wait(timeout=5.0)
        return self.audio


class FakeTranscriber:
    """Mock Whisper transcriber returning predefined transcript or raising error."""

    def __init__(
        self,
        text: str = "open notepad",
        error: Optional[Exception] = None,
    ) -> None:
        self.text = text
        self.error = error
        self.calls: int = 0

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return Transcript(text=self.text, language="en", duration_s=1.0)


class FakeSpeaker:
    """Mock speaker tracking spoken texts."""

    def __init__(self, error: Optional[Exception] = None) -> None:
        self.spoken: list[str] = []
        self.stop_calls: int = 0
        self.error = error
        self._is_speaking = False

    def speak(self, text: str, blocking: bool = False) -> None:
        if self.error is not None:
            raise self.error
        self.spoken.append(text)
        self._is_speaking = True

    def stop(self) -> None:
        self.stop_calls += 1
        self._is_speaking = False

    def is_speaking(self) -> bool:
        return self._is_speaking


class FakeController:
    """Mock ChatController for testing voice events and UI submissions."""

    def __init__(self) -> None:
        self.submits: list[tuple[str, str]] = []
        self.events: list[UiEvent] = []
        self.is_busy: bool = False
        self.cancel_called: bool = False
        self.shutdown_called: bool = False
        self.app: Optional[Any] = None
        self.voice_controller: Optional[Any] = None
        self.pending_tool: Optional[str] = None
        self.confirmed_resolutions: list[tuple[int, bool]] = []
        self.plan_resolutions: list[tuple[int, bool]] = []

    def _emit(self, kind: str, data: Optional[dict] = None) -> None:
        self.events.append(UiEvent(kind=kind, data=data or {}))

    def submit(self, text: str, source: str = "typed") -> bool:
        if self.is_busy:
            return False
        self.submits.append((text, source))
        return True

    def cancel(self) -> None:
        self.cancel_called = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def drain(self, max_items: int = 100) -> list[UiEvent]:
        evs = self.events[:max_items]
        self.events = self.events[max_items:]
        return evs

    def resolve_confirmation(self, request_id: int, allowed: bool) -> None:
        self.confirmed_resolutions.append((request_id, allowed))

    def resolve_plan(self, plan_id: Any, allowed: bool) -> None:
        self.plan_resolutions.append((plan_id, allowed))


# --- Voice Controller Unit Tests ---


def test_voice_recording_stops_on_stop_listening():
    """Verify start_listening runs recording on worker thread and stop_listening finishes it."""
    events: list[tuple[str, dict]] = []
    rec = FakeRecorder(block_until_stop=True)
    trans = FakeTranscriber(text="play jazz")

    ctrl = VoiceUiController(
        recorder_factory=lambda **kw: rec,
        transcriber_factory=lambda: trans,
        speaker=FakeSpeaker(),
        emit=lambda kind, data: events.append((kind, data)),
    )

    started = ctrl.start_listening()
    assert started is True
    assert ctrl.state == "listening"

    # Wait briefly for thread to start recording
    time.sleep(0.05)
    ctrl.stop_listening()

    # Wait for recording and transcription cycle to complete
    worker = ctrl._worker_thread
    if worker is not None:
        worker.join(timeout=2.0)

    assert ctrl.state == "idle"
    assert rec.calls == 1
    assert trans.calls == 1

    transcripts = [d for k, d in events if k == "voice_transcript"]
    assert len(transcripts) == 1
    assert transcripts[0]["text"] == "play jazz"
    assert transcripts[0]["source"] == "voice"
    ctrl.shutdown()


def test_voice_recording_capped_at_max_seconds():
    """Verify recorder receives stop_event and respects duration limit."""
    stop_event_received: list[threading.Event] = []

    class CappedRecorder:
        def __init__(self, **kw):
            pass

        def record(self, stop_event=None):
            if stop_event is not None:
                stop_event_received.append(stop_event)
                # Wait briefly simulating timeout
                stop_event.wait(timeout=0.05)
            return make_speech_audio()

    ctrl = VoiceUiController(
        recorder_factory=lambda **kw: CappedRecorder(),
        transcriber_factory=lambda: FakeTranscriber(text="capped speech"),
        emit=lambda k, d: None,
    )

    ctrl.start_listening()
    time.sleep(0.1)
    ctrl.shutdown()

    assert len(stop_event_received) == 1


def test_voice_silent_audio_emits_friendly_error():
    """Verify silent audio array (<0.005 RMS) emits friendly error and no transcript."""
    events: list[tuple[str, dict]] = []
    silent_audio = np.zeros(16000, dtype=np.float32)

    ctrl = VoiceUiController(
        recorder_factory=lambda **kw: FakeRecorder(audio=silent_audio),
        transcriber_factory=lambda: FakeTranscriber(text="should not transcribe"),
        emit=lambda kind, data: events.append((kind, data)),
    )

    ctrl.start_listening()
    ctrl.shutdown()

    errors = [d for k, d in events if k == "voice_error"]
    transcripts = [d for k, d in events if k == "voice_transcript"]

    assert len(errors) == 1
    assert "didn't hear anything" in errors[0]["text"]
    assert len(transcripts) == 0


def test_voice_short_audio_emits_friendly_error():
    """Verify audio array shorter than 0.3s emits friendly error and no transcript."""
    events: list[tuple[str, dict]] = []
    short_audio = make_speech_audio(duration_s=0.2)

    ctrl = VoiceUiController(
        recorder_factory=lambda **kw: FakeRecorder(audio=short_audio),
        transcriber_factory=lambda: FakeTranscriber(text="should not transcribe"),
        emit=lambda kind, data: events.append((kind, data)),
    )

    ctrl.start_listening()
    ctrl.shutdown()

    errors = [d for k, d in events if k == "voice_error"]
    transcripts = [d for k, d in events if k == "voice_transcript"]

    assert len(errors) == 1
    assert "didn't hear anything" in errors[0]["text"]
    assert len(transcripts) == 0


def test_voice_start_refused_while_blocked():
    """Verify start_listening is refused when is_blocked returns True."""
    events: list[tuple[str, dict]] = []
    blocked = True

    ctrl = VoiceUiController(
        recorder_factory=lambda **kw: FakeRecorder(),
        transcriber_factory=lambda: FakeTranscriber(),
        emit=lambda kind, data: events.append((kind, data)),
        is_blocked=lambda: blocked,
    )

    started = ctrl.start_listening()
    assert started is False
    assert ctrl.state == "idle"

    errors = [d for k, d in events if k == "voice_error"]
    assert len(errors) == 1
    assert "not available" in errors[0]["text"]


def test_voice_cancel_stops_recording_and_speech():
    """Verify cancel() aborts recording, transcribing, and active speech immediately."""
    speaker = FakeSpeaker()
    speaker.speak("long speech test")
    assert speaker.is_speaking() is True

    ctrl = VoiceUiController(
        recorder_factory=lambda **kw: FakeRecorder(block_until_stop=True),
        transcriber_factory=lambda: FakeTranscriber(),
        speaker=speaker,
        emit=lambda k, d: None,
    )

    ctrl.start_listening()
    assert ctrl.state == "listening"

    ctrl.cancel()
    assert ctrl.state == "idle"
    assert speaker.stop_calls == 1
    assert speaker.is_speaking() is False


def test_audio_and_transcription_error_mappings():
    """Verify AudioError and TranscriptionError produce clean messages without tracebacks."""
    ctrl = VoiceUiController()

    msg1 = ctrl._map_audio_error(AudioError("sounddevice is not installed. Please install it."))
    assert "sounddevice" in msg1
    assert "Traceback" not in msg1

    msg2 = ctrl._map_audio_error(AudioError("No microphone or audio input device found."))
    assert "No microphone found" in msg2

    msg3 = ctrl._map_transcription_error(
        TranscriptionError("Whisper model not found locally; offline mode enabled")
    )
    assert "downloaded once while online" in msg3

    msg4 = ctrl._map_transcription_error(
        TranscriptionError("faster-whisper is not installed.")
    )
    assert "faster-whisper" in msg4


def test_level_events_rate_limiting():
    """Verify level_callback limits emissions to at most 10 per second."""
    emitted_levels: list[float] = []
    ctrl = VoiceUiController(
        emit=lambda k, d: emitted_levels.append(d["level"]) if k == "voice_level" else None,
    )

    # Fire 50 level callbacks with negligible delay
    for i in range(50):
        ctrl._level_callback(0.1 + i * 0.01)

    # In a near-instant loop, only 1 event should pass the >= 0.1s rate limit
    assert len(emitted_levels) <= 2


def test_spoken_replies_cleaned_and_page_titles_stripped():
    """Verify strip_page_titles and clean_for_speech format text before speech."""
    speaker = FakeSpeaker()
    ctrl = VoiceUiController(
        speaker=speaker,
        emit=lambda k, d: None,
    )
    ctrl.speak_replies = True

    # 1. Test strip_page_titles
    raw1 = "Opened result 1: Lofi Hip Hop Radio - Beats to Relax/Study to. Playing."
    assert strip_page_titles(raw1) == "Opened result 1. Playing."

    raw2 = "Opened result 2: Classical Music Concert Live"
    assert strip_page_titles(raw2) == "Opened result 2."

    # 2. Test speak_reply stripping page titles and capping at 500 characters
    ctrl.speak_reply("Opened result 1: Massive Page Title Here. Playing.")
    assert len(speaker.spoken) == 1
    assert speaker.spoken[0] == "Opened result 1. Playing."

    # Test 500-char cap
    long_reply = "A" * 600
    ctrl.speak_reply(long_reply)
    assert len(speaker.spoken) == 2
    assert len(speaker.spoken[1]) <= 500


def test_spoken_replies_disabled_by_default():
    """Verify speak_replies is False by default and does not speak."""
    speaker = FakeSpeaker()
    ctrl = VoiceUiController(
        speaker=speaker,
        emit=lambda k, d: None,
    )
    assert ctrl.speak_replies is False

    ctrl.speak_reply("Hello there, how can I help you today?")
    assert len(speaker.spoken) == 0


def test_failed_speech_does_not_crash_controller():
    """Verify exception in speaker.speak produces voice_error event and sets idle state."""
    events: list[tuple[str, dict]] = []
    speaker = FakeSpeaker(error=SpeechError("Audio pipe broken"))

    ctrl = VoiceUiController(
        speaker=speaker,
        emit=lambda k, d: events.append((k, d)),
    )
    ctrl.speak_replies = True

    ctrl.speak_reply("Testing failure speech")
    assert ctrl.state == "idle"

    errors = [d for k, d in events if k == "voice_error"]
    assert len(errors) == 1
    assert "Audio pipe broken" in errors[0]["text"]


# --- OrbitWindow Desktop GUI Integration Tests ---


def test_orbit_window_voice_transcript_submitted_with_voice_source():
    """Verify voice_transcript in window adds Heard/User to chat and submits with source='voice'."""
    root = make_test_root()
    controller = FakeController()

    try:
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
        )

        event = UiEvent(kind="voice_transcript", data={"text": "create file test.txt"})
        window._handle_event(event)

        # Submitted with source="voice"
        assert len(controller.submits) == 1
        assert controller.submits[0] == ("create file test.txt", "voice")

        # Chat history contains Heard and User lines
        chat_text = window.history.get("1.0", "end")
        assert "Heard: create file test.txt" in chat_text
        assert "User: create file test.txt" in chat_text
    finally:
        root.destroy()


def test_orbit_window_cancel_on_confirm_dialog_open():
    """Verify opening ConfirmDialog immediately calls voice_controller.cancel()."""
    root = make_test_root()
    controller = FakeController()

    try:
        rec = FakeRecorder(block_until_stop=True)
        vc = VoiceUiController(
            recorder_factory=lambda **kw: rec,
            transcriber_factory=lambda: FakeTranscriber(),
            speaker=FakeSpeaker(),
            emit=lambda k, d: None,
        )
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
            voice_controller=vc,
        )
        vc.is_blocked = window._is_blocked

        vc.start_listening()
        assert vc.state == "listening"

        req = ConfirmRequest(
            id=101,
            tool="create_file",
            args={"path": "notes.txt"},
            warning=None,
            timeout_s=30,
        )
        window._handle_event(UiEvent(kind="confirm_request", data={"request": req}))

        # Confirm dialog opened and voice cancelled
        assert vc.state == "idle"
        assert window._confirm_dialog is not None
        assert window._is_blocked() is True

        # While dialog is open, start_listening is refused
        started = vc.start_listening()
        assert started is False

        # Close dialog
        window._confirm_dialog._resolve(False)
    finally:
        vc.shutdown()
        root.destroy()


def test_orbit_window_cancel_on_plan_dialog_open():
    """Verify opening PlanDialog immediately calls voice_controller.cancel()."""
    root = make_test_root()
    controller = FakeController()

    try:
        rec = FakeRecorder(block_until_stop=True)
        vc = VoiceUiController(
            recorder_factory=lambda **kw: rec,
            transcriber_factory=lambda: FakeTranscriber(),
            speaker=FakeSpeaker(),
            emit=lambda k, d: None,
        )
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
            voice_controller=vc,
        )
        vc.is_blocked = window._is_blocked

        vc.start_listening()
        assert vc.state == "listening"

        window._handle_event(UiEvent(
            kind="plan_preview",
            data={
                "id": "p-1",
                "labels": ["Open YouTube", "Search lofi"],
                "permissions": ["CONFIRM", "SAFE"],
                "timeout_s": 30,
            },
        ))

        # Plan dialog opened and voice cancelled
        assert vc.state == "idle"
        assert window._plan_dialog is not None
        assert window._is_blocked() is True

        # While dialog is open, start_listening is refused
        started = vc.start_listening()
        assert started is False

        # Close dialog
        window._plan_dialog._resolve(False)
    finally:
        vc.shutdown()
        root.destroy()


def test_confirm_and_plan_dialogs_cannot_be_resolved_by_voice():
    """Verify resolve_confirmation and resolve_plan are only invoked by button/Esc/timeout."""
    root = make_test_root()
    controller = FakeController()

    try:
        req = ConfirmRequest(id=1, tool="create_file", args={}, warning=None, timeout_s=30)
        dialog = ConfirmDialog(root, controller, req)

        # ConfirmDialog has NO voice-related attributes or methods
        assert not hasattr(dialog, "on_voice")
        assert not hasattr(dialog, "resolve_by_voice")

        # Enter triggers safe default (Deny)
        dialog._on_enter()
        assert controller.confirmed_resolutions == [(1, False)]

        plan_dialog = PlanDialog(
            root,
            controller,
            plan_id="p-1",
            labels=["step 1"],
            permissions=["SAFE"],
            timeout_s=30,
        )
        assert not hasattr(plan_dialog, "on_voice")
        assert not hasattr(plan_dialog, "resolve_by_voice")

        plan_dialog._on_enter()
        assert controller.plan_resolutions == [("p-1", False)]
    finally:
        root.destroy()


def test_orbit_window_stop_button_cancels_voice_and_speech():
    """Verify GUI stop button calls cancel() on voice controller."""
    root = make_test_root()
    controller = FakeController()

    try:
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
        )
        vc = window.voice_controller
        vc.start_listening()
        assert vc.state == "listening"

        window._on_stop()

        assert vc.state == "idle"
        assert controller.cancel_called is True
    finally:
        vc.shutdown()
        root.destroy()


def test_audio_arrays_never_written_to_disk_and_absent_from_diag(monkeypatch):
    """Verify audio data is never written to disk and omitted from diagnostics."""
    written_files: list[str] = []

    orig_open = open

    def fake_open(file, *args, **kwargs):
        written_files.append(str(file))
        return orig_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    root = make_test_root()
    controller = FakeController()

    try:
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
        )
        vc = window.voice_controller

        # Simulate voice recording and completion
        vc.start_listening()
        time.sleep(0.05)
        vc.shutdown()

        # Check no .wav, .raw, or audio temp files were created
        audio_writes = [f for f in written_files if f.endswith((".wav", ".raw", ".pcm", ".mp3"))]
        assert len(audio_writes) == 0

        # Build diagnostics
        from core.diagnostics import build_diagnostics_text

        diag = build_diagnostics_text(
            chat_controller=controller,
            voice_controller=vc,
        )

        assert "=== VOICE ===" in diag
        assert "Microphone Available:" in diag
        assert "Speech Model:" in diag
        assert "Speech Language:" in diag
        assert "Speak Replies:" in diag
        assert "Last Voice Error:" in diag
        assert "Last Transcription Duration:" in diag
        # Audio bytes or raw arrays must NOT be in diag text
        assert "ndarray" not in diag
        assert "PCM" not in diag
    finally:
        root.destroy()


def test_mock_mode_without_sounddevice_or_whisper_installed(monkeypatch):
    """Verify mock mode functions when sounddevice and faster_whisper are absent."""
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    root = make_test_root()
    controller = FakeController()

    try:
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
        )

        can_use, reason = window._check_voice_hardware_and_packages()
        assert can_use is True
        assert reason == ""
        assert str(window.mic_btn.cget("state")) == "normal"
        assert window.mic_btn.cget("text") == "mock voice"
    finally:
        root.destroy()


def test_non_mock_mode_missing_sounddevice_disables_mic(monkeypatch):
    """Verify non-mock mode disables mic button when sounddevice is absent."""
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    root = make_test_root()
    controller = FakeController()

    try:
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="LIVE",
        )

        can_use, reason = window._check_voice_hardware_and_packages()
        assert can_use is False
        assert "sounddevice" in reason
        assert str(window.mic_btn.cget("state")) == "disabled"
    finally:
        root.destroy()


def test_malformed_voice_events_do_not_crash_poll_loop():
    """Verify malformed UiEvent payloads do not disrupt the polling loop."""
    root = make_test_root()
    controller = FakeController()

    try:
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
        )

        # Malformed voice events
        window._handle_event(UiEvent(kind="voice_state", data=None))
        window._handle_event(UiEvent(kind="voice_level", data={"level": "invalid"}))
        window._handle_event(UiEvent(kind="voice_transcript", data=None))
        window._handle_event(UiEvent(kind="voice_error", data=None))

        # Window remains intact and functional
        assert window.root.winfo_exists()
    finally:
        root.destroy()


def test_ctrl_m_shortcut_toggles_mic():
    """Verify Ctrl+M triggers mic toggle."""
    root = make_test_root()
    controller = FakeController()

    try:
        window = OrbitWindow(
            root=root,
            controller=controller,
            mode_label="MOCK",
        )
        vc = window.voice_controller

        # First Ctrl+M starts listening
        window._on_ctrl_m()
        assert vc.state == "listening"

        # Second Ctrl+M stops listening
        window._on_ctrl_m()
        worker = vc._worker_thread
        if worker is not None:
            worker.join(timeout=2.0)
        vc.shutdown()
        assert vc.state == "idle"
    finally:
        root.destroy()
