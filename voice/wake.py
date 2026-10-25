"""Wake word detection module with fuzzy phrase matching and cooldown protection."""

from dataclasses import dataclass
import difflib
import re
import time
from typing import Any, Callable, Optional

from voice.audio import AudioError
from voice.stt import TranscriptionError


def normalize(text: str) -> list[str]:
    """Lowercase text, strip punctuation, and split into tokens."""
    if not text:
        return []
    clean = re.sub(r"[^\w\s]", "", text.lower())
    return clean.split()


def match_wake_phrase(text: str, phrase: str = "hey orbit", threshold: float = 0.8) -> Optional[str]:
    """Check if text begins with the wake phrase and return the remaining command text.

    Matching is fuzzy (SequenceMatcher) because speech-to-text engines commonly
    mangle wake names on brief or accented speech ("hey orbit", "hey or bit", "a orbit").
    Returns None if text does not begin with the phrase; returns "" if only the phrase was spoken.
    Allows an optional leading "ok" or "okay".
    """
    if not text or not text.strip():
        return None

    orig_tokens = text.split()
    phrase_tokens = normalize(phrase)
    if not phrase_tokens:
        return None

    target_str = "".join(phrase_tokens)
    k = len(phrase_tokens)

    # Determine candidate start offsets (allowing optional leading "ok" or "okay")
    start_offsets = [0]
    first_token_norm = normalize(orig_tokens[0])
    if first_token_norm and first_token_norm[0] in ("ok", "okay") and phrase_tokens[0] not in ("ok", "okay"):
        start_offsets.append(1)

    best_ratio = -1.0
    best_remainder: Optional[str] = None

    for start in start_offsets:
        tokens = orig_tokens[start:]
        for n in (k, k + 1, k - 1):
            if n <= 0 or n > len(tokens):
                continue
            first_n_text = " ".join(tokens[:n])
            cand_norm = "".join(normalize(first_n_text))
            ratio = difflib.SequenceMatcher(None, cand_norm, target_str).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                remainder_tokens = tokens[n:]
                raw_remainder = " ".join(remainder_tokens).strip()
                # Strip leading punctuation such as commas or dashes
                best_remainder = re.sub(r"^[^\w\s]+", "", raw_remainder).strip()

    if best_ratio >= threshold and best_remainder is not None:
        return best_remainder
    return None


@dataclass
class WakeResult:
    """Result of successful wake word detection containing remainder of the voice command."""

    remainder: str


class WakeDetector:
    """Listens continuously for wake words, applies cooldowns, and guards privacy by discarding non-wake speech."""

    def __init__(
        self,
        transcriber: Any,
        make_segmenter: Callable[[], Any],
        capture_fn: Callable[..., Any],
        phrase: str = "hey orbit",
        threshold: float = 0.8,
        cooldown_s: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        is_muted: Callable[[], bool] = lambda: False,
        on_error: Callable[[str], None] = print,
    ) -> None:
        self.transcriber = transcriber
        self.make_segmenter = make_segmenter
        self.capture_fn = capture_fn
        self.phrase = phrase
        self.threshold = threshold
        self.cooldown_s = float(cooldown_s)
        self.clock = clock
        self.sleep = sleep
        self.is_muted = is_muted
        self.on_error = on_error
        self.last_trigger_time: float = -999999.0

    def wait_for_wake(self, cancel_event: Optional[Any] = None) -> Optional[WakeResult]:
        """Block until the wake phrase is detected or cancel_event is set.

        Returns WakeResult with command remainder on detection, or None when cancelled.
        """
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None

            # When ORBIT is speaking, do not capture audio to avoid self-wake
            if self.is_muted():
                self.sleep(0.1)
                continue

            try:
                segmenter = self.make_segmenter() if callable(self.make_segmenter) else None
                try:
                    audio = self.capture_fn(segmenter=segmenter, cancel_event=cancel_event)
                except TypeError:
                    try:
                        audio = self.capture_fn(segmenter)
                    except TypeError:
                        try:
                            audio = self.capture_fn()
                        except TypeError:
                            audio = self.capture_fn(cancel_event=cancel_event)

                if audio is None:
                    continue

                transcript_obj = self.transcriber.transcribe(audio)
                raw_text = transcript_obj.text if hasattr(transcript_obj, "text") else str(transcript_obj)

                remainder = match_wake_phrase(raw_text, phrase=self.phrase, threshold=self.threshold)
                if remainder is None:
                    # Privacy preservation: non-matching audio/transcripts are immediately discarded
                    continue

                now = self.clock()
                if (now - self.last_trigger_time) < self.cooldown_s:
                    # Ignore re-triggers inside cooldown interval
                    continue

                self.last_trigger_time = now
                return WakeResult(remainder=remainder)

            except (AudioError, TranscriptionError) as exc:
                self.on_error(f"Wake detection error: {exc}")
                self.sleep(1.0)
                continue
