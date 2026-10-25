"""Tests for core/ollama_provider.py local LLM provider implementation."""

import json as json_lib
from unittest.mock import patch

import pytest
import requests

from core.llm import ToolCall
from core.offline import disable_network_guard, enable_network_guard
from core.ollama_provider import (
    ModelNotFound,
    OllamaError,
    OllamaProvider,
    OllamaUnavailable,
    list_models,
    server_version,
)
from tools.registry import Tool


@pytest.fixture(autouse=True)
def guard_network():
    """Ensure network guard is active for the entire test module so no real connection can occur."""
    enable_network_guard()
    yield
    disable_network_guard()


class FakeResponse:
    """Mock requests.Response for testing streaming NDJSON endpoints."""

    def __init__(
        self,
        lines: list[str] | None = None,
        status_code: int = 200,
        text: str = "",
        json_data: dict | None = None,
    ) -> None:
        self._lines = lines or []
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.closed = False

    def iter_lines(self):
        for line in self._lines:
            if self.closed:
                break
            if isinstance(line, str):
                yield line.encode("utf-8")
            else:
                yield line

    def json(self):
        if self._json_data is not None:
            return self._json_data
        if self.text:
            return json_lib.loads(self.text)
        return {}

    def close(self):
        self.closed = True


def test_text_deltas_in_order_and_full_text():
    """Verify text deltas arrive in order and full concatenated text is returned."""
    lines = [
        json_lib.dumps({"message": {"content": "Hello "}}),
        json_lib.dumps({"message": {"content": "world!"}}),
        json_lib.dumps({"done": True}),
    ]

    def fake_post(url, json=None, stream=False, timeout=None):
        return FakeResponse(lines=lines)

    provider = OllamaProvider(http_post=fake_post)
    deltas = []
    res = provider.generate(
        messages=[{"role": "user", "content": "Hi"}],
        on_delta=lambda d: deltas.append(d),
    )

    assert deltas == ["Hello ", "world!"]
    assert res.text == "Hello world!"
    assert res.tool_calls == []
    assert res.interrupted is False


def test_tool_calls_chunk_parsing_and_generated_id():
    """Verify tool_calls chunks are collected into ToolCall objects with generated IDs."""
    lines = [
        json_lib.dumps({
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "create_file",
                            "arguments": {"path": "test.txt", "content": "hello"},
                        }
                    }
                ],
            }
        }),
        json_lib.dumps({"done": True}),
    ]

    def fake_post(url, json=None, stream=False, timeout=None):
        return FakeResponse(lines=lines)

    provider = OllamaProvider(http_post=fake_post)
    res = provider.generate(messages=[{"role": "user", "content": "create file"}])

    assert len(res.tool_calls) == 1
    tc = res.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "create_file"
    assert tc.args == {"path": "test.txt", "content": "hello"}


def test_string_arguments_parsed_and_invalid_json_does_not_crash():
    """Verify JSON string arguments are parsed and invalid JSON strings become an error marker dict."""
    lines = [
        json_lib.dumps({
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "tool_valid",
                            "arguments": '{"valid_key": "valid_val"}',
                        }
                    },
                    {
                        "function": {
                            "name": "tool_invalid",
                            "arguments": "{not valid json}",
                        }
                    },
                ],
            }
        }),
        json_lib.dumps({"done": True}),
    ]

    def fake_post(url, json=None, stream=False, timeout=None):
        return FakeResponse(lines=lines)

    provider = OllamaProvider(http_post=fake_post)
    res = provider.generate(messages=[{"role": "user", "content": "test"}])

    assert len(res.tool_calls) == 2
    tc1, tc2 = res.tool_calls
    assert tc1.args == {"valid_key": "valid_val"}
    assert "_error" in tc2.args or "_invalid_arguments_json" in tc2.args


def test_thinking_field_and_think_blocks_never_reach_text_or_delta():
    """Verify that 'thinking' fields and <think>...</think> blocks never reach on_delta or final text."""
    lines = [
        json_lib.dumps({
            "message": {
                "thinking": "private thinking model scratchpad",
                "content": "<think>reasoning step 1\nreasoning step 2</think>Visible answer",
            }
        }),
        json_lib.dumps({"message": {"content": " to the user."}}),
        json_lib.dumps({"done": True}),
    ]

    deltas = []

    def fake_post(url, json=None, stream=False, timeout=None):
        return FakeResponse(lines=lines)

    provider = OllamaProvider(http_post=fake_post)
    res = provider.generate(
        messages=[{"role": "user", "content": "Why is the sky blue?"}],
        on_delta=lambda d: deltas.append(d),
    )

    combined_deltas = "".join(deltas)
    assert "<think>" not in combined_deltas
    assert "reasoning" not in combined_deltas
    assert "private thinking" not in combined_deltas

    assert "<think>" not in res.text
    assert "reasoning" not in res.text
    assert res.text == "Visible answer to the user."


def test_should_stop_halts_early_with_interrupted():
    """Verify should_stop halting early closes response and returns interrupted=True with empty tool_calls."""
    fake_resp = FakeResponse(lines=[
        json_lib.dumps({"message": {"content": "Part 1 "}}),
        json_lib.dumps({"message": {"content": "Part 2 "}}),
        json_lib.dumps({"done": True}),
    ])

    should_stop_flag = [False]

    def fake_post(url, json=None, stream=False, timeout=None):
        return fake_resp

    def check_stop():
        return should_stop_flag[0]

    def on_delta(chunk):
        # Trigger stop after first chunk
        should_stop_flag[0] = True

    provider = OllamaProvider(http_post=fake_post)
    res = provider.generate(
        messages=[{"role": "user", "content": "Long task"}],
        on_delta=on_delta,
        should_stop=check_stop,
    )

    assert res.interrupted is True
    assert res.tool_calls == []
    assert fake_resp.closed is True
    assert "Part 1" in res.text


def test_request_body_shape():
    """Verify request body converts messages, tools, and options with exact expected schemas."""
    captured_payload = {}

    def fake_post(url, json=None, stream=False, timeout=None):
        captured_payload.update(json or {})
        return FakeResponse(lines=[json_lib.dumps({"done": True})])

    dummy_tool = Tool(
        name="dummy_calc",
        func=lambda x: x,
        description="Calculate something",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        permission="safe",
    )

    provider = OllamaProvider(
        model="test-model:latest",
        num_ctx=4096,
        temperature=0.7,
        seed=12345,
        think=False,
        keep_alive="15m",
        http_post=fake_post,
    )

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Run calculation"},
        {
            "role": "assistant",
            "content": "Running",
            "tool_calls": [ToolCall(id="c1", name="dummy_calc", args={"x": 5})],
        },
        {"role": "tool", "name": "dummy_calc", "content": "25"},
    ]

    provider.generate(messages=messages, tools=[dummy_tool])

    assert captured_payload["model"] == "test-model:latest"
    assert captured_payload["stream"] is True
    assert captured_payload["think"] is False
    assert captured_payload["keep_alive"] == "15m"
    assert captured_payload["options"]["num_ctx"] == 4096
    assert captured_payload["options"]["temperature"] == 0.7
    assert captured_payload["options"]["seed"] == 12345

    # Converted messages check
    c_msgs = captured_payload["messages"]
    assert c_msgs[0] == {"role": "system", "content": "You are a helpful assistant."}
    assert c_msgs[1] == {"role": "user", "content": "Run calculation"}
    assert c_msgs[2]["role"] == "assistant"
    assert c_msgs[2]["tool_calls"] == [{"function": {"name": "dummy_calc", "arguments": {"x": 5}}}]
    assert c_msgs[3] == {"role": "tool", "tool_name": "dummy_calc", "content": "25"}

    # Tools check
    tools_payload = captured_payload["tools"]
    assert len(tools_payload) == 1
    assert tools_payload[0]["type"] == "function"
    assert tools_payload[0]["function"]["name"] == "dummy_calc"
    assert tools_payload[0]["function"]["description"] == "Calculate something"


def test_request_body_omits_tools_when_empty_and_omits_seed_when_unset():
    """Verify tools key is absent when tools is None/empty and seed is absent when None."""
    captured_payload = {}

    def fake_post(url, json=None, stream=False, timeout=None):
        captured_payload.update(json or {})
        return FakeResponse(lines=[json_lib.dumps({"done": True})])

    provider = OllamaProvider(seed=None, http_post=fake_post)
    provider.generate(messages=[{"role": "user", "content": "Hi"}], tools=None)

    assert "tools" not in captured_payload
    assert "seed" not in captured_payload["options"]


def test_non_loopback_host_refused_unless_allow_remote():
    """Verify non-loopback hosts are rejected unless allow_remote=True."""
    with pytest.raises(ValueError) as exc:
        OllamaProvider(host="http://192.168.1.50:11434", allow_remote=False)
    assert "not a permitted loopback address" in str(exc.value)

    with pytest.raises(ValueError) as exc2:
        OllamaProvider(host="http://example.com:11434", allow_remote=False)
    assert "not a permitted loopback address" in str(exc2.value)

    # Allowed loopback hosts
    p_local = OllamaProvider(host="http://localhost:11434", allow_remote=False)
    assert p_local.host == "http://localhost:11434"

    p_ip = OllamaProvider(host="http://127.0.0.1:11434", allow_remote=False)
    assert p_ip.host == "http://127.0.0.1:11434"

    # Explicit allow_remote=True
    p_remote = OllamaProvider(host="http://192.168.1.50:11434", allow_remote=True)
    assert p_remote.host == "http://192.168.1.50:11434"


def test_connection_error_and_timeout_raise_ollama_unavailable():
    """Verify connection errors and timeouts raise OllamaUnavailable."""
    def fake_conn_error(url, json=None, stream=False, timeout=None):
        raise requests.exceptions.ConnectionError("Connection refused")

    provider = OllamaProvider(http_post=fake_conn_error)
    with pytest.raises(OllamaUnavailable) as exc:
        provider.generate(messages=[{"role": "user", "content": "Hi"}])
    assert "Ollama is not running at" in str(exc.value)
    assert "ollama serve" in str(exc.value)

    def fake_timeout(url, json=None, stream=False, timeout=None):
        raise requests.exceptions.Timeout("Read timeout")

    provider_timeout = OllamaProvider(http_post=fake_timeout)
    with pytest.raises(OllamaUnavailable) as exc:
        provider_timeout.generate(messages=[{"role": "user", "content": "Hi"}])
    assert "Ollama is not running at" in str(exc.value)


def test_http_404_and_model_not_found_raise_model_not_found():
    """Verify 404 and model not found responses raise ModelNotFound."""
    def fake_404(url, json=None, stream=False, timeout=None):
        return FakeResponse(status_code=404, text="404 page not found")

    provider = OllamaProvider(model="qwen3:8b", http_post=fake_404)
    with pytest.raises(ModelNotFound) as exc:
        provider.generate(messages=[{"role": "user", "content": "Hi"}])
    assert "Model qwen3:8b is not installed" in str(exc.value)
    assert "ollama pull qwen3:8b" in str(exc.value)

    def fake_500_model_err(url, json=None, stream=False, timeout=None):
        return FakeResponse(status_code=500, text='{"error": "model \'qwen3:8b\' not found"}')

    provider2 = OllamaProvider(model="qwen3:8b", http_post=fake_500_model_err)
    with pytest.raises(ModelNotFound) as exc:
        provider2.generate(messages=[{"role": "user", "content": "Hi"}])
    assert "Model qwen3:8b is not installed" in str(exc.value)


def test_other_http_errors_raise_ollama_error_truncated():
    """Verify unexpected HTTP errors raise OllamaError truncated to 300 chars."""
    huge_msg = "x" * 500

    def fake_500(url, json=None, stream=False, timeout=None):
        return FakeResponse(status_code=500, text=f'{{"error": "{huge_msg}"}}')

    provider = OllamaProvider(http_post=fake_500)
    with pytest.raises(OllamaError) as exc:
        provider.generate(messages=[{"role": "user", "content": "Hi"}])
    assert "Ollama error (500):" in str(exc.value)
    assert len(str(exc.value)) < 350


def test_think_retry_happens_once_when_status_400():
    """Verify server 400 mentioning think triggers exactly one retry without the think key."""
    attempts = []

    def fake_post(url, json=None, stream=False, timeout=None):
        attempts.append(dict(json or {}))
        if len(attempts) == 1:
            return FakeResponse(status_code=400, text="unknown field: think")
        return FakeResponse(lines=[
            json_lib.dumps({"message": {"content": "Retry succeeded"}}),
            json_lib.dumps({"done": True}),
        ])

    provider = OllamaProvider(think=False, http_post=fake_post)
    res = provider.generate(messages=[{"role": "user", "content": "Hi"}])

    assert len(attempts) == 2
    assert "think" in attempts[0]
    assert "think" not in attempts[1]
    assert res.text == "Retry succeeded"


def test_inline_tool_json_conversion():
    """Verify inline tool JSON is converted to ToolCall only when inline_tools=True and exact match."""
    dummy_tool = Tool(
        name="create_file",
        func=lambda: None,
        description="Creates file",
        parameters={},
        permission="confirm",
    )

    # 1. inline_tools=True with valid exact JSON
    def fake_post_inline(url, json=None, stream=False, timeout=None):
        return FakeResponse(lines=[
            json_lib.dumps({"message": {"content": '{"name": "create_file", "arguments": {"path": "a.txt"}}'}}),
            json_lib.dumps({"done": True}),
        ])

    p_inline = OllamaProvider(inline_tools=True, http_post=fake_post_inline)
    res = p_inline.generate(messages=[{"role": "user", "content": "write"}], tools=[dummy_tool])
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "create_file"
    assert res.tool_calls[0].args == {"path": "a.txt"}
    assert res.text == ""

    # 2. inline_tools=False leaves text as text
    p_no_inline = OllamaProvider(inline_tools=False, http_post=fake_post_inline)
    res2 = p_no_inline.generate(messages=[{"role": "user", "content": "write"}], tools=[dummy_tool])
    assert res2.tool_calls == []
    assert '{"name": "create_file"' in res2.text

    # 3. Normal sentence with braces stays text even with inline_tools=True
    def fake_post_braces(url, json=None, stream=False, timeout=None):
        return FakeResponse(lines=[
            json_lib.dumps({"message": {"content": "Use parameters like {count: 5} to configure."}}),
            json_lib.dumps({"done": True}),
        ])

    p_braces = OllamaProvider(inline_tools=True, http_post=fake_post_braces)
    res3 = p_braces.generate(messages=[{"role": "user", "content": "help"}], tools=[dummy_tool])
    assert res3.tool_calls == []
    assert res3.text == "Use parameters like {count: 5} to configure."


def test_warmup_returns_float():
    """Verify warmup sends num_predict=1 non-streaming chat and returns elapsed seconds."""
    called_payload = {}

    def fake_post(url, json=None, stream=False, timeout=None):
        called_payload.update(json or {})
        return FakeResponse(status_code=200, text='{"done": true}')

    provider = OllamaProvider(http_post=fake_post)
    elapsed = provider.warmup()
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0
    assert called_payload["stream"] is False
    assert called_payload["options"]["num_predict"] == 1


def test_list_models_and_server_version():
    """Verify list_models and server_version helpers parse endpoints and return None on failure."""
    with patch("requests.get") as mock_get:
        # Success for list_models
        mock_get.return_value = FakeResponse(
            status_code=200,
            json_data={"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:3b"}]},
        )
        models = list_models(host="http://127.0.0.1:11434")
        assert models == ["qwen3:8b", "llama3.2:3b"]

        # Failure for list_models
        mock_get.return_value = FakeResponse(status_code=500)
        assert list_models(host="http://127.0.0.1:11434") is None

        # Success for server_version
        mock_get.return_value = FakeResponse(
            status_code=200,
            json_data={"version": "0.3.14"},
        )
        assert server_version(host="http://127.0.0.1:11434") == "0.3.14"

        # Failure for server_version
        mock_get.side_effect = requests.exceptions.ConnectionError()
        assert server_version(host="http://127.0.0.1:11434") is None
