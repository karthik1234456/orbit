from unittest.mock import MagicMock, patch
import pytest
from core.llm import (
    GeminiProvider,
    LLMError,
    LLMResponse,
    MockProvider,
    QuotaExceededError,
    ToolCall,
)
from tools.registry import Tool


def dummy_func(x: int) -> int:
    return x * 2


@pytest.fixture
def sample_tool() -> Tool:
    return Tool(
        name="test_tool",
        description="A test tool description",
        parameters={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of items"}
            },
            "required": ["count"],
        },
        func=dummy_func,
    )


def test_tool_call_and_llm_response_dataclasses():
    """Verify ToolCall and LLMResponse fields and defaults."""
    tc = ToolCall(name="calculator", args={"expr": "1+1"}, id="call_123")
    assert tc.name == "calculator"
    assert tc.args == {"expr": "1+1"}
    assert tc.id == "call_123"

    # Default id is None
    tc_no_id = ToolCall(name="time", args={})
    assert tc_no_id.id is None

    # LLMResponse with text only
    resp = LLMResponse(text="Hello")
    assert resp.text == "Hello"
    assert resp.tool_calls == []

    # LLMResponse with tool calls
    resp_with_tools = LLMResponse(text="", tool_calls=[tc])
    assert len(resp_with_tools.tool_calls) == 1
    assert resp_with_tools.tool_calls[0].name == "calculator"


def test_convert_tools_to_gemini(sample_tool: Tool):
    """Verify Tool objects are converted into Gemini Tool with FunctionDeclarations."""
    # Instantiating GeminiProvider with dummy key so settings check is bypassed
    provider = GeminiProvider(api_key="dummy_key", model_name="dummy_model")
    gemini_tools = provider._convert_tools([sample_tool])

    assert gemini_tools is not None
    assert len(gemini_tools) == 1
    func_decls = gemini_tools[0].function_declarations
    assert len(func_decls) == 1
    assert func_decls[0].name == "test_tool"
    assert func_decls[0].description == "A test tool description"

    # Empty tools list returns None
    assert provider._convert_tools(None) is None
    assert provider._convert_tools([]) is None


def test_convert_messages_to_gemini():
    """Verify conversion of user, assistant, and tool messages to Gemini contents."""
    provider = GeminiProvider(api_key="dummy_key", model_name="dummy_model")

    messages = [
        {"role": "user", "content": "What is 2+2?"},
        {
            "role": "assistant",
            "content": "Let me calculate.",
            "tool_calls": [ToolCall(name="calculate", args={"expression": "2+2"}, id="call_1")],
        },
        {"role": "tool", "name": "calculate", "content": "4"},
        {"role": "assistant", "content": "The result is 4."},
    ]

    contents = provider._convert_messages(messages)
    assert len(contents) == 4

    # 1. User message
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "What is 2+2?"

    # 2. Assistant message with both text and function call
    assert contents[1].role == "model"
    assert len(contents[1].parts) == 2
    assert contents[1].parts[0].text == "Let me calculate."
    assert contents[1].parts[1].function_call.name == "calculate"
    assert contents[1].parts[1].function_call.args == {"expression": "2+2"}

    # 3. Tool response message (Gemini API requires role 'user' for function responses)
    assert contents[2].role == "user"
    assert contents[2].parts[0].function_response.name == "calculate"
    assert contents[2].parts[0].function_response.response == {"result": "4"}

    # 4. Final assistant text
    assert contents[3].role == "model"
    assert contents[3].parts[0].text == "The result is 4."


def test_generate_parses_function_calls_and_disables_afc():
    """Verify generate disables automatic function calling and parses ToolCalls from response."""
    provider = GeminiProvider(api_key="dummy_key", model_name="dummy_model")

    # Mock Gemini client models.generate_content
    mock_fc = MagicMock()
    mock_fc.name = "calculate"
    mock_fc.args = {"expression": "10 * 5"}
    mock_fc.id = "fn_call_99"

    mock_response = MagicMock()
    mock_response.text = "Computing result"
    mock_response.candidates = []
    mock_response.function_calls = [mock_fc]

    provider.client.models.generate_content = MagicMock(return_value=mock_response)

    tool = Tool(
        name="calculate",
        description="calculate",
        parameters={"type": "object", "properties": {}},
        func=lambda: None,
    )

    response = provider.generate(
        messages=[{"role": "user", "content": "10 * 5"}],
        tools=[tool],
    )

    assert response.text == "Computing result"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "calculate"
    assert response.tool_calls[0].args == {"expression": "10 * 5"}
    assert response.tool_calls[0].id == "fn_call_99"

    # Verify that generate_content was called with automatic_function_calling disabled
    call_kwargs = provider.client.models.generate_content.call_args.kwargs
    config = call_kwargs.get("config")
    assert config is not None
    assert config.automatic_function_calling.disable is True


def test_generate_wraps_errors():
    """Verify Gemini API errors or network exceptions are wrapped in LLMError."""
    provider = GeminiProvider(api_key="dummy_key", model_name="dummy_model")
    provider.client.models.generate_content = MagicMock(side_effect=RuntimeError("Connection lost"))

    with pytest.raises(LLMError, match="Failed to generate response: Connection lost"):
        provider.generate(messages=[{"role": "user", "content": "Hi"}])


def test_429_becomes_quota_exceeded_error():
    """Verify HTTP 429 / RESOURCE_EXHAUSTED errors raise QuotaExceededError with friendly message."""
    provider = GeminiProvider(api_key="dummy_key", model_name="gemini-3.8-flash")
    raw_error_dump = "Detailed gRPC traceback: RESOURCE_EXHAUSTED 429 {quota_metric_limit: 15}"
    provider.client.models.generate_content = MagicMock(
        side_effect=RuntimeError(f"429 Resource exhausted: {raw_error_dump}")
    )

    with patch("time.sleep"):
        with pytest.raises(QuotaExceededError) as excinfo:
            provider.generate(messages=[{"role": "user", "content": "Hello"}])

    msg = str(excinfo.value)
    # Mentions the model name
    assert "gemini-3.8-flash" in msg
    # Mentions hint to switch MODEL_NAME or wait
    assert "switch model_name" in msg.lower() or "wait" in msg.lower()
    # Does NOT dump raw API traceback dump
    assert raw_error_dump not in msg
    # Is a subclass of LLMError
    assert isinstance(excinfo.value, LLMError)


def test_per_minute_429_is_retried_and_then_succeeds():
    """Verify transient per-minute 429 errors retry with backoff (2s, then 4s) and succeed."""
    provider = GeminiProvider(api_key="dummy_key", model_name="gemini-3.8-flash")

    mock_resp = MagicMock()
    mock_resp.text = "Success after retry"
    mock_resp.candidates = []
    mock_resp.function_calls = []

    # First call raises 429, second succeeds
    provider.client.models.generate_content = MagicMock(
        side_effect=[
            RuntimeError("429 RESOURCE_EXHAUSTED: Rate limit per minute exceeded. Please retry in 20s"),
            mock_resp,
        ]
    )

    with patch("time.sleep") as mock_sleep:
        resp = provider.generate(messages=[{"role": "user", "content": "Hello"}])

    assert resp.text == "Success after retry"
    assert provider.client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once_with(2.0)


def test_daily_limit_429_is_not_retried():
    """Verify daily limit 429 quota exhaustion is NOT retried and immediately raises QuotaExceededError."""
    provider = GeminiProvider(api_key="dummy_key", model_name="gemini-3.8-flash")
    provider.client.models.generate_content = MagicMock(
        side_effect=RuntimeError("429 RESOURCE_EXHAUSTED: Daily quota limit exceeded for free tier")
    )

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(QuotaExceededError) as excinfo:
            provider.generate(messages=[{"role": "user", "content": "Hello"}])

    # Must NOT call time.sleep
    mock_sleep.assert_not_called()
    assert provider.client.models.generate_content.call_count == 1
    assert "gemini-3.8-flash" in str(excinfo.value)


def test_mock_provider_returns_tool_call_for_time():
    """Verify MockProvider returns a fake ToolCall for queries containing 'time'."""
    mock = MockProvider()

    # Query with "time" triggers get_current_time tool call
    resp = mock.generate(messages=[{"role": "user", "content": "What is the current time in Tokyo?"}])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_current_time"
    assert resp.tool_calls[0].args == {}

    # Follow-up tool response returns acknowledging text
    followup_resp = mock.generate(
        messages=[
            {"role": "user", "content": "What is the current time in Tokyo?"},
            {"role": "assistant", "content": "", "tool_calls": resp.tool_calls},
            {"role": "tool", "name": "get_current_time", "content": "3:00 PM"},
        ]
    )
    assert "3:00 PM" in followup_resp.text

    # Query without "time" returns friendly mock text without tool calls
    normal_resp = mock.generate(messages=[{"role": "user", "content": "Calculate 2 + 2"}])
    assert len(normal_resp.tool_calls) == 0
    assert "Mock" in normal_resp.text


def test_mock_provider_returns_tool_call_for_apps():
    """Verify MockProvider triggers open_application and close_application."""
    mock = MockProvider()

    # Query with "open notepad"
    resp = mock.generate(messages=[{"role": "user", "content": "open notepad"}])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "open_application"
    assert resp.tool_calls[0].args == {"name": "notepad"}

    # Query with "close notepad"
    close_resp = mock.generate(messages=[{"role": "user", "content": "close notepad"}])
    assert len(close_resp.tool_calls) == 1
    assert close_resp.tool_calls[0].name == "close_application"
    assert close_resp.tool_calls[0].args == {"name": "notepad"}

    # Query with "open calc"
    calc_resp = mock.generate(messages=[{"role": "user", "content": "launch calc"}])
    assert len(calc_resp.tool_calls) == 1
    assert calc_resp.tool_calls[0].name == "open_application"
    assert calc_resp.tool_calls[0].args == {"name": "calculator"}

    # Query with "list files"
    files_resp = mock.generate(messages=[{"role": "user", "content": "list files"}])
    assert len(files_resp.tool_calls) == 1
    assert files_resp.tool_calls[0].name == "list_files"

