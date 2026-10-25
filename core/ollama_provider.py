"""Ollama local LLM provider implementation with streaming, tool calling, and offline guard support."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urlparse

import requests

from core.llm import LLMError, LLMProvider, LLMResponse, ToolCall
from core.offline import is_loopback

if TYPE_CHECKING:
    from tools.registry import Tool


class OllamaError(LLMError):
    """Base exception for errors communicating with the Ollama service."""
    pass


class OllamaUnavailable(OllamaError):
    """Raised when Ollama is not running, unreachable, or times out."""
    pass


class ModelNotFound(OllamaError):
    """Raised when the specified model is not installed in Ollama."""
    pass


class ThinkFilter:
    """Filters out <think>...</think> blocks from streaming text chunks."""

    def __init__(self) -> None:
        self.in_think = False
        self.pending = ""

    def process_chunk(self, chunk: str) -> str:
        """Process an incoming delta chunk and return text safe to emit to on_delta."""
        self.pending += chunk
        emitted = ""

        while self.pending:
            if not self.in_think:
                idx = self.pending.find("<think>")
                if idx != -1:
                    emitted += self.pending[:idx]
                    self.pending = self.pending[idx + len("<think>"):]
                    self.in_think = True
                else:
                    tag = "<think>"
                    partial_match = 0
                    for i in range(1, len(tag)):
                        if self.pending.endswith(tag[:i]):
                            partial_match = i
                            break
                    if partial_match > 0:
                        safe_len = len(self.pending) - partial_match
                        emitted += self.pending[:safe_len]
                        self.pending = self.pending[safe_len:]
                        break
                    else:
                        emitted += self.pending
                        self.pending = ""
                        break
            else:
                idx = self.pending.find("</think>")
                if idx != -1:
                    self.pending = self.pending[idx + len("</think>"):]
                    self.in_think = False
                else:
                    tag = "</think>"
                    partial_match = 0
                    for i in range(1, len(tag)):
                        if self.pending.endswith(tag[:i]):
                            partial_match = i
                            break
                    if partial_match > 0:
                        self.pending = self.pending[-partial_match:]
                    else:
                        self.pending = ""
                    break

        return emitted


def _strip_think_tags(text: str) -> str:
    """Strip any complete or unclosed <think>...</think> blocks from the final text."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<think>.*", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _extract_hostname(host_url: str) -> str:
    """Extract host string without protocol or port."""
    if not host_url.startswith(("http://", "https://")):
        host_url = "http://" + host_url
    parsed = urlparse(host_url)
    return parsed.hostname or host_url


def _try_extract_inline_tool(text: str, tools: Optional[list["Tool"]]) -> Optional[ToolCall]:
    """Attempt to extract an inline JSON tool call from final conversational text.

    Inline tool calling is disabled by default to prevent prompt injection and accidental execution:
    if the model quotes, echoes, or formats a JSON blob in conversational text, it must not silently
    become an action.
    """
    if not tools:
        return None

    raw = text.strip()
    # Strip markdown code fences if wrapped in one
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()

    if not (raw.startswith("{") and raw.endswith("}")):
        return None

    try:
        data = json.loads(raw)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    tool_name = data.get("name")
    if not tool_name:
        return None

    declared_names = {t.name for t in tools}
    if tool_name not in declared_names:
        return None

    args = data.get("arguments")
    if args is None:
        args = data.get("parameters")

    if not isinstance(args, dict):
        return None

    return ToolCall(id="call_1", name=tool_name, args=args)


class OllamaProvider(LLMProvider):
    """Local LLM provider interfacing with the Ollama chat completions API."""

    def __init__(
        self,
        model: Optional[str] = None,
        host: Optional[str] = None,
        num_ctx: Optional[int] = None,
        temperature: Optional[float] = None,
        seed: Optional[int] = None,
        think: Optional[bool] = None,
        keep_alive: Optional[str] = None,
        timeout_s: Optional[int] = None,
        allow_remote: Optional[bool] = None,
        inline_tools: Optional[bool] = None,
        http_post: Optional[Callable[..., Any]] = None,
    ) -> None:
        from config import settings

        self.model = model if model is not None else getattr(settings, "OLLAMA_MODEL", "qwen3:8b")
        self.host = host if host is not None else getattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")
        self.num_ctx = num_ctx if num_ctx is not None else getattr(settings, "OLLAMA_NUM_CTX", 8192)
        self.temperature = (
            temperature if temperature is not None else getattr(settings, "OLLAMA_TEMPERATURE", 0.2)
        )
        self.seed = seed if seed is not None else getattr(settings, "OLLAMA_SEED", None)
        self.think = think if think is not None else getattr(settings, "OLLAMA_THINK", False)
        self.keep_alive = (
            keep_alive if keep_alive is not None else getattr(settings, "OLLAMA_KEEP_ALIVE", "30m")
        )
        self.timeout_s = timeout_s if timeout_s is not None else getattr(settings, "OLLAMA_TIMEOUT_S", 180)
        self.allow_remote = (
            allow_remote if allow_remote is not None else getattr(settings, "OLLAMA_ALLOW_REMOTE", False)
        )
        self.inline_tools = (
            inline_tools if inline_tools is not None else getattr(settings, "OLLAMA_INLINE_TOOLS", False)
        )
        self.http_post = http_post or requests.post

        # Refuse any host that is not loopback unless allow_remote is True
        hostname = _extract_hostname(self.host)
        if not self.allow_remote and not is_loopback(hostname):
            raise ValueError(
                f"Ollama host '{self.host}' is not a permitted loopback address (127.0.0.0/8, ::1, localhost). "
                "Set allow_remote=True (or OLLAMA_ALLOW_REMOTE=1) to connect to remote hosts."
            )

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list["Tool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        """Generate response via POST {host}/api/chat with streaming NDJSON."""
        # 1. Message conversion from ORBIT format to Ollama format
        converted_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                converted_messages.append({"role": "system", "content": content})
            elif role == "user":
                converted_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": content}
                tcs = msg.get("tool_calls")
                if tcs:
                    fn_tcs: list[dict[str, Any]] = []
                    for tc in tcs:
                        tc_name = getattr(tc, "name", None) or (tc.get("name") if isinstance(tc, dict) else "")
                        tc_args = getattr(tc, "args", None) or (tc.get("args") if isinstance(tc, dict) else {})
                        fn_tcs.append({
                            "function": {
                                "name": tc_name,
                                "arguments": tc_args,
                            }
                        })
                    entry["tool_calls"] = fn_tcs
                converted_messages.append(entry)
            elif role == "tool":
                tool_name = msg.get("tool_name") or msg.get("name", "")
                converted_messages.append({
                    "role": "tool",
                    "tool_name": tool_name,
                    "content": str(content),
                })

        # 2. Tool declarations
        converted_tools = None
        if tools:
            converted_tools = []
            for t in tools:
                converted_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                })

        # 3. Payload creation
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted_messages,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
            },
        }
        if self.think is not None:
            payload["think"] = self.think
        if self.seed is not None:
            payload["options"]["seed"] = self.seed
        if converted_tools:
            payload["tools"] = converted_tools

        url = f"{self.host.rstrip('/')}/api/chat"

        def _send(body: dict[str, Any]) -> Any:
            try:
                return self.http_post(url, json=body, stream=True, timeout=self.timeout_s)
            except (requests.exceptions.ConnectionError, OSError) as exc:
                raise OllamaUnavailable(
                    f"Ollama is not running at {self.host}. Start it with the Ollama app or run: ollama serve"
                ) from exc
            except requests.exceptions.Timeout as exc:
                raise OllamaUnavailable(
                    f"Ollama is not running at {self.host}. Start it with the Ollama app or run: ollama serve"
                ) from exc

        resp = _send(payload)

        # 4. Handle HTTP 400 think retry if think=False and server mentions think
        if resp.status_code == 400 and not self.think and "think" in resp.text.lower() and "think" in payload:
            retry_payload = dict(payload)
            del retry_payload["think"]
            resp = _send(retry_payload)

        # 5. Handle errors
        if resp.status_code == 404:
            raise ModelNotFound(
                f"Model {self.model} is not installed. Run: ollama pull {self.model}"
            )
        if resp.status_code != 200:
            err_msg = resp.text
            try:
                err_data = resp.json()
                if isinstance(err_data, dict) and "error" in err_data:
                    err_msg = err_data["error"]
            except Exception:
                pass
            if "model not found" in err_msg.lower() or ("model" in err_msg.lower() and "not found" in err_msg.lower()):
                raise ModelNotFound(
                    f"Model {self.model} is not installed. Run: ollama pull {self.model}"
                )
            raise OllamaError(f"Ollama error ({resp.status_code}): {err_msg[:300]}")

        # 6. Stream NDJSON chunks
        tool_calls: list[ToolCall] = []
        accumulated_text = ""
        interrupted = False
        think_filter = ThinkFilter()

        try:
            for line in resp.iter_lines():
                if should_stop and should_stop():
                    try:
                        resp.close()
                    except Exception:
                        pass
                    interrupted = True
                    break

                if not line:
                    continue

                line_str = line.decode("utf-8") if isinstance(line, (bytes, bytearray)) else line
                line_str = line_str.strip()
                if not line_str:
                    continue

                try:
                    chunk = json.loads(line_str)
                except Exception:
                    continue

                msg = chunk.get("message", {})
                chunk_content = msg.get("content", "")

                # Ignore "thinking" field entirely; append content to text
                if chunk_content:
                    accumulated_text += chunk_content

                    delta_to_emit = think_filter.process_chunk(chunk_content)
                    if delta_to_emit and on_delta:
                        try:
                            on_delta(delta_to_emit)
                        except Exception:
                            pass

                # Collect tool_calls from ANY chunk
                raw_tool_calls = msg.get("tool_calls") or []
                for tc in raw_tool_calls:
                    tc_id = tc.get("id") or f"call_{len(tool_calls) + 1}"
                    fn = tc.get("function", {})
                    name = fn.get("name") or tc.get("name", "")
                    raw_args = fn.get("arguments", tc.get("arguments", {}))
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args)
                            if not isinstance(parsed_args, dict):
                                parsed_args = {"_invalid_arguments": raw_args}
                        except Exception:
                            parsed_args = {
                                "_invalid_arguments_json": raw_args,
                                "_error": "invalid json arguments",
                            }
                    elif isinstance(raw_args, dict):
                        parsed_args = raw_args
                    else:
                        parsed_args = {}
                    tool_calls.append(ToolCall(id=tc_id, name=name, args=parsed_args))

                if chunk.get("done", False):
                    break
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if interrupted:
            clean_text = _strip_think_tags(accumulated_text)
            return LLMResponse(text=clean_text, tool_calls=[], interrupted=True)

        clean_text = _strip_think_tags(accumulated_text)

        # 7. Inline tool call handling
        if self.inline_tools and not tool_calls and clean_text:
            extracted_tc = _try_extract_inline_tool(clean_text, tools)
            if extracted_tc is not None:
                tool_calls.append(extracted_tc)
                clean_text = ""

        return LLMResponse(text=clean_text, tool_calls=tool_calls, interrupted=False)

    def warmup(self) -> float:
        """Send one tiny non-streaming chat request (num_predict=1) and return the seconds taken."""
        t0 = time.perf_counter()
        url = f"{self.host.rstrip('/')}/api/chat"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "options": {"num_predict": 1},
        }
        try:
            resp = self.http_post(url, json=payload, timeout=self.timeout_s)
        except (requests.exceptions.ConnectionError, OSError) as exc:
            raise OllamaUnavailable(
                f"Ollama is not running at {self.host}. Start it with the Ollama app or run: ollama serve"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise OllamaUnavailable(
                f"Ollama is not running at {self.host}. Start it with the Ollama app or run: ollama serve"
            ) from exc

        if resp.status_code == 404:
            raise ModelNotFound(
                f"Model {self.model} is not installed. Run: ollama pull {self.model}"
            )
        if resp.status_code != 200:
            err_msg = resp.text
            try:
                err_data = resp.json()
                if isinstance(err_data, dict) and "error" in err_data:
                    err_msg = err_data["error"]
            except Exception:
                pass
            if "model not found" in err_msg.lower() or ("model" in err_msg.lower() and "not found" in err_msg.lower()):
                raise ModelNotFound(
                    f"Model {self.model} is not installed. Run: ollama pull {self.model}"
                )
            raise OllamaError(f"Ollama error ({resp.status_code}): {err_msg[:300]}")

        return time.perf_counter() - t0


def list_models(host: Optional[str] = None) -> Optional[list[str]]:
    """Return list of installed model names from Ollama GET /api/tags, or None on failure."""
    if host is None:
        from config import settings

        host = getattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")
    url = f"{host.rstrip('/')}/api/tags"
    try:
        resp = requests.get(url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("models", [])
            return [m["name"] for m in models if isinstance(m, dict) and "name" in m]
    except Exception:
        pass
    return None


def server_version(host: Optional[str] = None) -> Optional[str]:
    """Return Ollama server version string from GET /api/version, or None on failure."""
    if host is None:
        from config import settings

        host = getattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")
    url = f"{host.rstrip('/')}/api/version"
    try:
        resp = requests.get(url, timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("version")
    except Exception:
        pass
    return None
