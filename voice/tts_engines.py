"""TTS engine selector, Piper neural speaker, and safe SAPI fallback.

Supports:
- "sapi" (default): Windows SAPI5 via existing SubprocessSpeaker (unchanged)
- "piper": Local neural speech via voice.piper_worker with automatic fallback to SAPI

SECURITY & RELIABILITY GUARANTEES:
1. Piper is imported lazily inside the worker process only; tts_engines never imports piper.
2. Voice file path is strictly validated against voices_dir() with size and JSON checks.
3. Leftover temporary directories of dead workers are cleaned at startup.
4. Stop terminates the worker, then kills after 1 second if still alive.
5. On any neural failure (missing package, invalid voice, timeout >15s, worker exit),
   the system seamlessly falls back to SAPI, displays ONE notice per session,
   records the reason in diagnostics, and never raises to the UI.
"""

from __future__ import annotations
import os

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Callable, Optional, Union

from config import settings
from voice.text import clean_for_speech
from voice.tts import Speaker, SpeechError, SubprocessSpeaker
from voice.voices import validate_voice_file

# Diagnostics tracking
LAST_TTS_FALLBACK_REASON: Optional[str] = None
_SESSION_NOTIFIED_FALLBACK: bool = False


def reset_session_fallback_notice() -> None:
    """Reset the session fallback notification flag (used for testing and new sessions)."""
    global _SESSION_NOTIFIED_FALLBACK, LAST_TTS_FALLBACK_REASON
    _SESSION_NOTIFIED_FALLBACK = False
    LAST_TTS_FALLBACK_REASON = None


def clean_dead_worker_tempdirs() -> None:
    """Clean leftover temporary directories matching 'orbit_piper_*' from dead workers."""
    try:
        temp_base = Path(tempfile.gettempdir())
        for item in temp_base.glob("orbit_piper_*"):
            if item.is_dir():
                import shutil

                shutil.rmtree(str(item), ignore_errors=True)
    except Exception:
        pass


class PiperSpeaker(Speaker):
    """Neural text-to-speech speaker executing voice.piper_worker as a subprocess."""

    def __init__(
        self,
        voice_path: Optional[Union[str, Path]] = None,
        voice_file: Optional[Union[str, Path]] = None,
        speed: float = 1.0,
        volume: float = 1.0,
        worker_cmd: Optional[list[str]] = None,
        timeout_per_sentence: float = 15.0,
    ) -> None:
        self.speed = float(speed)
        self.volume = float(volume)
        self.worker_cmd = list(worker_cmd) if worker_cmd is not None else None
        self.timeout_per_sentence = float(timeout_per_sentence)
        self._process: Optional[subprocess.Popen] = None
        self._stream_chars: int = 0
        self._stream_cap_reached: bool = False

        # Clean leftover temporary directories from old workers at startup
        clean_dead_worker_tempdirs()

        # Resolve voice path
        v_target = voice_path if voice_path is not None else voice_file
        self.voice_path: Optional[Path] = None
        if v_target is not None:
            self.voice_path = Path(v_target).resolve()
        else:
            voice_name = getattr(settings, "PIPER_VOICE", "") or ""
            if voice_name.strip():
                is_valid, _, mod_path, _ = validate_voice_file(voice_name.strip())
                if is_valid and mod_path is not None:
                    self.voice_path = mod_path

    @property
    def is_available(self) -> bool:
        return (importlib.util.find_spec("piper") is not None) or (importlib.util.find_spec("piper_voice") is not None)

    def _build_command(self) -> list[str]:
        if self.worker_cmd is not None:
            return list(self.worker_cmd)

        v_path = str(self.voice_path) if self.voice_path else ""
        return [
            sys.executable,
            "-m",
            "voice.piper_worker",
            "--voice",
            v_path,
            "--speed",
            str(self.speed),
            "--volume",
            str(self.volume),
        ]

    def speak(self, text: str, blocking: bool = True) -> None:
        """Sanitize text and speak it through the piper worker process."""
        self.stop()

        cleaned_text = clean_for_speech(text)
        if not cleaned_text or not cleaned_text.strip():
            return

        cmd = self._build_command()
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
            if self._process.stdin is not None:
                self._process.stdin.write(cleaned_text + "\n")
                self._process.stdin.close()
        except Exception as exc:
            raise SpeechError(f"Failed to launch Piper worker: {exc}") from exc

        if blocking and self._process is not None:
            try:
                self._process.wait(timeout=self.timeout_per_sentence)
            except subprocess.TimeoutExpired:
                self.stop()
                raise SpeechError(f"Synthesis timed out after {self.timeout_per_sentence} seconds.")
            if self._process.returncode != 0:
                err_output = ""
                if self._process.stderr is not None:
                    err_output = self._process.stderr.read().strip()
                raise SpeechError(f"Piper worker failed (exit code {self._process.returncode}): {err_output}")

    def begin_stream(self) -> None:
        """Start worker in streaming sentence mode."""
        self.stop()
        self._stream_chars = 0
        self._stream_cap_reached = False

        cmd = self._build_command()
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                bufsize=1,
            )
        except Exception as exc:
            raise SpeechError(f"Failed to launch Piper streaming worker: {exc}") from exc

    def feed(self, text: str) -> None:
        """Feed a sentence chunk to the streaming worker."""
        if self._stream_cap_reached:
            return

        if self._process is None or self._process.poll() is not None:
            raise SpeechError("Piper worker process is not running or has terminated.")

        if self._process.stdin is None or self._process.stdin.closed:
            raise SpeechError("Piper worker stdin is closed.")

        cleaned = clean_for_speech(text, max_chars=100000).replace("\r", " ").replace("\n", " ").strip()
        if not cleaned:
            return

        self._stream_chars += len(cleaned)
        try:
            if self._stream_chars > 1200:
                self._stream_cap_reached = True
                self._process.stdin.write("The rest is in the text.\n")
                self._process.stdin.flush()
                return

            self._process.stdin.write(cleaned + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SpeechError(f"Piper worker communication error: {exc}") from exc

    def end_stream(self, wait: bool = True) -> None:
        """Close worker stdin and optionally wait for playback to complete."""
        proc = self._process
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

            if wait and proc.poll() is None:
                try:
                    proc.wait(timeout=120.0)
                except subprocess.TimeoutExpired:
                    self.stop()
                except Exception:
                    pass

    def stop(self) -> None:
        """Terminate the worker, and kill after 1 second if still alive."""
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
            except (ProcessLookupError, OSError):
                pass
            finally:
                self._process = None
        else:
            self._process = None

    def is_speaking(self) -> bool:
        """Return True if the worker process is currently running."""
        return self._process is not None and self._process.poll() is None


class FallbackSpeaker(Speaker):
    """Wrapper that tries Piper first and falls back to SAPI on any failure.

    Emits at most ONE notice per session when fallback occurs and records the reason in diagnostics.
    """

    def __init__(
        self,
        piper_speaker: PiperSpeaker,
        sapi_speaker: SubprocessSpeaker,
        on_fallback_notice: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.piper_speaker = piper_speaker
        self.sapi_speaker = sapi_speaker
        self.on_fallback_notice = on_fallback_notice
        self._using_fallback = False

    def _trigger_fallback(self, reason: str, text: Optional[str] = None, blocking: bool = False) -> None:
        global LAST_TTS_FALLBACK_REASON, _SESSION_NOTIFIED_FALLBACK
        self._using_fallback = True
        LAST_TTS_FALLBACK_REASON = reason

        if not _SESSION_NOTIFIED_FALLBACK:
            _SESSION_NOTIFIED_FALLBACK = True
            notice = f"Neural voice unavailable ({reason}); using the Windows voice"
            if self.on_fallback_notice is not None:
                try:
                    self.on_fallback_notice(notice)
                except Exception:
                    pass

        # Speak via SAPI fallback
        if text:
            try:
                self.sapi_speaker.speak(text, blocking=blocking)
            except Exception:
                pass

    def speak(self, text: str, blocking: bool = True) -> None:
        has_cmd = isinstance(getattr(self.piper_speaker, "worker_cmd", None), (list, tuple))

        # Pre-check piper installation
        piper_avail = getattr(self.piper_speaker, "is_available", None)
        if callable(piper_avail):
            avail = bool(piper_avail())
        elif piper_avail is not None:
            avail = bool(piper_avail)
        else:
            avail = (importlib.util.find_spec("piper") is not None) or (importlib.util.find_spec("piper_voice") is not None)

        if not avail and not has_cmd:
            self._trigger_fallback("piper is not installed", text=text, blocking=blocking)
            return

        # Pre-check voice file existence & validity
        voice_name = getattr(settings, "PIPER_VOICE", "") or ""
        v_path = getattr(self.piper_speaker, "voice_path", None)
        if not voice_name.strip() and v_path is None and not has_cmd:
            self._trigger_fallback("no voice file configured", text=text, blocking=blocking)
            return

        if v_path is None and not has_cmd:
            is_valid, msg, mod_path, _ = validate_voice_file(voice_name.strip())
            if not is_valid or mod_path is None:
                self._trigger_fallback(f"voice file invalid: {msg}", text=text, blocking=blocking)
                return
            self.piper_speaker.voice_path = mod_path

        # Attempt neural speech
        try:
            self.piper_speaker.speak(text, blocking=blocking)
        except Exception as exc:
            short_err = str(exc)
            if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or "timeout" in short_err.lower() or "timed out" in short_err.lower():
                short_reason = "synthesis timed out"
            else:
                short_reason = f"worker error: {short_err}"[:60]
            self._trigger_fallback(short_reason, text=text, blocking=blocking)

    def begin_stream(self) -> None:
        if importlib.util.find_spec("piper") is None and self.piper_speaker.worker_cmd is None:
            self._trigger_fallback("piper is not installed")
            self.sapi_speaker.begin_stream()
            return

        voice_name = getattr(settings, "PIPER_VOICE", "") or ""
        if not voice_name.strip() and self.piper_speaker.voice_path is None and self.piper_speaker.worker_cmd is None:
            self._trigger_fallback("no voice file configured")
            self.sapi_speaker.begin_stream()
            return

        try:
            self.piper_speaker.begin_stream()
        except Exception as exc:
            self._trigger_fallback(f"worker error: {exc}"[:60])
            self.sapi_speaker.begin_stream()

    def feed(self, text: str) -> None:
        if self._using_fallback:
            self.sapi_speaker.feed(text)
            return

        try:
            self.piper_speaker.feed(text)
        except Exception as exc:
            self._trigger_fallback(f"stream error: {exc}"[:60])
            self.sapi_speaker.feed(text)

    def end_stream(self, wait: bool = True) -> None:
        if self._using_fallback:
            self.sapi_speaker.end_stream(wait=wait)
        else:
            self.piper_speaker.end_stream(wait=wait)

    def stop(self) -> None:
        self.piper_speaker.stop()
        self.sapi_speaker.stop()

    def is_speaking(self) -> bool:
        return self.piper_speaker.is_speaking() or self.sapi_speaker.is_speaking()


def get_speaker(
    engine: Optional[str] = None,
    speed: Optional[float] = None,
    volume: Optional[float] = None,
    voice: Optional[Union[str, Path]] = None,
    rate: Optional[int] = None,
    on_fallback_notice: Optional[Callable[[str], None]] = None,
) -> Speaker:
    """Factory returning the configured Speaker instance based on TTS_ENGINE setting."""
    eng = (engine or os.environ.get("TTS_ENGINE") or getattr(settings, "TTS_ENGINE", "sapi")).strip().lower()

    if eng == "piper":
        # Resolve speed, volume, and voice
        spd = speed if speed is not None else getattr(settings, "TTS_SPEED", 1.0)
        vol = volume if volume is not None else getattr(settings, "TTS_VOLUME", 1.0)
        sapi_rate = rate if rate is not None else getattr(settings, "TTS_RATE", int(180 * spd))

        piper_spk = PiperSpeaker(voice_path=voice, speed=spd, volume=vol)
        sapi_spk = SubprocessSpeaker(rate=sapi_rate)
        return FallbackSpeaker(piper_spk, sapi_spk, on_fallback_notice=on_fallback_notice)

    # Default SAPI engine
    sapi_rate = rate if rate is not None else getattr(settings, "TTS_RATE", 180)
    return SubprocessSpeaker(rate=sapi_rate)
