"""Text-to-speech (TTS) interfaces, null speaker, and subprocess-isolated speaker."""

from abc import ABC, abstractmethod
import os
import subprocess
import sys
from typing import Optional

from voice.text import clean_for_speech


class SpeechError(Exception):
    """Raised when speech synthesis or worker execution fails."""


class Speaker(ABC):
    """Abstract base class for text-to-speech engines."""

    @abstractmethod
    def speak(self, text: str, blocking: bool = True) -> None:
        """Speak the given text, optionally blocking until speech is complete."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop any active speech immediately."""
        pass

    @abstractmethod
    def is_speaking(self) -> bool:
        """Return True if speech playback is currently active."""
        pass

    def begin_stream(self) -> None:
        """Start a streaming TTS session."""
        pass

    def feed(self, text: str) -> None:
        """Feed a sentence chunk to the active stream."""
        pass

    def end_stream(self, wait: bool = True) -> None:
        """End the active stream and optionally wait for remaining speech."""
        pass


class NullSpeaker(Speaker):
    """No-op speaker implementation for headless or disabled TTS environments."""

    def speak(self, text: str, blocking: bool = True) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_speaking(self) -> bool:
        return False

    def begin_stream(self) -> None:
        pass

    def feed(self, text: str) -> None:
        pass

    def end_stream(self, wait: bool = True) -> None:
        pass


class SubprocessSpeaker(Speaker):
    """TTS speaker that spawns an isolated subprocess worker for pyttsx3 stability."""

    def __init__(
        self,
        command: Optional[list[str]] = None,
        rate: int = 180,
        timeout: float = 60.0,
    ) -> None:
        self.command = list(command) if command is not None else [sys.executable, "-m", "voice.tts_worker"]
        self.rate = rate
        self.timeout = timeout
        self._process: Optional[subprocess.Popen] = None
        self._stream_chars: int = 0
        self._stream_cap_reached: bool = False

    def speak(self, text: str, blocking: bool = True) -> None:
        """Sanitize text and speak it in a background or synchronous subprocess."""
        self.stop()

        cleaned_text = clean_for_speech(text)
        if not cleaned_text or not cleaned_text.strip():
            return

        env = os.environ.copy()
        env["ORBIT_TTS_RATE"] = str(self.rate)

        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                shell=False,
            )
            if self._process.stdin is not None:
                self._process.stdin.write(cleaned_text)
                self._process.stdin.close()
        except Exception as exc:
            raise SpeechError(f"Failed to launch TTS worker: {exc}") from exc

        if blocking and self._process is not None:
            try:
                self._process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
                raise SpeechError(f"TTS process timed out after {self.timeout} seconds.")
            if self._process.returncode != 0:
                err_output = ""
                if self._process.stderr is not None:
                    err_output = self._process.stderr.read().strip()
                raise SpeechError(f"TTS worker failed (exit code {self._process.returncode}): {err_output}")

    def begin_stream(self) -> None:
        """Stop any active process and start worker in --lines streaming mode."""
        self.stop()
        self._stream_chars = 0
        self._stream_cap_reached = False

        cmd = list(self.command)
        if "--lines" not in cmd:
            cmd.append("--lines")

        env = os.environ.copy()
        env["ORBIT_TTS_RATE"] = str(self.rate)

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                shell=False,
                bufsize=1,
            )
        except Exception as exc:
            raise SpeechError(f"Failed to launch TTS streaming worker: {exc}") from exc

    def feed(self, text: str) -> None:
        """Sanitize text sentence and feed it to the streaming worker."""
        if self._stream_cap_reached:
            return

        if self._process is None or self._process.poll() is not None:
            raise SpeechError("TTS worker process is not running or has terminated.")

        if self._process.stdin is None or self._process.stdin.closed:
            raise SpeechError("TTS worker stdin is closed.")

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
            raise SpeechError(f"TTS worker communication error: {exc}") from exc

    def end_stream(self, wait: bool = True) -> None:
        """Close worker stdin and optionally wait for speech playback to finish."""
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
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass

    def stop(self) -> None:
        """Terminate the active speech worker process immediately if running."""
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait()
            except (ProcessLookupError, OSError):
                pass
            finally:
                self._process = None
        else:
            self._process = None

    def is_speaking(self) -> bool:
        """Check whether the TTS subprocess is currently executing."""
        return self._process is not None and self._process.poll() is None
