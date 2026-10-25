"""Tests for voice/audio.py: silence detection, audio stats, and Recorder with fake stream factories."""

import threading
import numpy as np
import pytest

from voice.audio import AudioError, Recorder, audio_stats, is_silent, list_input_devices


def test_audio_stats_and_is_silent():
    """Verify silence detection and loudness stats on synthetic audio arrays."""
    # 1. Empty array
    dur, rms = audio_stats(np.array([], dtype=np.float32))
    assert dur == 0.0
    assert rms == 0.0
    assert is_silent(np.array([], dtype=np.float32)) is True

    # 2. Pure silence (zeros) for 1 second at 16kHz
    zeros = np.zeros(16000, dtype=np.float32)
    dur, rms = audio_stats(zeros, sample_rate=16000)
    assert dur == 1.0
    assert rms == 0.0
    assert is_silent(zeros) is True

    # 3. Sine wave with amplitude 0.5 (theoretical RMS ~ 0.353)
    t = np.linspace(0, 1, 16000, endpoint=False, dtype=np.float32)
    sine = 0.5 * np.sin(2 * np.pi * 440 * t)
    dur, rms = audio_stats(sine, sample_rate=16000)
    assert dur == 1.0
    assert 0.3 < rms < 0.4
    assert is_silent(sine, rms_threshold=0.005) is False


def test_list_input_devices_fallback():
    """list_input_devices returns a list (empty if no sounddevice or audio drivers)."""
    devices = list_input_devices()
    assert isinstance(devices, list)


class FakeInputStream:
    """Mock sounddevice.InputStream for testing without audio hardware."""

    def __init__(self, callback, **kwargs):
        self.callback = callback
        self.closed = False
        self.started = False

    def start(self):
        self.started = True
        # Deliver two blocks of 8000 samples each (total 1.0s at 16kHz)
        block1 = np.ones((8000, 1), dtype=np.float32) * 0.1
        block2 = np.ones((8000, 1), dtype=np.float32) * 0.2
        self.callback(block1, 8000, None, None)
        self.callback(block2, 8000, None, None)

    def stop(self):
        pass

    def close(self):
        self.closed = True


def test_recorder_with_fake_stream_and_stop_event():
    """Recorder captures blocks from stream_factory and closes the stream when stop_event fires."""
    created_stream = None

    def factory(**kwargs):
        nonlocal created_stream
        created_stream = FakeInputStream(**kwargs)
        return created_stream

    stop_event = threading.Event()
    # Trigger stop event shortly after start
    threading.Timer(0.05, stop_event.set).start()

    recorder = Recorder(stream_factory=factory, max_seconds=10)
    audio = recorder.record(stop_event=stop_event)

    assert len(audio) == 16000
    assert audio.dtype == np.float32
    assert created_stream is not None
    assert created_stream.started is True
    assert created_stream.closed is True


def test_recorder_closes_stream_when_wait_raises():
    """Stream is guaranteed to close even if an error is raised during streaming."""
    created_stream = None

    class RaisingStream(FakeInputStream):
        def start(self):
            super().start()
            raise RuntimeError("Hardware disconnect")

    def factory(**kwargs):
        nonlocal created_stream
        created_stream = RaisingStream(**kwargs)
        return created_stream

    recorder = Recorder(stream_factory=factory, max_seconds=1)
    with pytest.raises(RuntimeError):
        recorder.record()

    assert created_stream is not None
    assert created_stream.closed is True


def test_recorder_closes_stream_when_callback_fails():
    """Stream is closed and AudioError is raised when callback encounters an error."""
    created_stream = None

    class FaultyStream(FakeInputStream):
        def start(self):
            # Pass invalid data type that causes copy() to fail
            class BrokenData:
                def copy(self):
                    raise ValueError("Corrupt memory buffer")
            self.callback(BrokenData(), 100, None, None)

    def factory(**kwargs):
        nonlocal created_stream
        created_stream = FaultyStream(**kwargs)
        return created_stream

    recorder = Recorder(stream_factory=factory, max_seconds=0.05)
    with pytest.raises(AudioError) as exc_info:
        recorder.record()

    assert "Audio callback failed" in str(exc_info.value)
    assert created_stream is not None
    assert created_stream.closed is True


def test_recorder_no_microphone_raises_audio_error():
    """Recorder without hardware stream_factory raises AudioError when sounddevice is missing."""
    recorder = Recorder(stream_factory=None)
    with pytest.raises(AudioError):
        recorder.record()
