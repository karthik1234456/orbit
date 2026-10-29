"""Speech-to-text (STT) module with Whisper transcriber, hallucination filtering, and test fakes."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import inspect
import re
import time
from typing import Any, Optional, Union
import numpy as np

from config import settings

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None


@dataclass
class Transcript:
    """Represents a speech transcription result."""

    text: str
    language: Optional[str]
    duration_s: float
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    decode_seconds: Optional[float] = None


class TranscriptionError(Exception):
    """Raised when speech transcription fails or audio input is invalid."""


class Transcriber(ABC):
    """Abstract base class for audio transcription engines."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        """Transcribe a 1-D numpy float32 audio array in [-1, 1] into a Transcript."""
        pass


def filter_hallucinations(text: str) -> str:
    """Filter out common Whisper silence/noise phantom hallucinations and fillers.

    Returns an empty string if the text is empty, only filler words, or matches
    known phantom phrases produced on background noise. Otherwise returns text unchanged.
    """
    if not text or not text.strip():
        return ""

    # Normalize: lowercase, strip punctuation, collapse whitespace
    norm = text.lower()
    norm = re.sub(r"[^\w\s]", "", norm)
    norm = re.sub(r"\s+", " ", norm).strip()

    if not norm:
        return ""

    # Filler words commonly emitted on pauses or breathing
    filler_words = {"uh", "um", "hmm", "hm", "ah", "er", "oh", "mm", "mmm"}
    words = norm.split()
    if all(w in filler_words for w in words):
        return ""

    # Known exact phantom hallucination phrases produced by Whisper on silence/static
    phantom_phrases = {
        "thank you",
        "thanks",
        "thanks for watching",
        "thank you for watching",
        "thanks for listening",
        "thank you for listening",
        "you",
        "bye",
        "goodbye",
        "bye bye",
        "subtitles by",
        "subtitles",
        "subscribe",
        "please subscribe",
        "like and subscribe",
        "thank you very much",
    }

    if norm in phantom_phrases:
        return ""

    # Subtitle attribution prefixes
    if (
        norm.startswith("subtitles by")
        or norm.startswith("closed captions by")
        or norm.startswith("transcribed by")
    ):
        return ""

    return text


class WhisperTranscriber(Transcriber):
    """Transcriber using faster-whisper with lazy model loading and hallucination filtering."""

    def __init__(
        self,
        model_name: str = "base.en",
        language: Optional[str] = "en",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: Optional[int] = None,
        initial_prompt: Optional[str] = None,
    ) -> None:
        self.model_name = model_name
        self.language = language
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt
        self._model: Any = None

    def _load_model(self) -> Any:
        """Lazy-load the WhisperModel on first transcribe call."""
        if self._model is not None:
            return self._model

        model_cls = WhisperModel
        if model_cls is None:
            try:
                import faster_whisper

                model_cls = getattr(faster_whisper, "WhisperModel", None)
            except ImportError:
                pass

        if model_cls is None:
            raise TranscriptionError(
                "faster-whisper is not installed. Please install it with 'pip install faster-whisper'. "
                "Note that the first run downloads the model and needs an internet connection."
            )

        kwargs: dict[str, Any] = {
            "device": self.device,
            "compute_type": self.compute_type,
        }

        is_offline = bool(getattr(settings, "ORBIT_OFFLINE", False))
        if is_offline:
            try:
                sig = inspect.signature(model_cls.__init__)
                if "local_files_only" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    kwargs["local_files_only"] = True
            except Exception:
                pass

        try:
            self._model = model_cls(
                self.model_name,
                **kwargs,
            )
            return self._model
        except Exception as exc:
            if is_offline:
                raise TranscriptionError(
                    f"Whisper model '{self.model_name}' is not found locally. "
                    f"The model '{self.model_name}' must be downloaded once while online before running in offline mode."
                ) from exc
            raise TranscriptionError(
                f"Failed to load Whisper model '{self.model_name}' on {self.device}. "
                f"Note that the first run downloads the model and needs an internet connection. Error: {exc}"
            ) from exc

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        """Transcribe audio array using faster-whisper."""
        if sample_rate <= 0:
            raise TranscriptionError(f"Invalid sample rate: {sample_rate}")

        duration_s = float(len(audio)) / float(sample_rate)
        if duration_s < 0.3:
            raise TranscriptionError(f"Audio too short ({duration_s:.2f}s). Minimum is 0.3s.")
        if duration_s > 60.0:
            raise TranscriptionError(f"Audio too long ({duration_s:.2f}s). Maximum is 60.0s.")

        model = self._load_model()

        beam = self.beam_size
        if beam is None:
            beam = int(getattr(settings, "STT_BEAM", 1))
        beam = max(1, min(5, beam))

        transcribe_kwargs: dict[str, Any] = {
            "language": self.language or None,
            "vad_filter": True,
            "beam_size": beam,
            "condition_on_previous_text": False,
        }

        prompt_enabled = bool(getattr(settings, "STT_PROMPT", 0))
        prompt_text = self.initial_prompt
        if prompt_enabled and prompt_text is None:
            try:
                from voice.vocab import build_vocab_prompt
                prompt_text = build_vocab_prompt()
            except Exception:
                prompt_text = None

        if prompt_text:
            sig = inspect.signature(model.transcribe)
            if "hotwords" in sig.parameters:
                transcribe_kwargs["hotwords"] = prompt_text
            elif "initial_prompt" in sig.parameters:
                transcribe_kwargs["initial_prompt"] = prompt_text

        t_start = time.perf_counter()
        try:
            segments, info = model.transcribe(audio, **transcribe_kwargs)
            seg_list = list(segments)
            decode_seconds = time.perf_counter() - t_start
            pieces = [seg.text.strip() for seg in seg_list if hasattr(seg, "text") and seg.text.strip()]
            raw_text = " ".join(pieces).strip()
            detected_language = getattr(info, "language", self.language) or self.language

            logprobs = [getattr(seg, "avg_logprob", None) for seg in seg_list if getattr(seg, "avg_logprob", None) is not None]
            no_speech_probs = [getattr(seg, "no_speech_prob", None) for seg in seg_list if getattr(seg, "no_speech_prob", None) is not None]
            avg_logprob = float(np.mean(logprobs)) if logprobs else None
            no_speech_prob = float(np.max(no_speech_probs)) if no_speech_probs else None
        except Exception as exc:
            raise TranscriptionError(f"Transcription failed: {exc}") from exc

        clean_text = filter_hallucinations(raw_text)
        return Transcript(
            text=clean_text,
            language=detected_language,
            duration_s=round(duration_s, 2),
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            decode_seconds=round(decode_seconds, 2),
        )


class FakeTranscriber(Transcriber):
    """Scripted transcriber returning predefined responses in order."""

    def __init__(
        self,
        scripted: Optional[list[Union[str, Transcript]]] = None,
        default_text: str = "",
        default_language: Optional[str] = "en",
        avg_logprob: Optional[float] = None,
        no_speech_prob: Optional[float] = None,
        decode_seconds: Optional[float] = None,
    ) -> None:
        self.scripted = list(scripted) if scripted is not None else []
        self.default_text = default_text
        self.default_language = default_language
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.decode_seconds = decode_seconds
        self.calls: list[tuple[np.ndarray, int]] = []

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        self.calls.append((audio, sample_rate))
        duration_s = float(len(audio)) / float(sample_rate) if sample_rate > 0 else 0.0

        if self.scripted:
            item = self.scripted.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, Transcript):
                return item
            return Transcript(
                text=str(item),
                language=self.default_language,
                duration_s=round(duration_s, 2),
                avg_logprob=self.avg_logprob,
                no_speech_prob=self.no_speech_prob,
                decode_seconds=self.decode_seconds,
            )

        return Transcript(
            text=self.default_text,
            language=self.default_language,
            duration_s=round(duration_s, 2),
            avg_logprob=self.avg_logprob,
            no_speech_prob=self.no_speech_prob,
            decode_seconds=self.decode_seconds,
        )
