"""Speech-to-text (STT) module with Whisper transcriber, hallucination filtering, and test fakes."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import inspect
import re
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
    ) -> None:
        self.model_name = model_name
        self.language = language
        self.device = device
        self.compute_type = compute_type
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

        try:
            segments, info = model.transcribe(
                audio,
                language=self.language or None,
                vad_filter=True,
                beam_size=1,
                condition_on_previous_text=False,
            )
            # Join segment texts
            pieces = [seg.text.strip() for seg in segments if hasattr(seg, "text") and seg.text.strip()]
            raw_text = " ".join(pieces).strip()
            detected_language = getattr(info, "language", self.language) or self.language
        except Exception as exc:
            raise TranscriptionError(f"Transcription failed: {exc}") from exc

        clean_text = filter_hallucinations(raw_text)
        return Transcript(text=clean_text, language=detected_language, duration_s=round(duration_s, 2))


class FakeTranscriber(Transcriber):
    """Scripted transcriber returning predefined responses in order."""

    def __init__(
        self,
        scripted: Optional[list[Union[str, Transcript]]] = None,
        default_text: str = "",
        default_language: Optional[str] = "en",
    ) -> None:
        self.scripted = list(scripted) if scripted is not None else []
        self.default_text = default_text
        self.default_language = default_language
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
            return Transcript(text=str(item), language=self.default_language, duration_s=round(duration_s, 2))

        return Transcript(text=self.default_text, language=self.default_language, duration_s=round(duration_s, 2))
