"""Tests for streaming, cancellation, and delta handling in core/llm.py."""

from unittest.mock import MagicMock, patch
import pytest

from core.llm import (
    GeminiProvider,
    LLMError,
    LLMResponse,
    MockProvider,
)


class DummyPart:
    def __init__(self, text=None, function_call=None, thought_signature=None):
        self.text = text
        self.function_call = function_call
        self.thought_signature = thought_signature


class DummyCandidate:
    def __init__(self, parts):
        self.content = type("Content", (), {"parts": parts})()


class DummyChunk:
    def __init__(self, text=None, parts=None, function_calls=None):
        self.text = text
        if parts is not None:
            self.candidates = [DummyCandidate(parts)]
        else:
            self.candidates = []
        self.function_calls = function_calls or []


def test_stream_text_pieces_reach_on_delta_in_order_and_full_text_returned():
    """Streaming chunks deliver deltas in order, and generate() returns complete text."""
    provider = GeminiProvider(api_key="dummy", model_name="gemini-3.8-flash")
    chunks = [
        DummyChunk(text="The "),
        DummyChunk(text="quick "),
        DummyChunk(text="brown "),
        DummyChunk(text="fox"),
    ]
    provider.client.models.generate_content_stream = MagicMock(return_value=chunks)

    deltas: list[str] = []
    response = provider.generate(
        messages=[{"role": "user", "content": "Tell me a story"}],
        on_delta=deltas.append,
    )

    assert deltas == ["The ", "quick ", "brown ", "fox"]
    assert response.text == "The quick brown fox"
    assert response.interrupted is False
    assert response.tool_calls == []


def test_stream_should_stop_halts_early_with_interrupted():
    """Setting should_stop halts stream iteration, closes stream, and marks response interrupted."""
    provider = GeminiProvider(api_key="dummy", model_name="gemini-3.8-flash")

    deltas: list[str] = []
    stop_flag = False

    def on_delta(piece: str):
        nonlocal stop_flag
        deltas.append(piece)
        if "second" in piece:
            stop_flag = True

    stream_chunks = [
        DummyChunk(text="first "),
        DummyChunk(text="second "),
        DummyChunk(text="third "),
    ]
    mock_stream = MagicMock()
    mock_stream.__iter__ = MagicMock(return_value=iter(stream_chunks))
    mock_stream.close = MagicMock()

    provider.client.models.generate_content_stream = MagicMock(return_value=mock_stream)

    response = provider.generate(
        messages=[{"role": "user", "content": "Count"}],
        on_delta=on_delta,
        should_stop=lambda: stop_flag,
    )

    assert response.interrupted is True
    assert response.text == "first second "
    assert response.tool_calls == []
    mock_stream.close.assert_called_once()


def test_stream_function_call_becomes_tool_call():
    """Function calls delivered in streaming chunks are properly parsed into ToolCall objects."""
    provider = GeminiProvider(api_key="dummy", model_name="gemini-3.8-flash")

    mock_fc = MagicMock()
    mock_fc.name = "get_weather"
    mock_fc.args = {"city": "Berlin"}
    mock_fc.id = "fn_42"

    part = DummyPart(function_call=mock_fc, thought_signature="sig_123")
    chunks = [
        DummyChunk(parts=[part]),
    ]
    provider.client.models.generate_content_stream = MagicMock(return_value=chunks)

    deltas: list[str] = []
    response = provider.generate(
        messages=[{"role": "user", "content": "Weather in Berlin"}],
        on_delta=deltas.append,
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "get_weather"
    assert response.tool_calls[0].args == {"city": "Berlin"}
    assert response.tool_calls[0].id == "fn_42"
    assert response.tool_calls[0].thought_signature == "sig_123"
    assert response.interrupted is False


def test_stream_on_delta_raises_does_not_break():
    """Exceptions raised inside on_delta are caught and do not interrupt generation."""
    provider = GeminiProvider(api_key="dummy", model_name="gemini-3.8-flash")

    def broken_delta(piece: str):
        raise ValueError("Callback exploded")

    chunks = [
        DummyChunk(text="Safe "),
        DummyChunk(text="output"),
    ]
    provider.client.models.generate_content_stream = MagicMock(return_value=chunks)

    response = provider.generate(
        messages=[{"role": "user", "content": "Hi"}],
        on_delta=broken_delta,
    )

    assert response.text == "Safe output"
    assert response.interrupted is False


def test_failure_before_delta_is_retried_once_and_succeeds():
    """A transient server error before any delta has been delivered is retried and succeeds."""
    provider = GeminiProvider(api_key="dummy", model_name="gemini-3.8-flash")

    success_chunks = [DummyChunk(text="Recovered text")]
    provider.client.models.generate_content_stream = MagicMock(
        side_effect=[
            RuntimeError("503 Service Unavailable"),
            success_chunks,
        ]
    )

    deltas: list[str] = []
    with patch("time.sleep") as mock_sleep:
        response = provider.generate(
            messages=[{"role": "user", "content": "Hello"}],
            on_delta=deltas.append,
        )

    assert response.text == "Recovered text"
    assert deltas == ["Recovered text"]
    assert provider.client.models.generate_content_stream.call_count == 2
    mock_sleep.assert_called_once()


def test_failure_after_delta_is_not_retried():
    """A failure occurring after at least one delta is delivered is NOT retried to avoid repeat audio."""
    provider = GeminiProvider(api_key="dummy", model_name="gemini-3.8-flash")

    def stream_with_midway_crash():
        yield DummyChunk(text="Heard by user. ")
        raise RuntimeError("503 Network drop mid-stream")

    provider.client.models.generate_content_stream = MagicMock(
        side_effect=[
            stream_with_midway_crash(),
            [DummyChunk(text="Should never run")],
        ]
    )

    deltas: list[str] = []
    with patch("time.sleep") as mock_sleep:
        with pytest.raises(LLMError, match="503 Network drop mid-stream"):
            provider.generate(
                messages=[{"role": "user", "content": "Hello"}],
                on_delta=deltas.append,
            )

    # User heard first chunk
    assert deltas == ["Heard by user. "]
    # Must NOT retry
    assert provider.client.models.generate_content_stream.call_count == 1
    mock_sleep.assert_not_called()


def test_mock_provider_streaming_and_stop():
    """MockProvider delivers text in word pieces to on_delta and honors should_stop."""
    mock = MockProvider([LLMResponse(text="One two three four")])

    deltas: list[str] = []
    stop_after_two = False

    def handle_delta(delta: str):
        nonlocal stop_after_two
        deltas.append(delta)
        if "two" in delta:
            stop_after_two = True

    resp = mock.generate(
        messages=[{"role": "user", "content": "hi"}],
        on_delta=handle_delta,
        should_stop=lambda: stop_after_two,
    )

    assert resp.interrupted is True
    assert "three" not in resp.text
    assert deltas == ["One ", "two "]
