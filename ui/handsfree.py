"""Hands-free voice controller for the ORBIT desktop window.

Listens locally for the configured wake phrase using a lightweight model,
discards non-wake audio and transcripts immediately, and routes recognized
voice commands through the standard agent execution flow with source="voice-handsfree".

SECURITY CONTRACT:
- This module must NEVER import tkinter to remain UI-toolkit agnostic.
- Audio arrays and non-wake transcripts are strictly ephemeral: non-wake speech
  is discarded immediately and never stored, logged, emitted, or assigned to attributes.
- Voice input ONLY produces text; it NEVER approves, confirms, or resolves dialogs.
- ConfirmDialog and PlanDialog approvals remain strictly keyboard and mouse only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional
import numpy as np

from config import settings
from voice.audio import AudioError
from voice.stt import filter_hallucinations
from voice.vad import UtteranceSegmenter, capture_utterance
from voice.wake import match_wake_phrase

logger = logging.getLogger(__name__)


class HandsFreeController:
    """Thread-safe controller managing background hands-free listening and wake detection.

    States:
    - "off": Hands-free is inactive and stream is closed.
    - "armed": Background daemon thread is listening for wake phrase.
    - "capturing": Wake phrase matched; capturing command utterance.
    - "transcribing": Transcribing command audio with command STT model.
    - "paused": Temporarily paused (e.g. click-to-talk active; mic released).
    - "error": Critical error encountered.
    """

    def __init__(
        self,
        recorder_factory: Optional[Callable[..., Any]] = None,
        wake_transcriber_factory: Optional[Callable[..., Any]] = None,
        command_transcriber_factory: Optional[Callable[..., Any]] = None,
        segmenter_factory: Optional[Callable[..., Any]] = None,
        emit: Optional[Callable[[str, dict], None]] = None,
        is_blocked: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
        beeper: Optional[Any] = None,
        wake_phrase: Optional[str] = None,
        sensitivity: Optional[float] = None,
        idle_minutes: Optional[int] = None,
        followup_seconds: Optional[int] = None,
        listen_timeout_s: Optional[float] = None,
        audit_logger: Optional[Any] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.state: str = "off"
        self._recorder_factory = recorder_factory
        self._wake_transcriber_factory = wake_transcriber_factory
        self._command_transcriber_factory = command_transcriber_factory
        self._segmenter_factory = segmenter_factory or (
            lambda: UtteranceSegmenter(
                threshold=getattr(settings, "VAD_THRESHOLD", 0.01),
                silence_seconds=getattr(settings, "SILENCE_SECONDS", 0.8),
            )
        )
        self._emit_fn = emit or (lambda kind, data: None)
        self._is_blocked_fn = is_blocked or (lambda: False)
        self.clock = clock or time.monotonic
        self.beeper = beeper
        self._sleep_fn = sleep_fn or time.sleep
        self.audit_logger = audit_logger

        # Settings with dynamic fallbacks
        self.wake_phrase = str(
            wake_phrase
            if wake_phrase is not None
            else getattr(settings, "WAKE_PHRASE", "hey orbit")
        ).strip().lower()
        self.sensitivity = float(
            sensitivity
            if sensitivity is not None
            else getattr(settings, "WAKE_SENSITIVITY", 0.80)
        )
        self.idle_minutes = int(
            idle_minutes
            if idle_minutes is not None
            else getattr(settings, "HANDS_FREE_IDLE_MINUTES", 10)
        )
        self.followup_seconds = int(
            followup_seconds
            if followup_seconds is not None
            else getattr(settings, "HANDS_FREE_FOLLOWUP_SECONDS", 0)
        )
        self.listen_timeout_s = float(
            listen_timeout_s
            if listen_timeout_s is not None
            else getattr(settings, "WAKE_LISTEN_TIMEOUT", 6.0)
        )

        # Threading and control primitives
        self._lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cancel_current_event = threading.Event()
        self._pause_event = threading.Event()
        self._shutdown: bool = False

        # Transcribers cache
        self._wake_transcriber: Optional[Any] = None
        self._command_transcriber: Optional[Any] = None

        # Gating hooks (injected by UI)
        self.is_speaking_fn: Callable[[], bool] = lambda: False
        self.is_dialog_open_fn: Callable[[], bool] = lambda: False
        self.is_toast_open_fn: Callable[[], bool] = lambda: False
        self.is_task_running_fn: Callable[[], bool] = lambda: False

        # State tracking timestamps
        self._last_speaking_end: float = -999.0
        self._last_wake_or_armed_time: float = self.clock()
        self._wake_timestamps: list[float] = []
        self._cooldown_until: float = -999.0
        self._consecutive_audio_errors: int = 0
        self._last_dialog_open_state: bool = False

        # Followup window tracking
        self._followup_active: bool = False
        self._followup_deadline: float = -999.0

        # Click-to-talk pause tracking
        self._paused_for_click_to_talk: bool = False

        # Diagnostics & Doctor counters (integers only; zero text/audio)
        self.counters: dict[str, int] = {
            "wake_events": 0,
            "drops_low_confidence": 0,
            "cooldowns": 0,
            "auto_offs": 0,
        }

    # --- Properties ---

    @property
    def is_armed(self) -> bool:
        return self.state == "armed"

    @property
    def is_active(self) -> bool:
        return self.state in ("armed", "capturing", "transcribing", "paused")

    # --- Event Emission & State Transitions ---

    def emit(self, kind: str, data: Optional[dict] = None) -> None:
        """Enqueue or emit an event for the UI thread."""
        try:
            self._emit_fn(kind, data or {})
        except Exception:
            pass

    def _set_state(self, new_state: str, extra: Optional[dict] = None) -> None:
        self.state = new_state
        payload = {"state": new_state}
        if extra:
            payload.update(extra)
        self.emit("handsfree_state", payload)

    # --- Transcriber Resolvers ---

    def _get_wake_transcriber(self) -> Any:
        if self._wake_transcriber is None:
            if callable(self._wake_transcriber_factory):
                self._wake_transcriber = self._wake_transcriber_factory()
            else:
                self._wake_transcriber = self._wake_transcriber_factory
        return self._wake_transcriber

    def _get_command_transcriber(self) -> Any:
        if self._command_transcriber is None:
            if callable(self._command_transcriber_factory):
                self._command_transcriber = self._command_transcriber_factory()
            else:
                self._command_transcriber = self._command_transcriber_factory
        return self._command_transcriber

    # --- Muting & Gating Rules ---

    def is_muted(self) -> bool:
        """Check all stream gating conditions. When muted, audio frames are dropped."""
        now = self.clock()

        # 1. Speaking plus 1.0s echo suppression
        if self.is_speaking_fn():
            self._last_speaking_end = now
            return True
        if (now - self._last_speaking_end) < 1.0:
            return True

        # 2. Confirmation, plan dialog, or reminder toast open
        dialog_open = False
        try:
            dialog_open = bool(self.is_dialog_open_fn())
        except Exception:
            pass

        # Detect edge transition to open dialog -> play attention cue & cancel followup
        if dialog_open and not self._last_dialog_open_state:
            if self.beeper and hasattr(self.beeper, "play"):
                self.beeper.play("attention")
            self._followup_active = False
        self._last_dialog_open_state = dialog_open

        if dialog_open:
            self._followup_active = False
            return True

        try:
            if self.is_toast_open_fn():
                return True
        except Exception:
            pass

        # 3. Task running (unless inside active follow-up window)
        try:
            task_running = bool(self.is_task_running_fn())
        except Exception:
            task_running = False

        if task_running and not self._followup_active:
            return True

        # 4. Click-to-talk pause
        if self._paused_for_click_to_talk:
            return True

        # 5. Generic blocked condition
        try:
            if self._is_blocked_fn():
                return True
        except Exception:
            pass

        return False

    # --- Lifecycle Controls ---

    def start(self) -> bool:
        """Enable and arm hands-free listening on a background daemon worker thread."""
        with self._lock:
            if self._shutdown:
                return False
            if self.state in ("armed", "capturing", "transcribing"):
                return True

            self._stop_event.clear()
            self._cancel_current_event.clear()
            self._pause_event.clear()
            self._last_wake_or_armed_time = self.clock()
            self._consecutive_audio_errors = 0
            self._followup_active = False
            self._set_state("armed")

            worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="HandsFreeWorkerThread",
            )
            self._worker_thread = worker
            worker.start()
            return True

    def stop(self) -> None:
        """Dedicated off switch: turns off hands-free and joins worker within 1 second."""
        with self._lock:
            if self.state == "off":
                return
            self.state = "off"
            self._stop_event.set()
            self._cancel_current_event.set()
            self._followup_active = False

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=0.85)

        self._set_state("off")
        if self.beeper and hasattr(self.beeper, "play"):
            self.beeper.play("off")

    def turn_off(self) -> None:
        """Alias for stop()."""
        self.stop()

    def shutdown(self) -> None:
        """Idempotent shutdown handler."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self.stop()

    def pause_for_click_to_talk(self) -> None:
        """Pause hands-free and release microphone during manual click-to-talk."""
        with self._lock:
            self._paused_for_click_to_talk = True
            self._pause_event.set()
            if self.state in ("armed", "capturing", "transcribing"):
                self._set_state("paused")

    def resume_from_click_to_talk(self) -> None:
        """Resume hands-free listening after manual click-to-talk completes."""
        with self._lock:
            self._paused_for_click_to_talk = False
            self._pause_event.clear()
            if self.state == "paused":
                self._last_wake_or_armed_time = self.clock()
                self._set_state("armed")

    def cancel_current_capture(self) -> None:
        """Stop action: cancels in-progress capture/transcription, leaves armed."""
        self._cancel_current_event.set()
        self._followup_active = False

    def notify_reply(self) -> None:
        """Notify that an assistant response was delivered to arm follow-up window."""
        if self.state == "armed" and self.followup_seconds > 0:
            self._followup_active = True
            self._followup_deadline = self.clock() + float(self.followup_seconds)

    def cancel_followup(self) -> None:
        """Cancel active follow-up window (e.g. when dialog opens)."""
        self._followup_active = False

    # --- Audit Logging Helper (Zero text/audio) ---

    def _log_audit(self, decision: str, outcome: str = "success") -> None:
        if self.audit_logger is not None and hasattr(self.audit_logger, "log"):
            try:
                self.audit_logger.log(
                    tool="handsfree",
                    args={},
                    decision=decision,
                    outcome=outcome,
                    source="voice-handsfree",
                )
            except Exception:
                pass

    # --- Worker Thread Audio Loop ---

    def _worker_loop(self) -> None:
        """Background daemon audio stream loop."""
        segmenter = self._segmenter_factory()
        sample_rate = getattr(segmenter, "sample_rate", 16000) or 16000

        while not self._stop_event.is_set():
            now = self.clock()

            # 1. Check idle auto-off
            idle_timeout_s = float(self.idle_minutes * 60)
            if (now - self._last_wake_or_armed_time) >= idle_timeout_s:
                self.counters["auto_offs"] += 1
                self._log_audit("handsfree_auto_off", outcome="idle_timeout")
                self.emit("handsfree_notice", {
                    "text": f"Hands-free turned off after {self.idle_minutes} idle minutes"
                })
                self.state = "off"
                self._set_state("off")
                if self.beeper and hasattr(self.beeper, "play"):
                    self.beeper.play("off")
                break

            # 2. Check paused state
            if self._pause_event.is_set():
                segmenter.reset()
                self._sleep_fn(0.05)
                continue

            # 3. Check gating / muting
            if self.is_muted():
                segmenter.reset()
                self._sleep_fn(0.05)
                continue

            # 4. Check follow-up window expiration
            if self._followup_active and now >= self._followup_deadline:
                self._followup_active = False

            # 5. Capture single utterance segment
            try:
                audio = self._read_utterance_segment(segmenter, sample_rate)
                self._consecutive_audio_errors = 0
            except AudioError as exc:
                self._consecutive_audio_errors += 1
                logger.debug("Audio error in handsfree loop: %s", exc)
                if self._consecutive_audio_errors >= 3:
                    self.emit("handsfree_notice", {
                        "text": "Hands-free turned off due to audio errors"
                    })
                    self.state = "off"
                    self._set_state("off")
                    if self.beeper and hasattr(self.beeper, "play"):
                        self.beeper.play("off")
                    break
                self._sleep_fn(0.2)
                continue
            except Exception as exc:
                logger.debug("Unexpected exception in handsfree loop: %s", exc)
                self._sleep_fn(0.1)
                continue

            if audio is None or len(audio) == 0:
                continue

            if self._stop_event.is_set():
                break

            # If follow-up window is active, treat this speech directly as command
            if self._followup_active:
                self._followup_active = False
                self._process_command_utterance(audio, sample_rate)
                continue

            # Otherwise, evaluate utterance for wake detection
            duration_s = float(len(audio)) / float(sample_rate)

            # Wake detection runs ONLY on speech between 0.3 s and 4.0 s
            if duration_s < 0.3 or duration_s > 4.0:
                # Discard non-matching length audio immediately
                audio = None
                continue

            # Crop to the first 3.0 s before passing to wake model
            max_samples = int(3.0 * sample_rate)
            wake_crop = audio[:max_samples] if len(audio) > max_samples else audio

            # Wake STT decode
            wake_transcriber = self._get_wake_transcriber()
            if wake_transcriber is None:
                continue

            try:
                t_res = wake_transcriber.transcribe(wake_crop, sample_rate=sample_rate)
            except Exception:
                continue

            raw_text = t_res.text if hasattr(t_res, "text") else str(t_res)

            # Fuzzy phrase matching
            remainder = match_wake_phrase(
                raw_text,
                phrase=self.wake_phrase,
                threshold=self.sensitivity,
            )

            # PRIVACY CONTRACT: Non-wake transcripts are DISCARDED at once.
            # Never stored, logged, emitted, or kept in any attribute.
            if remainder is None:
                del raw_text, t_res, wake_crop, audio
                continue

            # Clean raw variables immediately
            del raw_text, t_res, wake_crop

            # Wake match detected! Update activity timestamp
            self._last_wake_or_armed_time = self.clock()

            # Check wake rate limit: at most 6 per minute
            clean_ts: list[float] = [
                ts for ts in self._wake_timestamps if (self.clock() - ts) < 60.0
            ]
            self._wake_timestamps = clean_ts

            if len(self._wake_timestamps) >= 6 or self.clock() < self._cooldown_until:
                self._cooldown_until = self.clock() + 30.0
                self.counters["cooldowns"] += 1
                self._log_audit("handsfree_cooldown", outcome="rate_limit")
                self.emit("handsfree_notice", {
                    "text": "Hands-free rate limit reached; cooling down for 30 seconds"
                })
                continue

            self._wake_timestamps.append(self.clock())
            self.counters["wake_events"] += 1
            self._log_audit("handsfree_wake", outcome="success")
            self.emit("handsfree_wake", {})

            # Play wake cue
            if self.beeper and hasattr(self.beeper, "play"):
                self.beeper.play("wake")

            # Check for inline command remainder in the same utterance
            trimmed_remainder = remainder.strip()
            if trimmed_remainder:
                self._handle_command_text(trimmed_remainder)
            else:
                # Capture the next utterance
                self._capture_and_process_next_command(sample_rate)

    def _read_utterance_segment(
        self,
        segmenter: UtteranceSegmenter,
        sample_rate: int,
    ) -> Optional[np.ndarray]:
        """Capture one speech segment using configured recorder or stream."""
        if self._recorder_factory is None:
            # Fallback to direct capture_utterance with default microphone
            return capture_utterance(
                stream_factory=None,
                segmenter=segmenter,
                cancel_event=self._stop_event,
            )

        # Call factory
        try:
            rec = self._recorder_factory()
        except TypeError:
            rec = self._recorder_factory

        # Support mock or direct frame generators
        if hasattr(rec, "get_audio"):
            return rec.get_audio()
        if hasattr(rec, "record"):
            return rec.record(stop_event=self._stop_event)
        if callable(getattr(rec, "read_frame", None)):
            segmenter.reset()
            while not self._stop_event.is_set() and not self._pause_event.is_set():
                frame = rec.read_frame()
                if frame is None:
                    break
                st = segmenter.feed(frame)
                if st == "done":
                    return segmenter.get_audio()
            return None

        # Fallback to stream factory
        sf = getattr(rec, "stream_factory", rec)
        return capture_utterance(
            stream_factory=sf if callable(sf) else None,
            segmenter=segmenter,
            cancel_event=self._stop_event,
        )

    def _capture_and_process_next_command(self, sample_rate: int) -> None:
        """Capture next utterance following wake match with timeout and silence endpointing."""
        self._set_state("capturing")
        self._cancel_current_event.clear()

        cmd_segmenter = self._segmenter_factory()
        if self._recorder_factory is not None:
            cmd_audio = self._read_utterance_segment(cmd_segmenter, sample_rate)
        else:
            cmd_audio = capture_utterance(
                stream_factory=None,
                segmenter=cmd_segmenter,
                start_timeout=self.listen_timeout_s,
                cancel_event=self._cancel_current_event,
            )

        if self._cancel_current_event.is_set():
            self._set_state("armed")
            return

        if cmd_audio is None or len(cmd_audio) == 0:
            self._set_state("armed")
            return

        self._process_command_utterance(cmd_audio, sample_rate)

    def _process_command_utterance(self, audio: np.ndarray, sample_rate: int) -> None:
        """Enhance, transcribe, and validate an audio command."""
        self._set_state("transcribing")

        if self._cancel_current_event.is_set():
            self._set_state("armed")
            return

        # Audio enhancement
        try:
            from voice.enhance import enhance
            audio = enhance(audio, settings)
        except Exception:
            pass

        cmd_transcriber = self._get_command_transcriber()
        if cmd_transcriber is None:
            self._set_state("armed")
            return

        try:
            t_res = cmd_transcriber.transcribe(audio, sample_rate=sample_rate)
        except Exception as exc:
            logger.debug("Command transcription failed: %s", exc)
            self._set_state("armed")
            return

        if self._cancel_current_event.is_set():
            self._set_state("armed")
            return

        raw_text = t_res.text if hasattr(t_res, "text") else str(t_res)
        avg_logprob = getattr(t_res, "avg_logprob", None)
        no_speech_prob = getattr(t_res, "no_speech_prob", None)

        self._handle_command_text(raw_text, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob)

    def _handle_command_text(
        self,
        text: str,
        avg_logprob: Optional[float] = None,
        no_speech_prob: Optional[float] = None,
    ) -> None:
        """Apply alias resolution and quality drops, then emit handsfree_command."""
        clean_text = filter_hallucinations(text).strip()

        # Alias resolution
        if clean_text and bool(getattr(settings, "VOICE_ALIASES", 0)):
            try:
                from config.apps import ALLOWED_APPS
                from config.sites import SITES
                from core.chains import parse_chain
                from core.shortcuts import parse_shortcut
                from voice.aliases import resolve

                def _parse_cand(t: str) -> Any:
                    browser_on = getattr(settings, "BROWSER_ENABLED", "auto") != "0"
                    sc = parse_shortcut(
                        t,
                        allowed_apps=ALLOWED_APPS,
                        sites=SITES,
                        browser_enabled=browser_on,
                    )
                    if sc is not None:
                        return sc
                    if bool(getattr(settings, "CHAINS_ENABLED", True)):
                        ch = parse_chain(
                            t,
                            allowed_apps=ALLOWED_APPS,
                            sites=SITES,
                            browser_enabled=browser_on,
                        )
                        if ch is not None:
                            return ch
                    return None

                resolved, _ = resolve(clean_text, _parse_cand, allowed_apps=ALLOWED_APPS, sites=SITES)
                clean_text = resolved
            except Exception:
                pass

        # Quality Drop Conditions:
        # 1. avg_logprob < -1.0
        # 2. no_speech_prob > 0.6
        # 3. fewer than 2 words
        # 4. empty text
        words = clean_text.split()
        is_low_logprob = avg_logprob is not None and avg_logprob < -1.0
        is_high_no_speech = no_speech_prob is not None and no_speech_prob > 0.6
        is_too_short = len(words) < 2
        is_empty = not clean_text

        if is_low_logprob or is_high_no_speech or is_too_short or is_empty:
            self.counters["drops_low_confidence"] += 1
            if self.beeper and hasattr(self.beeper, "play"):
                self.beeper.play("error")
            self.emit("handsfree_notice", {"text": "I didn't catch that", "dropped": True})
            self._set_state("armed")
            return

        # Accepted command: play done cue and emit command
        if self.beeper and hasattr(self.beeper, "play"):
            self.beeper.play("done")

        self.emit("handsfree_command", {
            "text": clean_text,
            "source": "voice-handsfree",
        })
        self._set_state("armed")
