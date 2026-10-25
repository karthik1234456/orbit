"""Tests for voice/wake.py: Wake word detection, fuzzy phrase matching, privacy preservation, and cooldown logic."""

import threading
import numpy as np

from voice.audio import AudioError
from voice.stt import FakeTranscriber, TranscriptionError
from voice.wake import WakeDetector, WakeResult, match_wake_phrase, normalize


def make_dummy_audio() -> np.ndarray:
    """Generate 0.5s of synthetic audio."""
    return np.zeros(8000, dtype=np.float32)


class MockClock:
    """Controllable mock clock for deterministic testing of cooldowns and timing."""

    def __init__(self, start: float = 1000.0):
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class MockSleeper:
    """Records sleep durations without blocking."""

    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_normalize():
    """normalize() lowercases, strips punctuation, and splits into words."""
    assert normalize("Hey, ORBIT! What's up?") == ["hey", "orbit", "whats", "up"]
    assert normalize("  OK... hey   orbit  ") == ["ok", "hey", "orbit"]
    assert normalize("") == []
    assert normalize("---!@#$---") == []


def test_match_wake_phrase_start_cases():
    """Verify match_wake_phrase matches at start only, handles fuzzy speech-to-text, and strips remainder."""
    # 1. Start with phrase + remainder
    assert match_wake_phrase("Hey Orbit what time is it") == "what time is it"

    # 2. Phrase only returns empty string
    assert match_wake_phrase("hey orbit") == ""

    # 3. Speech-to-text mangled name ("hey or bit") with leading punctuation on remainder
    assert match_wake_phrase("hey, or bit, open notepad") == "open notepad"

    # 4. Optional leading "ok" / "okay"
    assert match_wake_phrase("ok hey orbit hello") == "hello"
    assert match_wake_phrase("okay hey orbit check status") == "check status"

    # 5. Phrase in middle of sentence must NOT match
    assert match_wake_phrase("I said hey orbit to him") is None
    assert match_wake_phrase("I told her hey orbit is cool") is None

    # Empty / whitespace
    assert match_wake_phrase("") is None
    assert match_wake_phrase("   ") is None


def test_wake_result_dataclass():
    """WakeResult stores the remainder text."""
    res = WakeResult(remainder="turn on lights")
    assert res.remainder == "turn on lights"


def test_wake_detector_triggers_and_returns_remainder():
    """WakeDetector transcribes utterance, matches wake phrase, and returns remainder."""
    clock = MockClock()
    sleeper = MockSleeper()
    transcriber = FakeTranscriber(scripted=["Hey Orbit what time is it"])
    detector = WakeDetector(
        transcriber=transcriber,
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
        clock=clock.now,
        sleep=sleeper.sleep,
    )

    result = detector.wait_for_wake()
    assert isinstance(result, WakeResult)
    assert result.remainder == "what time is it"
    assert detector.last_trigger_time == 1000.0


def test_wake_detector_phrase_only():
    """WakeDetector returning empty remainder when only wake phrase is spoken."""
    clock = MockClock()
    sleeper = MockSleeper()
    transcriber = FakeTranscriber(scripted=["hey orbit"])
    detector = WakeDetector(
        transcriber=transcriber,
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
        clock=clock.now,
        sleep=sleeper.sleep,
    )

    result = detector.wait_for_wake()
    assert isinstance(result, WakeResult)
    assert result.remainder == ""


def test_wake_detector_fuzzy_matching_and_leading_ok():
    """WakeDetector handles fuzzy speech-to-text transcriptions and optional ok."""
    clock = MockClock()
    sleeper = MockSleeper()

    # Fuzzy STT mangling: "hey, or bit, open notepad"
    transcriber1 = FakeTranscriber(scripted=["hey, or bit, open notepad"])
    detector1 = WakeDetector(
        transcriber=transcriber1,
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
        clock=clock.now,
        sleep=sleeper.sleep,
    )
    result1 = detector1.wait_for_wake()
    assert isinstance(result1, WakeResult)
    assert result1.remainder == "open notepad"

    # Leading ok: "ok hey orbit hello"
    transcriber2 = FakeTranscriber(scripted=["ok hey orbit hello"])
    detector2 = WakeDetector(
        transcriber=transcriber2,
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
        clock=clock.now,
        sleep=sleeper.sleep,
    )
    result2 = detector2.wait_for_wake()
    assert isinstance(result2, WakeResult)
    assert result2.remainder == "hello"


def test_wake_detector_mid_sentence_does_not_match():
    """Phrase inside a sentence ('I said hey orbit to him') is discarded and doesn't trigger."""
    clock = MockClock()
    sleeper = MockSleeper()
    cancel_event = threading.Event()

    # Transcriber first returns mid-sentence speech, then triggers cancel_event
    class MidSentenceTranscriber:
        def __init__(self):
            self.calls = 0

        def transcribe(self, audio, sample_rate=16000):
            self.calls += 1
            if self.calls == 1:
                return "I said hey orbit to him"
            cancel_event.set()
            return "anything"

    detector = WakeDetector(
        transcriber=MidSentenceTranscriber(),
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
        clock=clock.now,
        sleep=sleeper.sleep,
    )

    result = detector.wait_for_wake(cancel_event=cancel_event)
    assert result is None


def test_unrelated_chatter_never_triggers_and_is_not_stored():
    """Unrelated chatter is discarded immediately and never stored on the detector."""
    clock = MockClock()
    sleeper = MockSleeper()
    cancel_event = threading.Event()

    chatter = [
        "pass the salt please",
        "the weather looks like rain today",
        "talking to someone else in the room",
    ]
    transcriber = FakeTranscriber(scripted=chatter)

    call_count = 0

    def capture(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > len(chatter):
            cancel_event.set()
            return None
        return make_dummy_audio()

    detector = WakeDetector(
        transcriber=transcriber,
        make_segmenter=lambda: None,
        capture_fn=capture,
        phrase="hey orbit",
        clock=clock.now,
        sleep=sleeper.sleep,
    )

    result = detector.wait_for_wake(cancel_event=cancel_event)
    assert result is None

    # Privacy verification: assert the detector keeps no transcript attributes
    forbidden_attrs = ["transcript", "transcripts", "last_text", "text", "history", "logs", "audio", "buffer"]
    for attr in forbidden_attrs:
        assert not hasattr(detector, attr), f"Detector unexpectedly has attribute '{attr}'"

    # Also assert no attribute values on the detector contain any of the chatter strings
    for k, v in vars(detector).items():
        if isinstance(v, str):
            for c in chatter:
                assert c not in v, f"Chatter leaked into attribute '{k}'"


def test_cooldown_ignores_second_trigger_within_3s():
    """A match inside the cooldown period (3s) since the last trigger is ignored."""
    sleeper = MockSleeper()
    timestamps = [100.0, 101.5, 104.0]

    def mock_clock() -> float:
        if timestamps:
            return timestamps.pop(0)
        return 200.0

    # Transcriber supplies three triggers:
    # 1. at t = 100.0 -> accepted
    # 2. at t = 101.5 (1.5s later, < 3s cooldown) -> ignored in loop
    # 3. at t = 104.0 (4.0s later, >= 3s cooldown) -> accepted
    transcriber = FakeTranscriber(
        scripted=[
            "hey orbit first command",
            "hey orbit second command too soon",
            "hey orbit third command allowed",
        ]
    )

    detector = WakeDetector(
        transcriber=transcriber,
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
        cooldown_s=3.0,
        clock=mock_clock,
        sleep=sleeper.sleep,
    )

    # First trigger at t=100.0
    res1 = detector.wait_for_wake()
    assert isinstance(res1, WakeResult)
    assert res1.remainder == "first command"
    assert detector.last_trigger_time == 100.0

    # Second wait_for_wake(): processes second phrase at t=101.5 (ignored due to cooldown),
    # continues in loop and processes third phrase at t=104.0 (accepted)
    res2 = detector.wait_for_wake()
    assert isinstance(res2, WakeResult)
    assert res2.remainder == "third command allowed"
    assert detector.last_trigger_time == 104.0


def test_is_muted_skips_capturing():
    """When is_muted() is True, capture_fn is not called and sleep(0.1) is executed."""
    sleeper = MockSleeper()
    cancel_event = threading.Event()
    capture_calls = 0

    def capture(**kwargs):
        nonlocal capture_calls
        capture_calls += 1
        return make_dummy_audio()

    muted_state = [True, True, False]

    def is_muted():
        if muted_state:
            return muted_state.pop(0)
        cancel_event.set()
        return False

    transcriber = FakeTranscriber(scripted=["hey orbit unmute worked"])
    detector = WakeDetector(
        transcriber=transcriber,
        make_segmenter=lambda: None,
        capture_fn=capture,
        phrase="hey orbit",
        is_muted=is_muted,
        sleep=sleeper.sleep,
    )

    result = detector.wait_for_wake(cancel_event=cancel_event)
    assert isinstance(result, WakeResult)
    assert result.remainder == "unmute worked"
    # While muted twice, sleep(0.1) was called twice
    assert sleeper.sleeps.count(0.1) == 2
    # capture was called only once after unmuting
    assert capture_calls == 1


def test_transcription_error_reported_and_loop_continues():
    """TranscriptionError is reported through on_error, sleeps 1s, and loop continues."""
    sleeper = MockSleeper()
    errors_reported: list[str] = []

    transcriber = FakeTranscriber(
        scripted=[
            TranscriptionError("Model decoding failed"),
            "hey orbit continue after error",
        ]
    )

    detector = WakeDetector(
        transcriber=transcriber,
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
        sleep=sleeper.sleep,
        on_error=errors_reported.append,
    )

    result = detector.wait_for_wake()
    assert isinstance(result, WakeResult)
    assert result.remainder == "continue after error"
    assert len(errors_reported) == 1
    assert "Model decoding failed" in errors_reported[0]
    assert 1.0 in sleeper.sleeps


def test_audio_error_reported_and_loop_continues():
    """AudioError during capture is reported, sleeps 1s, and continues."""
    sleeper = MockSleeper()
    errors_reported: list[str] = []
    capture_count = 0

    def capture_flaky(**kwargs):
        nonlocal capture_count
        capture_count += 1
        if capture_count == 1:
            raise AudioError("Microphone disconnected")
        return make_dummy_audio()

    transcriber = FakeTranscriber(scripted=["hey orbit recovered"])
    detector = WakeDetector(
        transcriber=transcriber,
        make_segmenter=lambda: None,
        capture_fn=capture_flaky,
        phrase="hey orbit",
        sleep=sleeper.sleep,
        on_error=errors_reported.append,
    )

    result = detector.wait_for_wake()
    assert isinstance(result, WakeResult)
    assert result.remainder == "recovered"
    assert len(errors_reported) == 1
    assert "Microphone disconnected" in errors_reported[0]
    assert 1.0 in sleeper.sleeps


def test_cancel_event_stops_loop():
    """cancel_event immediately stops wait_for_wake and returns None."""
    cancel_event = threading.Event()
    cancel_event.set()

    detector = WakeDetector(
        transcriber=FakeTranscriber(),
        make_segmenter=lambda: None,
        capture_fn=lambda **kwargs: make_dummy_audio(),
        phrase="hey orbit",
    )

    result = detector.wait_for_wake(cancel_event=cancel_event)
    assert result is None
