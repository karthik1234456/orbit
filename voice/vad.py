"""Voice Activity Detection (VAD) and utterance segmentation module."""

import queue
import threading
import time
from typing import Any, Callable, Optional
import numpy as np

from config.settings import MIC_DEVICE
from voice.audio import AudioError, list_input_devices

try:
    import sounddevice as sd
except ImportError:
    sd = None


def frame_rms(frame: np.ndarray) -> float:
    """Calculate the Root Mean Square (RMS) energy of an audio frame."""
    if frame is None or len(frame) == 0:
        return 0.0
    arr = np.asarray(frame, dtype=np.float32)
    return float(np.sqrt(np.mean(np.square(arr))))


class UtteranceSegmenter:
    """Pure state machine that segments speech from a continuous audio stream."""

    def __init__(
        self,
        threshold: float,
        silence_seconds: float,
        sample_rate: int = 16000,
        preroll_seconds: float = 0.3,
        max_seconds: float = 30.0,
        min_speech_seconds: float = 0.25,
    ) -> None:
        self.threshold = float(threshold)
        self.silence_seconds = float(silence_seconds)
        self.sample_rate = int(sample_rate)
        self.preroll_seconds = float(preroll_seconds)
        self.max_seconds = float(max_seconds)
        self.min_speech_seconds = float(min_speech_seconds)

        self.preroll_samples = int(self.preroll_seconds * self.sample_rate)
        self.silence_samples = int(self.silence_seconds * self.sample_rate)
        self.max_samples = int(self.max_seconds * self.sample_rate)
        self.min_speech_samples = int(self.min_speech_seconds * self.sample_rate)
        self.tail_samples = int(0.2 * self.sample_rate)

        self.state: str = "idle"  # "idle" | "speaking" | "done"
        self._preroll_buffer: list[np.ndarray] = []
        self._preroll_count: int = 0
        self._utterance_frames: list[np.ndarray] = []
        self._utterance_count: int = 0
        self._quiet_samples: int = 0
        self._speech_samples: int = 0
        self._finished_audio: Optional[np.ndarray] = None

    def reset(self) -> None:
        """Clear all buffers and reset state to idle."""
        self.state = "idle"
        self._preroll_buffer.clear()
        self._preroll_count = 0
        self._utterance_frames.clear()
        self._utterance_count = 0
        self._quiet_samples = 0
        self._speech_samples = 0
        self._finished_audio = None

    def feed(self, frame: np.ndarray) -> str:
        """Process an incoming audio frame and transition state.

        Returns:
            "idle": waiting for speech or discarded noise.
            "speaking": speech is currently being recorded.
            "done": an utterance has successfully finished.
        """
        if self.state == "done":
            return "done"

        flat = np.asarray(frame, dtype=np.float32).flatten()
        n = len(flat)
        if n == 0:
            return self.state

        rms = frame_rms(flat)
        is_loud = rms >= self.threshold

        if self.state == "idle":
            if not is_loud:
                # Maintain rolling preroll buffer
                self._preroll_buffer.append(flat)
                self._preroll_count += n
                while self._preroll_buffer and (self._preroll_count - len(self._preroll_buffer[0]) >= self.preroll_samples):
                    self._preroll_count -= len(self._preroll_buffer.pop(0))
                return "idle"

            # Speech detected: transition to speaking and prepend preroll
            self.state = "speaking"
            self._utterance_frames = list(self._preroll_buffer)
            self._utterance_count = self._preroll_count
            self._preroll_buffer.clear()
            self._preroll_count = 0

            self._utterance_frames.append(flat)
            self._utterance_count += n
            self._speech_samples = n
            self._quiet_samples = 0

            if self._utterance_count >= self.max_samples:
                return self._finalize()
            return "speaking"

        if self.state == "speaking":
            self._utterance_frames.append(flat)
            self._utterance_count += n

            if is_loud:
                self._speech_samples += n
                self._quiet_samples = 0
            else:
                self._quiet_samples += n

            # Check termination criteria
            silence_met = self._quiet_samples >= self.silence_samples
            max_met = self._utterance_count >= self.max_samples

            if silence_met or max_met:
                return self._finalize()

            return "speaking"

        return self.state

    def _finalize(self) -> str:
        """Evaluate completed utterance: discard if too brief, or trim and store."""
        if self._speech_samples < self.min_speech_samples:
            # Discard clicks, taps, or brief coughs and return to idle
            self.reset()
            return "idle"

        full = np.concatenate(self._utterance_frames, axis=0) if self._utterance_frames else np.zeros(0, dtype=np.float32)

        # Trim trailing silence to about 0.2s if ended on silence
        if self._quiet_samples > self.tail_samples:
            excess = self._quiet_samples - self.tail_samples
            if excess > 0 and len(full) > excess:
                full = full[:-excess]

        # Enforce max_samples hard cap
        if len(full) > self.max_samples:
            full = full[:self.max_samples]

        self._finished_audio = full.astype(np.float32)
        self.state = "done"
        return "done"

    def get_audio(self) -> np.ndarray:
        """Return the completed utterance audio as a 1-D float32 numpy array."""
        if self._finished_audio is not None:
            return self._finished_audio
        return np.zeros(0, dtype=np.float32)


def capture_utterance(
    stream_factory: Optional[Callable[..., Any]],
    segmenter: UtteranceSegmenter,
    start_timeout: Optional[float] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Optional[np.ndarray]:
    """Capture a single speech utterance using audio stream callbacks and the segmenter.

    Returns the float32 audio array if speech is completed, or None if start_timeout
    expires without speech or cancel_event is set.
    """
    segmenter.reset()
    frame_queue: queue.Queue = queue.Queue()

    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        frame_queue.put(indata.copy())

    # Resolve stream factory
    if stream_factory is not None:
        factory = stream_factory
    else:
        if sd is None:
            raise AudioError("sounddevice is not installed. Please install it with 'pip install sounddevice'.")
        try:
            devs = list_input_devices()
            if not devs and MIC_DEVICE is None:
                raise AudioError("No microphone or audio input device found.")
            factory = sd.InputStream
        except Exception as exc:
            raise AudioError(f"Microphone initialization failed: {exc}") from exc

    try:
        stream = factory(
            samplerate=segmenter.sample_rate,
            channels=1,
            dtype="float32",
            callback=callback,
        )
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(f"Failed to open audio input stream: {exc}") from exc

    try:
        if hasattr(stream, "start"):
            stream.start()

        start_time = time.monotonic()
        speech_started = False

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None

            if start_timeout is not None and not speech_started:
                if (time.monotonic() - start_time) >= start_timeout:
                    return None

            try:
                frame = frame_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            status = segmenter.feed(frame)
            if status == "speaking":
                speech_started = True
            elif status == "done":
                return segmenter.get_audio()

    finally:
        if hasattr(stream, "stop"):
            try:
                stream.stop()
            except Exception:
                pass
        if hasattr(stream, "close"):
            try:
                stream.close()
            except Exception:
                pass
