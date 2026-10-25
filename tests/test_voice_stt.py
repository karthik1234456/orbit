"""Tests for voice/stt.py without downloading any Whisper models."""

from typing import Any
import numpy as np
import pytest

from voice.stt import (
    FakeTranscriber,
    TranscriptionError,
    WhisperTranscriber,
    filter_hallucinations,
)


class DummySegment:
    def __init__(self, text: str):
        self.text = text


class DummyInfo:
    def __init__(self, language: str = "en"):
        self.language = language


class MockWhisperModel:
    load_count = 0

    def __init__(self, model_name: str, device: str = "cpu", compute_type: str = "int8"):
        MockWhisperModel.load_count += 1
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.last_kwargs: dict[str, Any] = {}

    def transcribe(self, audio: np.ndarray, **kwargs) -> tuple[list[DummySegment], DummyInfo]:
        self.last_kwargs = kwargs
        return [DummySegment("Hello"), DummySegment("world")], DummyInfo(language=kwargs.get("language") or "en")


def test_filter_hallucinations():
    """Verify that phantom noise phrases and fillers are removed while legitimate utterances are kept."""
    # Phantom phrases removed
    assert filter_hallucinations("Thank you.") == ""
    assert filter_hallucinations("Thanks for watching!") == ""
    assert filter_hallucinations("you") == ""
    assert filter_hallucinations("Bye.") == ""
    assert filter_hallucinations("Subtitles by Community") == ""
    assert filter_hallucinations("Closed captions by Amara") == ""
    assert filter_hallucinations("Um, uh, hmm.") == ""
    assert filter_hallucinations("   ") == ""
    assert filter_hallucinations("") == ""

    # Real sentences kept
    assert filter_hallucinations("thank you for the file") == "thank you for the file"
    assert filter_hallucinations("Thank you for your help.") == "Thank you for your help."
    assert filter_hallucinations("Open the browser and find recipes.") == "Open the browser and find recipes."


def test_fake_transcriber_returns_scripted_values():
    """FakeTranscriber returns scripted texts in sequence, then falls back to default_text."""
    transcriber = FakeTranscriber(
        scripted=["First transcription", "Second transcription"],
        default_text="Fallback text",
        default_language="en",
    )

    audio = np.zeros(16000, dtype=np.float32)  # 1.0 second duration

    t1 = transcriber.transcribe(audio, sample_rate=16000)
    assert t1.text == "First transcription"
    assert t1.duration_s == 1.0
    assert t1.language == "en"

    t2 = transcriber.transcribe(audio, sample_rate=16000)
    assert t2.text == "Second transcription"

    t3 = transcriber.transcribe(audio, sample_rate=16000)
    assert t3.text == "Fallback text"


def test_whisper_transcriber_lazy_loading_and_execution(monkeypatch: pytest.MonkeyPatch):
    """The model is NOT loaded at construction, is loaded exactly once across multiple calls, and passes args."""
    MockWhisperModel.load_count = 0
    monkeypatch.setattr("voice.stt.WhisperModel", MockWhisperModel)

    # 1. Assert model is NOT loaded at construction
    transcriber = WhisperTranscriber(model_name="base.en", language="en")
    assert MockWhisperModel.load_count == 0

    # 2. First transcribe call loads model and returns joined text
    audio = np.zeros(16000, dtype=np.float32)  # 1.0 s
    result1 = transcriber.transcribe(audio, sample_rate=16000)
    assert MockWhisperModel.load_count == 1
    assert result1.text == "Hello world"
    assert result1.language == "en"
    assert result1.duration_s == 1.0

    # Verify parameters passed to transcribe
    model_instance = transcriber._model
    assert model_instance.last_kwargs.get("language") == "en"
    assert model_instance.last_kwargs.get("vad_filter") is True
    assert model_instance.last_kwargs.get("beam_size") == 1
    assert model_instance.last_kwargs.get("condition_on_previous_text") is False

    # 3. Second call reuses the already loaded model (still loaded count == 1)
    result2 = transcriber.transcribe(audio, sample_rate=16000)
    assert MockWhisperModel.load_count == 1
    assert result2.text == "Hello world"


def test_whisper_transcriber_short_audio_rejected():
    """Audio shorter than 0.3s raises TranscriptionError."""
    transcriber = WhisperTranscriber(model_name="base.en")
    # 0.2 seconds at 16kHz is 3200 samples
    short_audio = np.zeros(3200, dtype=np.float32)

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.transcribe(short_audio, sample_rate=16000)
    assert "too short" in str(exc_info.value).lower()


def test_whisper_transcriber_long_audio_rejected():
    """Audio longer than 60s raises TranscriptionError."""
    transcriber = WhisperTranscriber(model_name="base.en")
    # 61 seconds at 16kHz
    long_audio = np.zeros(16000 * 61, dtype=np.float32)

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.transcribe(long_audio, sample_rate=16000)
    assert "too long" in str(exc_info.value).lower()


def test_model_initialization_failure_raises_transcription_error(monkeypatch: pytest.MonkeyPatch):
    """If model loading fails, a TranscriptionError with friendly download advice is raised."""
    class FailingLoadModel:
        def __init__(self, *args, **kwargs):
            raise OSError("No network connection to HuggingFace")

    monkeypatch.setattr("voice.stt.WhisperModel", FailingLoadModel)

    transcriber = WhisperTranscriber(model_name="base.en")
    audio = np.zeros(16000, dtype=np.float32)

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.transcribe(audio, sample_rate=16000)

    err_msg = str(exc_info.value)
    assert "Failed to load Whisper model" in err_msg
    assert "internet" in err_msg


def test_model_inference_failure_raises_transcription_error(monkeypatch: pytest.MonkeyPatch):
    """If model transcribe execution fails, a TranscriptionError is raised."""
    class FailingInferenceModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio: np.ndarray, **kwargs):
            raise RuntimeError("CUDA device error")

    monkeypatch.setattr("voice.stt.WhisperModel", FailingInferenceModel)

    transcriber = WhisperTranscriber(model_name="base.en")
    audio = np.zeros(16000, dtype=np.float32)

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.transcribe(audio, sample_rate=16000)

    assert "Transcription failed" in str(exc_info.value)


def test_whisper_transcriber_offline_mode_passes_local_files_only(monkeypatch: pytest.MonkeyPatch):
    """When ORBIT_OFFLINE is True, WhisperModel is created with local_files_only=True."""
    from config import settings

    monkeypatch.setattr(settings, "ORBIT_OFFLINE", True)

    captured_kwargs: dict[str, Any] = {}

    class OfflineModel:
        def __init__(self, model_name: str, device: str = "cpu", compute_type: str = "int8", **kwargs):
            captured_kwargs.update(kwargs)
            captured_kwargs["model_name"] = model_name

        def transcribe(self, audio: np.ndarray, **kwargs):
            return [DummySegment("Offline test")], DummyInfo()

    monkeypatch.setattr("voice.stt.WhisperModel", OfflineModel)

    transcriber = WhisperTranscriber(model_name="base.en")
    audio = np.zeros(16000, dtype=np.float32)
    trans = transcriber.transcribe(audio, sample_rate=16000)

    assert trans.text == "Offline test"
    assert captured_kwargs.get("local_files_only") is True
    assert captured_kwargs.get("model_name") == "base.en"


def test_whisper_transcriber_offline_missing_model_raises_friendly_error(monkeypatch: pytest.MonkeyPatch):
    """When ORBIT_OFFLINE is True and model is missing, friendly TranscriptionError with model name is raised."""
    from config import settings

    monkeypatch.setattr(settings, "ORBIT_OFFLINE", True)

    class MissingModel:
        def __init__(self, model_name: str, **kwargs):
            raise FileNotFoundError(f"Model {model_name} not found in local cache")

    monkeypatch.setattr("voice.stt.WhisperModel", MissingModel)

    # Standard model
    transcriber = WhisperTranscriber(model_name="base.en")
    audio = np.zeros(16000, dtype=np.float32)

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.transcribe(audio, sample_rate=16000)

    err = str(exc_info.value)
    assert "base.en" in err
    assert "downloaded once while online" in err

    # Wake-word model
    wake_transcriber = WhisperTranscriber(model_name="tiny.en")
    with pytest.raises(TranscriptionError) as exc_info2:
        wake_transcriber.transcribe(audio, sample_rate=16000)

    err2 = str(exc_info2.value)
    assert "tiny.en" in err2
    assert "downloaded once while online" in err2
