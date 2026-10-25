"""Audio capture and analysis module using sounddevice."""

import threading
from typing import Any, Callable, Optional, Union
import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None


class AudioError(Exception):
    """Raised when an audio recording, device, or stream operation fails."""


def list_input_devices() -> list[tuple[int, str]]:
    """Query available audio input devices.

    Returns a list of (device_index, device_name) tuples, or [] if query fails.
    """
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        input_devices: list[tuple[int, str]] = []
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) > 0:
                name = dev.get("name", f"Device {idx}")
                input_devices.append((idx, name))
        return input_devices
    except Exception:
        return []


def audio_stats(audio: np.ndarray, sample_rate: int = 16000) -> tuple[float, float]:
    """Compute duration in seconds and RMS loudness for a 1-D audio array.

    Returns (duration_seconds, rms_loudness).
    """
    if audio is None or len(audio) == 0:
        return 0.0, 0.0

    duration_s = float(len(audio)) / float(sample_rate)
    audio_float = np.asarray(audio, dtype=np.float32)
    rms = float(np.sqrt(np.mean(np.square(audio_float))))
    return round(duration_s, 2), float(rms)


def is_silent(audio: np.ndarray, rms_threshold: float = 0.005) -> bool:
    """Check if the audio array has an RMS loudness below the silence threshold."""
    if audio is None or len(audio) == 0:
        return True
    _, rms = audio_stats(audio)
    return rms < rms_threshold


class Recorder:
    """Microphone audio recorder using sounddevice with callback streaming."""

    def __init__(
        self,
        device: Optional[Union[int, str]] = None,
        sample_rate: int = 16000,
        max_seconds: int = 30,
        stream_factory: Optional[Callable[..., Any]] = None,
        level_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.max_seconds = max_seconds
        self.stream_factory = stream_factory
        self.level_callback = level_callback

    def record(self, stop_event: Optional[threading.Event] = None) -> np.ndarray:
        """Record audio until stop_event is set or max_seconds elapses.

        Returns a 1-D numpy float32 mono array normalized to [-1, 1].
        """
        recorded_blocks: list[np.ndarray] = []
        callback_error: list[Exception] = []

        def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            try:
                # Copy block to prevent sounddevice buffer reuse
                recorded_blocks.append(indata.copy())
                if self.level_callback is not None:
                    try:
                        chunk = np.asarray(indata, dtype=np.float32)
                        rms = float(np.sqrt(np.mean(np.square(chunk))))
                        self.level_callback(rms)
                    except Exception:
                        pass
            except Exception as exc:
                callback_error.append(exc)

        # Resolve stream constructor
        if self.stream_factory is not None:
            factory = self.stream_factory
        else:
            if sd is None:
                raise AudioError(
                    "sounddevice is not installed. Please install it with 'pip install sounddevice'."
                )
            try:
                input_devs = list_input_devices()
                if not input_devs and self.device is None:
                    raise AudioError("No microphone or audio input device found.")
                factory = sd.InputStream
            except Exception as exc:
                raise AudioError(f"Microphone device query failed: {exc}") from exc

        try:
            stream = factory(
                samplerate=self.sample_rate,
                device=self.device,
                channels=1,
                dtype="float32",
                callback=callback,
            )
        except Exception as exc:
            raise AudioError(
                f"Failed to open audio input stream on device {self.device}: {exc}"
            ) from exc

        try:
            if hasattr(stream, "start"):
                stream.start()

            event = stop_event or threading.Event()
            event.wait(timeout=float(self.max_seconds))

            if callback_error:
                raise AudioError(f"Audio callback failed: {callback_error[0]}")
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

        if not recorded_blocks:
            return np.zeros(0, dtype=np.float32)

        audio_data = np.concatenate(recorded_blocks, axis=0)
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()
        return audio_data.astype(np.float32)


class FakeRecorder:
    """Mock audio recorder returning predefined numpy array or raising an error."""

    def __init__(
        self,
        audio: Optional[np.ndarray] = None,
        error: Optional[Exception] = None,
        level_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        if audio is not None:
            self.audio = audio
        else:
            t = np.linspace(0, 1.0, 16000, endpoint=False, dtype=np.float32)
            self.audio = 0.5 * np.sin(2 * np.pi * 440 * t)
        self.error = error
        self.level_callback = level_callback
        self.calls: int = 0

    def record(self, stop_event: Optional[threading.Event] = None) -> np.ndarray:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.level_callback is not None:
            self.level_callback(0.2)
        return self.audio
