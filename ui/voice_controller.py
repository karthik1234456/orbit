"""Voice UI controller for click-to-talk audio input and text-to-speech output.

This controller handles background audio recording, level metering, speech-to-text
transcription, and optional text-to-speech reply vocalization without any direct
GUI/tkinter dependencies.
"""

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Optional
import numpy as np

from config import settings
from voice.audio import AudioError, audio_stats, is_silent
from voice.stt import TranscriptionError, filter_hallucinations
from voice.text import clean_for_speech


@dataclass
class VoiceState:
    """Possible states for the voice controller."""

    IDLE: str = "idle"
    LISTENING: str = "listening"
    TRANSCRIBING: str = "transcribing"
    SPEAKING: str = "speaking"
    ERROR: str = "error"


class VoiceUiController:
    """Coordinates microphone recording, Whisper transcription, and TTS speech in the UI."""

    def __init__(
        self,
        recorder_factory: Optional[Callable[[], Any]] = None,
        transcriber_factory: Optional[Callable[[], Any]] = None,
        speaker: Optional[Any] = None,
        emit: Optional[Callable[[str, dict], None]] = None,
        is_blocked: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.recorder_factory = recorder_factory
        self.transcriber_factory = transcriber_factory
        self.speaker = speaker
        self._emit_fn = emit
        self._is_blocked_fn = is_blocked

        self._state: str = VoiceState.IDLE
        self.last_error: str = ""
        self.last_transcription_duration_s: float = 0.0
        self.speak_replies: bool = bool(getattr(settings, "SPEAK_REPLIES", False))

        self._lock = threading.Lock()
        self._stop_record_event: Optional[threading.Event] = None
        self._cancel_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._transcriber: Optional[Any] = None
        self._last_level_time: float = 0.0
        self._shutdown: bool = False

    @property
    def state(self) -> str:
        """Current operational state of the voice controller."""
        with self._lock:
            return self._state

    def _set_state(self, new_state: str, detail: str = "") -> None:
        """Update internal state and emit state change event."""
        with self._lock:
            self._state = new_state
        data = {"state": new_state}
        if detail:
            data["detail"] = detail
            data["status"] = detail
        self._emit("voice_state", data)

    def _emit(self, kind: str, data: dict) -> None:
        """Emit an event via the wired emit callback."""
        if self._emit_fn is not None:
            try:
                self._emit_fn(kind, data)
            except Exception:
                pass

    def is_blocked(self) -> bool:
        """Return True if voice input is currently blocked."""
        if self._is_blocked_fn is not None:
            try:
                if self._is_blocked_fn():
                    return True
            except Exception:
                pass
        with self._lock:
            if self._state in (VoiceState.LISTENING, VoiceState.TRANSCRIBING, VoiceState.SPEAKING):
                return True
        if self.speaker is not None:
            try:
                if self.speaker.is_speaking():
                    return True
            except Exception:
                pass
        return False

    def is_speaking(self) -> bool:
        """Return True if TTS playback is active."""
        if self.speaker is not None:
            try:
                return bool(self.speaker.is_speaking())
            except Exception:
                return False
        return False

    def start_listening(self) -> bool:
        """Begin audio capture on a dedicated background worker thread.

        Refuses if shutting down, if already recording/transcribing, or if blocked
        by an active task, dialog, or active speech.
        """
        if self._shutdown:
            return False

        if self._is_blocked_fn is not None:
            try:
                if self._is_blocked_fn():
                    msg = "Cannot listen while ORBIT is busy, waiting for approval, or speaking."
                    self._emit("voice_error", {"error": msg, "message": msg})
                    return False
            except Exception:
                pass

        with self._lock:
            if self._state != VoiceState.IDLE:
                return False
            self._state = VoiceState.LISTENING

        # Stop any active TTS speech before listening
        self.stop_speaking()

        self._stop_record_event = threading.Event()
        self._cancel_event.clear()
        self._last_level_time = 0.0

        self._emit("voice_state", {"state": VoiceState.LISTENING})

        worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="VoiceUiWorker",
        )
        self._worker_thread = worker
        worker.start()
        return True

    def stop_listening(self) -> bool:
        """Signal the recording thread to finish and begin transcription."""
        with self._lock:
            if self._state != VoiceState.LISTENING:
                return False
        if self._stop_record_event is not None:
            self._stop_record_event.set()
        return True

    def _worker_loop(self) -> None:
        """Worker thread executing recording and subsequent transcription."""
        audio: Optional[np.ndarray] = None

        def on_level(rms_val: float) -> None:
            if self._cancel_event.is_set():
                return
            now = time.monotonic()
            if now - self._last_level_time >= 0.1:  # at most 10 events per second
                self._last_level_time = now
                clamped = max(0.0, min(1.0, float(rms_val)))
                self._emit("voice_level", {"level": clamped})

        try:
            # 1. Obtain recorder
            if self.recorder_factory is not None:
                recorder = self.recorder_factory()
            else:
                from voice.audio import Recorder

                recorder = Recorder(
                    device=getattr(settings, "MIC_DEVICE", None),
                    max_seconds=int(getattr(settings, "MAX_RECORD_SECONDS", 30)),
                )

            # Wire level callback
            if hasattr(recorder, "level_callback"):
                recorder.level_callback = on_level
            elif getattr(recorder, "stream_factory", None) is not None:
                base_factory = recorder.stream_factory

                def metered_factory(*args: Any, **kwargs: Any) -> Any:
                    cb = kwargs.get("callback")

                    def wrapped_cb(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
                        if cb:
                            cb(indata, frames, time_info, status)
                        try:
                            chunk = np.asarray(indata, dtype=np.float32)
                            on_level(float(np.sqrt(np.mean(np.square(chunk)))))
                        except Exception:
                            pass

                    kwargs["callback"] = wrapped_cb
                    return base_factory(*args, **kwargs)

                recorder.stream_factory = metered_factory

            # 2. Record
            audio = recorder.record(stop_event=self._stop_record_event)

        except AudioError as exc:
            msg = str(exc)
            with self._lock:
                self._state = VoiceState.ERROR
                self.last_error = msg
            self._emit("voice_level", {"level": 0.0})
            self._emit("voice_error", {"error": msg, "message": msg})
            self._set_state(VoiceState.IDLE)
            return
        except Exception as exc:
            msg = f"Audio capture error: {exc}"
            with self._lock:
                self._state = VoiceState.ERROR
                self.last_error = msg
            self._emit("voice_level", {"level": 0.0})
            self._emit("voice_error", {"error": msg, "message": msg})
            self._set_state(VoiceState.IDLE)
            return
        finally:
            self._emit("voice_level", {"level": 0.0})

        # Check for cancellation during recording
        if self._cancel_event.is_set():
            audio = None
            self._set_state(VoiceState.IDLE)
            return

        # 3. Validate audio duration and silence
        duration_s, _ = audio_stats(audio)
        if duration_s < 0.3 or is_silent(audio):
            audio = None
            msg = "I didn't hear anything."
            with self._lock:
                self.last_error = msg
            self._emit("voice_error", {"error": msg, "message": msg})
            self._set_state(VoiceState.IDLE)
            return

        # 4. Transcribe audio
        is_first_load = self._transcriber is None
        if is_first_load:
            self._set_state(VoiceState.TRANSCRIBING, detail="Loading speech model...")
        else:
            self._set_state(VoiceState.TRANSCRIBING)

        try:
            if self._transcriber is None:
                if self.transcriber_factory is not None:
                    self._transcriber = self.transcriber_factory()
                else:
                    from voice.stt import WhisperTranscriber

                    self._transcriber = WhisperTranscriber(
                        model_name=getattr(settings, "STT_MODEL", "base.en"),
                        language=getattr(settings, "STT_LANGUAGE", "en"),
                    )

            if self._cancel_event.is_set():
                audio = None
                self._set_state(VoiceState.IDLE)
                return

            t0 = time.monotonic()
            transcript_obj = self._transcriber.transcribe(audio)
            t1 = time.monotonic()
            self.last_transcription_duration_s = round(t1 - t0, 2)

            raw_text = getattr(transcript_obj, "text", str(transcript_obj))
        except TranscriptionError as exc:
            err_str = str(exc)
            if "download" in err_str.lower() or "offline" in err_str.lower() or "not found locally" in err_str.lower():
                friendly = "The speech model has to be downloaded once while online before running offline."
            else:
                friendly = f"Speech transcription failed: {err_str}"
            with self._lock:
                self._state = VoiceState.ERROR
                self.last_error = friendly
            self._emit("voice_error", {"error": friendly, "message": friendly})
            self._set_state(VoiceState.IDLE)
            return
        except Exception as exc:
            friendly = f"Transcription error: {exc}"
            with self._lock:
                self._state = VoiceState.ERROR
                self.last_error = friendly
            self._emit("voice_error", {"error": friendly, "message": friendly})
            self._set_state(VoiceState.IDLE)
            return
        finally:
            # Memory and privacy safety: drop raw audio array reference immediately
            audio = None

        if self._cancel_event.is_set():
            self._set_state(VoiceState.IDLE)
            return

        # 5. Hallucination filter and emit transcript
        clean_transcript = filter_hallucinations(raw_text).strip()
        self._set_state(VoiceState.IDLE)

        if clean_transcript:
            self._emit(
                "voice_transcript",
                {
                    "text": clean_transcript,
                    "transcript": clean_transcript,
                    "source": "voice",
                },
            )

    def speak_reply(self, text: str) -> None:
        """Speak the final assistant reply if speak_replies is enabled.

        Sanitizes text, caps to 500 characters, and removes page-derived titles
        following 'Opened result' to protect against untrusted webpage text.
        """
        if not self.speak_replies or not text:
            return

        # Security/privacy: strip untrusted page titles after 'Opened result'
        clean_text = text
        lower_text = text.lower()
        if "opened result" in lower_text:
            idx = lower_text.find("opened result")
            prefix = text[:idx].strip()
            clean_text = prefix if prefix else "Opened result."

        spoken = clean_for_speech(clean_text, max_chars=500)
        if not spoken or not spoken.strip():
            return

        if self.speaker is None:
            from voice.tts import SubprocessSpeaker

            self.speaker = SubprocessSpeaker()

        try:
            self.speaker.speak(spoken, blocking=False)
            self._set_state(VoiceState.SPEAKING)
        except Exception as exc:
            friendly = f"Speech output failed: {exc}"
            with self._lock:
                self._state = VoiceState.ERROR
                self.last_error = friendly
            self._emit("voice_error", {"error": friendly, "message": friendly})
            self._set_state(VoiceState.IDLE)

    def stop_speaking(self) -> None:
        """Immediately terminate active TTS playback."""
        if self.speaker is not None:
            try:
                self.speaker.stop()
            except Exception:
                pass
        with self._lock:
            if self._state == VoiceState.SPEAKING:
                self._state = VoiceState.IDLE
                self._emit("voice_state", {"state": VoiceState.IDLE})

    def poll_speaker(self) -> None:
        """Poll speaker state from UI timer to update speaking state."""
        with self._lock:
            curr_state = self._state

        if curr_state == VoiceState.SPEAKING:
            if self.speaker is None or not self.speaker.is_speaking():
                self._set_state(VoiceState.IDLE)
        elif self.speaker is not None and self.speaker.is_speaking():
            if curr_state == VoiceState.IDLE:
                self._set_state(VoiceState.SPEAKING)

    def cancel(self) -> None:
        """Cancel ongoing recording, transcription, or speech."""
        self._cancel_event.set()
        if self._stop_record_event is not None:
            self._stop_record_event.set()
        self.stop_speaking()
        with self._lock:
            if self._state in (VoiceState.LISTENING, VoiceState.TRANSCRIBING, VoiceState.SPEAKING):
                self._state = VoiceState.IDLE
                self._emit("voice_state", {"state": VoiceState.IDLE})
        self._emit("voice_level", {"level": 0.0})

    def shutdown(self) -> None:
        """Idempotently cancel and clean up threads."""
        if self._shutdown:
            return
        self._shutdown = True
        self.cancel()
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        self.stop_speaking()
