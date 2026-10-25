"""Voice session module managing speech-to-text input, agent dispatch, and text-to-speech output."""

import time
from typing import Any, Callable, Optional
import numpy as np

from voice.audio import AudioError, is_silent
from voice.stt import TranscriptionError
from voice.tts import SpeechError
from voice.streaming import SentenceSplitter

# ==============================================================================
# SECURITY ARCHITECTURE NOTE: WHY CONFIRMATIONS REMAIN KEYBOARD-ONLY
# ==============================================================================
# Voice input strictly produces text messages that are routed to Agent.chat().
# Voice input MUST NEVER automatically approve or bypass security confirmation
# prompts (confirm_callback).
#
# Confirmation prompts for CONFIRM-level tools (file writes, launching apps, etc.)
# remain strictly keyboard-only (terminal y/n prompt) for several reasons:
# 1. Acoustic injection defense: Audio from external speakers, podcasts, videos,
#    or room background noise could inadvertently contain speech sounding like
#    "yes" or "confirm", triggering dangerous operations without user awareness.
# 2. Intentional verification: Requiring a deliberate, physical keystroke ensures
#    the user consciously evaluates the action, tool parameters, and taint warnings.
# 3. Ambient safety: Background conversation can never trick ORBIT into authorizing
#    restricted desktop or filesystem actions.
# ==============================================================================


class VoiceSession:
    """Manages conversational voice loop: audio capture, STT, agent chat, and TTS playback."""

    def __init__(
        self,
        agent: Any,
        recorder: Any,
        transcriber: Any,
        speaker: Any,
        on_status: Callable[[str], None] = print,
        speak_enabled: bool = True,
        monitor: Optional[Any] = None,
    ) -> None:
        self.agent = agent
        self.recorder = recorder
        self.transcriber = transcriber
        self.speaker = speaker
        self.on_status = on_status
        self._speak_enabled = bool(speak_enabled)
        self.monitor = monitor
        self.last_latency: dict[str, float] = {}
        self._last_speech_end_to_transcript: Optional[float] = None

    @property
    def speak_enabled(self) -> bool:
        """Whether spoken replies are enabled."""
        return self._speak_enabled

    @speak_enabled.setter
    def speak_enabled(self, value: bool) -> None:
        self._speak_enabled = bool(value)

    def is_speaking(self) -> bool:
        """Check whether speaker is currently outputting audio."""
        if hasattr(self.speaker, "is_speaking"):
            return bool(self.speaker.is_speaking())
        return False

    def listen_once(
        self,
        stop_event: Optional[Any] = None,
        audio: Optional[np.ndarray] = None,
    ) -> str:
        """Record and transcribe a single speech utterance.

        Stops any ongoing speech before recording so ORBIT does not listen to itself.
        Returns the recognized text, or "" if audio is silent, empty, or an error occurs.
        """
        try:
            if audio is None:
                # Always stop playback before listening to prevent acoustic self-listening
                self.speaker.stop()
                audio = self.recorder.record(stop_event=stop_event)

            t_speech_end = time.monotonic()

            if audio is None or len(audio) == 0 or is_silent(audio):
                self.on_status("I didn't hear anything")
                return ""

            transcript = self.transcriber.transcribe(audio)
            t_transcript = time.monotonic()
            self._last_speech_end_to_transcript = t_transcript - t_speech_end

            text = transcript.text.strip() if transcript and transcript.text else ""
            return text
        except (AudioError, TranscriptionError) as exc:
            self.on_status(f"Listening error: {exc}")
            return ""

    def respond(
        self,
        text: str,
        speech_end_to_transcript: Optional[float] = None,
    ) -> str:
        """Dispatch text response with streaming TTS, interrupt monitoring, and latency tracking."""
        if self.monitor is not None:
            self.monitor.arm()
            if hasattr(self.monitor, "event") and self.monitor.event is not None:
                self.monitor.event.clear()

        cancel_event = self.monitor.event if (self.monitor and hasattr(self.monitor, "event")) else None

        t_start = time.monotonic()
        t_first_delta: Optional[float] = None
        t_first_feed: Optional[float] = None
        delta_count = 0
        splitter = SentenceSplitter()
        reply = ""

        try:
            if self._speak_enabled:
                try:
                    self.speaker.begin_stream()
                except SpeechError as exc:
                    self.on_status(f"Speech error: {exc}")

            def on_delta(delta: str) -> None:
                nonlocal t_first_delta, t_first_feed, delta_count
                if not delta:
                    return
                if cancel_event is not None and cancel_event.is_set():
                    return
                delta_count += 1
                now = time.monotonic()
                if t_first_delta is None:
                    t_first_delta = now

                chunks = splitter.feed(delta)
                if self._speak_enabled:
                    for chunk in chunks:
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        if t_first_feed is None:
                            t_first_feed = time.monotonic()
                        try:
                            self.speaker.feed(chunk)
                        except SpeechError as exc:
                            self.on_status(f"Speech error: {exc}")

            try:
                reply = self.agent.chat(
                    text,
                    on_delta=on_delta,
                    cancel_event=cancel_event,
                )
            except TypeError:
                reply = self.agent.chat(text)

            is_interrupted = bool(cancel_event and cancel_event.is_set())
            if is_interrupted:
                try:
                    self.speaker.stop()
                except SpeechError as exc:
                    self.on_status(f"Speech error: {exc}")
                self.on_status("(interrupted)")
            else:
                if delta_count > 0:
                    if self._speak_enabled:
                        rem = splitter.flush()
                        if rem:
                            if t_first_feed is None:
                                t_first_feed = time.monotonic()
                            try:
                                self.speaker.feed(rem)
                            except SpeechError as exc:
                                self.on_status(f"Speech error: {exc}")
                        try:
                            self.speaker.end_stream(wait=True)
                        except SpeechError as exc:
                            self.on_status(f"Speech error: {exc}")
                else:
                    # Provider did not stream deltas: fall back to speaker.speak()
                    if self._speak_enabled:
                        try:
                            self.speaker.end_stream(wait=False)
                        except SpeechError:
                            pass
                        if reply:
                            try:
                                self.speaker.speak(reply)
                            except SpeechError as exc:
                                self.on_status(f"Speech error: {exc}")

        except SpeechError as exc:
            self.on_status(f"Speech error: {exc}")
        finally:
            if self.monitor is not None:
                self.monitor.disarm()

            t_end = time.monotonic()
            lat: dict[str, float] = {}
            s_to_t = (
                speech_end_to_transcript
                if speech_end_to_transcript is not None
                else self._last_speech_end_to_transcript
            )
            if s_to_t is not None:
                lat["speech_end_to_transcript"] = round(s_to_t, 4)
            if t_first_delta is not None:
                lat["transcript_to_first_delta"] = round(t_first_delta - t_start, 4)
            if t_first_feed is not None and t_first_delta is not None:
                lat["first_delta_to_first_audio"] = round(t_first_feed - t_first_delta, 4)
            if s_to_t is not None:
                lat["total"] = round(s_to_t + (t_end - t_start), 4)
            else:
                lat["total"] = round(t_end - t_start, 4)

            self.last_latency = lat

        return reply

    def turn(
        self,
        stop_event: Optional[Any] = None,
        audio: Optional[np.ndarray] = None,
    ) -> Optional[str]:
        """Execute a single conversational voice turn.

        Captures user speech, dispatches the transcript to respond(),
        and returns the response text. Returns None if no speech was detected.
        """
        text = self.listen_once(stop_event=stop_event, audio=audio)
        if not text:
            return None

        self.on_status(f'Heard: "{text}"')
        return self.respond(text, speech_end_to_transcript=self._last_speech_end_to_transcript)
