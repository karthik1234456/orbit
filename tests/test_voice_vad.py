"""Tests for voice/vad.py: VAD energy calculation, UtteranceSegmenter state machine, and capture_utterance."""

import threading
import time
import numpy as np
import pytest

from voice.vad import UtteranceSegmenter, capture_utterance, frame_rms


def make_silence_frame(samples: int = 1600) -> np.ndarray:
    """Return a silent frame (0.1s at 16kHz)."""
    return np.zeros(samples, dtype=np.float32)


def make_speech_frame(samples: int = 1600, amplitude: float = 0.3) -> np.ndarray:
    """Return a synthetic speech frame (sine wave at 400Hz)."""
    t = np.linspace(0, samples / 16000, samples, endpoint=False, dtype=np.float32)
    return (amplitude * np.sin(2 * np.pi * 400 * t)).astype(np.float32)


class MockAudioStream:
    """Mock sounddevice stream feeding scripted frames to the callback."""

    def __init__(self, callback, frames_to_feed: list[np.ndarray], delay: float = 0.01, **kwargs):
        self.callback = callback
        self.frames = list(frames_to_feed)
        self.delay = delay
        self.closed = False
        self.started = False
        self._thread: threading.Thread = None
        self._running = False

    def start(self) -> None:
        self.started = True
        self._running = True

        def feed_worker():
            for frame in self.frames:
                if not self._running:
                    break
                self.callback(frame, len(frame), None, None)
                time.sleep(self.delay)

        self._thread = threading.Thread(target=feed_worker)
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        self._running = False
        self.closed = True


def test_frame_rms():
    """Verify frame RMS loudness calculation."""
    assert frame_rms(np.zeros(1000, dtype=np.float32)) == 0.0
    assert frame_rms(np.array([], dtype=np.float32)) == 0.0

    # Sine with amplitude 0.2 has RMS ~ 0.2 / sqrt(2) ~ 0.1414
    speech = make_speech_frame(amplitude=0.2)
    rms = frame_rms(speech)
    assert 0.13 < rms < 0.15


def test_silence_only_never_completes():
    """Feeding silence only keeps the segmenter in idle state and never finishes."""
    segmenter = UtteranceSegmenter(threshold=0.01, silence_seconds=0.8, sample_rate=16000)

    for _ in range(25):  # 2.5s of silence
        status = segmenter.feed(make_silence_frame())
        assert status == "idle"

    assert segmenter.state == "idle"
    assert len(segmenter.get_audio()) == 0


def test_speech_then_silence_completes_and_includes_preroll():
    """Speech followed by silence transitions to done and includes preroll audio."""
    segmenter = UtteranceSegmenter(
        threshold=0.01,
        silence_seconds=0.5,
        preroll_seconds=0.3,
        sample_rate=16000,
    )

    # 1. Feed 4 frames of silence (0.4s > 0.3s preroll)
    for _ in range(4):
        assert segmenter.feed(make_silence_frame()) == "idle"

    # 2. Feed 5 frames of speech (0.5s speech)
    for _ in range(5):
        assert segmenter.feed(make_speech_frame()) == "speaking"

    # 3. Feed silence to trigger completion (5 frames = 0.5s silence)
    final_status = None
    for _ in range(5):
        final_status = segmenter.feed(make_silence_frame())

    assert final_status == "done"
    audio = segmenter.get_audio()
    assert len(audio) > 0

    # Audio must include preroll (about 0.3s) + speech (0.5s) + trimmed tail (0.2s) ~ 1.0s (16000 samples)
    dur_s = len(audio) / 16000.0
    assert 0.8 <= dur_s <= 1.2


def test_trailing_silence_is_trimmed():
    """Trailing silence exceeding 0.2s is trimmed from the finished audio."""
    segmenter = UtteranceSegmenter(
        threshold=0.01,
        silence_seconds=0.5,
        sample_rate=16000,
    )

    # Speech 0.3s
    for _ in range(3):
        segmenter.feed(make_speech_frame())

    # 15 frames of silence (1.5s of silence)
    for _ in range(15):
        status = segmenter.feed(make_silence_frame())
        if status == "done":
            break

    audio = segmenter.get_audio()
    # If 1.5s silence wasn't trimmed, total would be >= 1.8s (28800 samples)
    # Trimmed tail of ~0.2s gives ~0.5s (8000 samples)
    dur_s = len(audio) / 16000.0
    assert dur_s < 0.9


def test_short_click_is_discarded():
    """A brief sound below min_speech_seconds is discarded as noise and returns to idle."""
    segmenter = UtteranceSegmenter(
        threshold=0.01,
        silence_seconds=0.4,
        min_speech_seconds=0.25,
        sample_rate=16000,
    )

    # 1 frame click of 0.05s (800 samples < 4000 min_speech_samples)
    click = make_speech_frame(samples=800)
    assert segmenter.feed(click) == "speaking"

    # Follow with 5 frames silence (0.5s)
    status = None
    for _ in range(5):
        status = segmenter.feed(make_silence_frame())

    # Should discard click and return to idle
    assert status == "idle"
    assert segmenter.state == "idle"
    assert len(segmenter.get_audio()) == 0


def test_max_seconds_cuts_off_never_ending_sound():
    """Sound continuing up to max_seconds triggers completion."""
    segmenter = UtteranceSegmenter(
        threshold=0.01,
        silence_seconds=0.5,
        max_seconds=1.0,
        sample_rate=16000,
    )

    status = None
    # Feed 15 speech frames (1.5s > 1.0s max)
    for _ in range(15):
        status = segmenter.feed(make_speech_frame())
        if status == "done":
            break

    assert status == "done"
    audio = segmenter.get_audio()
    # Length capped at max_seconds (16000 samples)
    assert len(audio) <= 16000


def test_threshold_is_respected():
    """Audio below the threshold is treated as silence; audio above triggers speaking."""
    segmenter = UtteranceSegmenter(threshold=0.02, silence_seconds=0.8, sample_rate=16000)

    # Low amplitude frame (RMS ~ 0.007 < 0.02)
    quiet_frame = make_speech_frame(amplitude=0.01)
    assert segmenter.feed(quiet_frame) == "idle"

    # Loud amplitude frame (RMS ~ 0.07 > 0.02)
    loud_frame = make_speech_frame(amplitude=0.1)
    assert segmenter.feed(loud_frame) == "speaking"


def test_capture_utterance_start_timeout_returns_none():
    """capture_utterance returns None when start_timeout expires before speech begins."""
    # Feed only silence
    silence_frames = [make_silence_frame() for _ in range(10)]
    stream_instance = None

    def factory(**kwargs):
        nonlocal stream_instance
        stream_instance = MockAudioStream(frames_to_feed=silence_frames, delay=0.02, **kwargs)
        return stream_instance

    segmenter = UtteranceSegmenter(threshold=0.01, silence_seconds=0.5)
    result = capture_utterance(
        stream_factory=factory,
        segmenter=segmenter,
        start_timeout=0.1,
    )

    assert result is None
    assert stream_instance is not None
    assert stream_instance.closed is True


def test_capture_utterance_cancel_event_returns_none():
    """capture_utterance returns None when cancel_event is set."""
    silence_frames = [make_silence_frame() for _ in range(10)]
    stream_instance = None

    def factory(**kwargs):
        nonlocal stream_instance
        stream_instance = MockAudioStream(frames_to_feed=silence_frames, delay=0.02, **kwargs)
        return stream_instance

    cancel_event = threading.Event()
    # Pre-set cancel event
    cancel_event.set()

    segmenter = UtteranceSegmenter(threshold=0.01, silence_seconds=0.5)
    result = capture_utterance(
        stream_factory=factory,
        segmenter=segmenter,
        cancel_event=cancel_event,
    )

    assert result is None
    assert stream_instance is not None
    assert stream_instance.closed is True


def test_capture_utterance_stream_closed_on_exception():
    """Stream is closed even if an exception occurs during streaming."""
    stream_instance = None

    class RaisingStream(MockAudioStream):
        def start(self):
            super().start()
            raise RuntimeError("Stream buffer failure")

    def factory(**kwargs):
        nonlocal stream_instance
        stream_instance = RaisingStream(callback=kwargs["callback"], frames_to_feed=[])
        return stream_instance

    segmenter = UtteranceSegmenter(threshold=0.01, silence_seconds=0.5)

    with pytest.raises(RuntimeError):
        capture_utterance(stream_factory=factory, segmenter=segmenter)

    assert stream_instance is not None
    assert stream_instance.closed is True
