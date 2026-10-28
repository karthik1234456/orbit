"""Voice UI controller managing microphone capture, transcription, and spoken replies for desktop window.

NOTE: This module must NOT import tkinter to remain UI-toolkit agnostic.
All audio arrays are kept in memory and never written to disk or logged.
"""

from collections.abc import Callable
import re
import threading
import time
from typing import Any, Optional

from config import settings
from voice.audio import AudioError, is_silent
from voice.stt import TranscriptionError, filter_hallucinations
from voice.text import clean_for_speech
from voice.tts import NullSpeaker, SpeechError


def strip_page_titles(text: str) -> str:
    """Strip browser page-derived titles from action messages before speech synthesis.

    Plan actions and browser tools report titles such as:
    'Opened result 1: Some Video Title Here. Playing.' -> 'Opened result 1. Playing.'
    'Opened result 2: Another Title' -> 'Opened result 2.'
    """
    if not text or "Opened result" not in text:
        return text

    # Pattern: 'Opened result <num>: <title>. <rest>' -> 'Opened result <num>. <rest>'
    res = re.sub(
        r"Opened result\s+(\d+)\s*:\s*[^.]+\.\s*(.*)",
        r"Opened result \1. \2",
        text,
        flags=re.DOTALL,
    )
    # Pattern: 'Opened result <num>: <title>' -> 'Opened result <num>.'
    res = re.sub(
        r"Opened result\s+(\d+)\s*:\s*.*",
        r"Opened result \1.",
        res,
        flags=re.DOTALL,
    )
    # Pattern without number: 'Opened result: <title>' -> 'Opened result.'
    res = re.sub(
        r"Opened result\s*:\s*.*",
        r"Opened result.",
        res,
        flags=re.DOTALL,
    )
    return res


class VoiceUiController:
    """Thread-safe controller managing voice capture, Whisper transcription, and TTS speech.

    States: 'idle', 'listening', 'transcribing', 'speaking', 'error'.
    """

    def __init__(
        self,
        recorder_factory: Optional[Callable[..., Any]] = None,
        transcriber_factory: Optional[Callable[..., Any]] = None,
        speaker: Optional[Any] = None,
        emit: Optional[Callable[[str, dict], None]] = None,
        is_blocked: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.state: str = "idle"
        self.last_error: str = ""
        self.last_transcription_duration_s: float = 0.0
        self.speak_replies: bool = bool(getattr(settings, "SPEAK_REPLIES", False))

        self._recorder_factory = recorder_factory
        self._transcriber_factory = transcriber_factory
        self.speaker = speaker or NullSpeaker()
        self._emit_fn = emit or (lambda kind, data: None)
        self._is_blocked_fn = is_blocked or (lambda: False)

        self._lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        self._transcriber: Optional[Any] = None
        self._model_load_notified: bool = False
        self._shutdown: bool = False
        self._last_level_emit_time: float = 0.0

    def emit(self, kind: str, data: Optional[dict] = None) -> None:
        """Enqueue or emit an event for the UI thread."""
        try:
            self._emit_fn(kind, data or {})
        except Exception:
            pass

    def _emit_state(self, new_state: str, extra: Optional[dict] = None) -> None:
        self.state = new_state
        payload = {"state": new_state}
        if extra:
            payload.update(extra)
        self.emit("voice_state", payload)

    def is_blocked(self) -> bool:
        """Check whether voice interaction is currently blocked."""
        try:
            return bool(self._is_blocked_fn())
        except Exception:
            return False

    def is_speaking(self) -> bool:
        """Return True if speaker output is actively running."""
        if self.speaker and hasattr(self.speaker, "is_speaking"):
            try:
                return bool(self.speaker.is_speaking())
            except Exception:
                return False
        return self.state == "speaking"

    def poll_speaker(self) -> None:
        """Poll the speaker process status.

        Called exclusively from the UI timer (e.g. every 200 ms), never from worker threads.
        """
        speaking = self.is_speaking()
        if speaking:
            if self.state != "speaking":
                self._emit_state("speaking")
        else:
            if self.state == "speaking":
                self._emit_state("idle")

    def start_listening(self) -> bool:
        """Start audio recording on a background daemon worker thread.

        Refuses if is_blocked() or already listening.
        """
        with self._lock:
            if self._shutdown:
                return False
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return False
            if self.is_blocked():
                self.emit("voice_error", {
                    "text": "Voice input is not available right now.",
                    "error": "blocked",
                })
                return False

            self._stop_event.clear()
            self._cancel_event.clear()
            self._emit_state("listening")

            worker = threading.Thread(
                target=self._run_voice_cycle,
                daemon=True,
                name="VoiceUiControllerWorker",
            )
            self._worker_thread = worker
            worker.start()
            return True

    def stop_listening(self) -> None:
        """Signal recording to stop and begin transcription."""
        self._stop_event.set()

    def _level_callback(self, rms: float) -> None:
        """Rate-limited level callback emitting RMS 0.0 - 1.0 at most 10 times per second."""
        now = time.monotonic()
        if now - self._last_level_emit_time >= 0.1:
            self._last_level_emit_time = now
            level = min(1.0, max(0.0, float(rms)))
            self.emit("voice_level", {"level": round(level, 4)})

    def _map_audio_error(self, exc: Exception) -> str:
        """Map AudioError to friendly text without stack traces."""
        msg = str(exc)
        msg_lower = msg.lower()
        if "sounddevice" in msg_lower:
            return "Microphone package (sounddevice) is not installed."
        if "no microphone" in msg_lower or "input device" in msg_lower or "device query failed" in msg_lower:
            return "No microphone found. Please connect a microphone."
        return f"Microphone error: {msg}"

    def _map_transcription_error(self, exc: Exception) -> str:
        """Map TranscriptionError to friendly text without stack traces."""
        msg = str(exc)
        msg_lower = msg.lower()
        if (
            "downloaded once while online" in msg_lower
            or "not found locally" in msg_lower
            or "offline" in msg_lower
            or "failed to load whisper model" in msg_lower
            or "connection" in msg_lower
        ):
            return "The speech model has to be downloaded once while online."
        if "faster-whisper is not installed" in msg_lower or "faster_whisper" in msg_lower:
            return "Speech model package (faster-whisper) is not installed."
        return f"Transcription error: {msg}"

    def _run_voice_cycle(self) -> None:
        """Record audio, validate duration/silence, and transcribe on the same thread."""
        audio = None
        try:
            if self._recorder_factory is None:
                raise AudioError("No recorder configured.")

            # Construct recorder with rate-limited level callback
            try:
                recorder = self._recorder_factory(level_callback=self._level_callback)
            except TypeError:
                recorder = self._recorder_factory()
                if hasattr(recorder, "level_callback"):
                    recorder.level_callback = self._level_callback

            # Record until stop_event or MAX_RECORD_SECONDS
            audio = recorder.record(stop_event=self._stop_event)

            if self._cancel_event.is_set():
                audio = None
                self._emit_state("idle")
                return

            if audio is None or len(audio) == 0:
                self.emit("voice_error", {"text": "I didn't hear anything"})
                self._emit_state("idle")
                return

            sample_rate = getattr(recorder, "sample_rate", 16000) or 16000
            duration_s = float(len(audio)) / float(sample_rate)

            # Reject audio shorter than 0.3s
            if duration_s < 0.3:
                self.emit("voice_error", {"text": "I didn't hear anything"})
                self._emit_state("idle")
                return

            # Reject silent audio
            if is_silent(audio):
                self.emit("voice_error", {"text": "I didn't hear anything"})
                self._emit_state("idle")
                return

            if self._cancel_event.is_set():
                audio = None
                self._emit_state("idle")
                return

            # Transcribe
            self._emit_state("transcribing")

            if self._transcriber is None:
                if not self._model_load_notified:
                    self._model_load_notified = True
                    self.emit("status", {"text": "Loading speech model..."})

                if callable(self._transcriber_factory):
                    self._transcriber = self._transcriber_factory()
                else:
                    self._transcriber = self._transcriber_factory

            if self._transcriber is None:
                raise TranscriptionError("No transcriber configured.")

            t_start = time.perf_counter()
            transcript_obj = self._transcriber.transcribe(audio, sample_rate=sample_rate)
            duration_trans = time.perf_counter() - t_start
            self.last_transcription_duration_s = round(duration_trans, 2)

            if self._cancel_event.is_set():
                audio = None
                self._emit_state("idle")
                return

            raw_text = transcript_obj.text.strip() if (transcript_obj and transcript_obj.text) else ""
            clean_text = filter_hallucinations(raw_text).strip()

            if clean_text:
                self.emit("voice_transcript", {"text": clean_text, "source": "voice"})

            self._emit_state("idle")

        except AudioError as exc:
            msg = self._map_audio_error(exc)
            self.last_error = msg
            self.emit("voice_error", {"text": msg, "error": str(exc)})
            self._emit_state("error", {"error": msg})
        except TranscriptionError as exc:
            msg = self._map_transcription_error(exc)
            self.last_error = msg
            self.emit("voice_error", {"text": msg, "error": str(exc)})
            self._emit_state("error", {"error": msg})
        except Exception as exc:
            msg = str(exc) or exc.__class__.__name__
            self.last_error = msg
            self.emit("voice_error", {"text": msg, "error": str(exc)})
            self._emit_state("error", {"error": msg})
        finally:
            # Strictly drop references to audio array; never write to disk or log
            audio = None
            with self._lock:
                self._worker_thread = None

    def speak_reply(self, text: str) -> None:
        """Speak final response text non-blocking using SubprocessSpeaker.

        Sanitizes text, strips page-derived titles, caps at 500 characters,
        and speaks only when speak_replies is True.
        """
        if not self.speak_replies or not self.speaker:
            return

        # Strip page titles (e.g. from Opened result ...)
        safe_text = strip_page_titles(text)

        # Sanitize for TTS (max 500 chars total)
        clean_text = clean_for_speech(safe_text, max_chars=475)
        if len(clean_text) > 500:
            clean_text = clean_text[:500]
        if not clean_text or not clean_text.strip():
            return

        try:
            self.speaker.speak(clean_text, blocking=False)
            self._emit_state("speaking")
        except SpeechError as exc:
            self.last_error = str(exc)
            self.emit("voice_error", {"text": f"Speech error: {exc}", "error": str(exc)})
            self._emit_state("idle")
        except Exception as exc:
            self.last_error = str(exc)
            self.emit("voice_error", {"text": f"Speech error: {exc}", "error": str(exc)})
            self._emit_state("idle")

    def stop_speaking(self) -> None:
        """Terminate any active speech process immediately."""
        if self.speaker and hasattr(self.speaker, "stop"):
            try:
                self.speaker.stop()
            except Exception:
                pass
        if self.state == "speaking":
            self._emit_state("idle")

    def cancel(self) -> None:
        """Abort recording or transcription, discard audio, and stop speech."""
        self._cancel_event.set()
        self._stop_event.set()
        self.stop_speaking()
        self._emit_state("idle")

    def shutdown(self) -> None:
        """Idempotently cancel, discard state, and join background worker thread."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True

        self.cancel()
        worker = None
        with self._lock:
            worker = self._worker_thread
        if worker is not None and worker.is_alive():
            if threading.current_thread() != worker:
                worker.join(timeout=2.0)
