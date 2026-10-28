"""LLM provider abstraction and Gemini implementation with tool calling support."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from google import genai
from google.genai import errors, types

if TYPE_CHECKING:
    from tools.registry import Tool


@dataclass
class ToolCall:
    """Represents a request from the LLM to call a specific tool."""
    name: str
    args: dict
    id: Optional[str] = None
    thought_signature: Optional[Any] = None


@dataclass
class LLMResponse:
    """Standardized response from an LLM provider containing text and/or tool calls."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    interrupted: bool = False
    prompt_tokens: Optional[int] = None


class LLMError(Exception):
    """Custom exception raised when an LLM provider fails."""
    pass


class QuotaExceededError(LLMError):
    """Raised when an LLM provider hits rate limits or quota exhaustion."""
    pass


class LLMProvider(ABC):
    """Abstract base class defining the interface for LLM providers."""

    @abstractmethod
    def generate(
        self,
        messages: list[dict],
        tools: Optional[list["Tool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        """Generate a response from a sequence of chat messages and optional tools.

        Message format:
        - User message: {"role": "user", "content": str}
        - Assistant message: {"role": "assistant", "content": str, "tool_calls": list[ToolCall]}
        - Tool response: {"role": "tool", "name": str, "content": str}
        """
        pass

    def warmup(self) -> float:
        """Warm up the provider and return elapsed seconds (default no-op returns 0.0)."""
        return 0.0


class GeminiProvider(LLMProvider):
    """Google Gemini LLM provider implementation using google-genai SDK."""

    def __init__(self, api_key: str | None = None, model_name: str | None = None):
        # Only import settings if credentials were not provided explicitly
        if api_key is None:
            from config import settings
            api_key = settings.require_api_key()
        if model_name is None:
            from config import settings
            model_name = settings.MODEL_NAME

        self.api_key = api_key
        self.model_name = model_name
        self.client = genai.Client(api_key=self.api_key)

    def _convert_tools(self, tools: Optional[list["Tool"]]) -> Optional[list[types.Tool]]:
        """Convert ORBIT Tool dataclasses into Gemini FunctionDeclarations.

        Maps each Tool's name, description, and JSON schema parameters into a
        Gemini types.FunctionDeclaration bundled into a types.Tool object.
        """
        if not tools:
            return None

        function_declarations: list[types.FunctionDeclaration] = []
        for tool in tools:
            decl = types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters_json_schema=tool.parameters,
            )
            function_declarations.append(decl)

        return [types.Tool(function_declarations=function_declarations)]

    def _convert_messages(self, messages: list[dict]) -> list[types.Content]:
        """Convert internal message dicts into Gemini types.Content objects.

        Conversions:
        - 'user': Text part with role='user'.
        - 'assistant': Model response, combining optional text parts and any
          function call parts requested by the model, with role='model'.
          Preserves thought_signature on function call parts for Gemini reasoning models.
        - 'tool': Function execution result packed into types.Part.from_function_response
          with role='user' (required by Gemini API for function returns).
        """
        contents: list[types.Content] = []

        for msg in messages:
            role = msg.get("role")
            parts: list[types.Part] = []

            if role == "user":
                parts.append(types.Part.from_text(text=msg.get("content", "")))
                contents.append(types.Content(role="user", parts=parts))

            elif role == "assistant":
                content_text = msg.get("content")
                if content_text:
                    parts.append(types.Part.from_text(text=content_text))

                # Include any tool calls previously requested by the assistant
                tool_calls: list[ToolCall] = msg.get("tool_calls") or []
                for tc in tool_calls:
                    p = types.Part.from_function_call(
                        name=tc.name,
                        args=tc.args,
                    )
                    # Restore thought signature if present
                    if getattr(tc, "thought_signature", None) is not None:
                        p.thought_signature = tc.thought_signature
                    parts.append(p)

                # Ensure at least one part exists
                if not parts:
                    parts.append(types.Part.from_text(text=""))

                contents.append(types.Content(role="model", parts=parts))

            elif role == "tool":
                # Convert tool output into a function response part with role 'user'
                tool_name = msg.get("name", "")
                result_content = msg.get("content", "")
                parts.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"result": result_content},
                    )
                )
                contents.append(types.Content(role="user", parts=parts))

        return contents

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        """Check if an exception indicates HTTP 429 or RESOURCE_EXHAUSTED."""
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "")).upper()
        msg = str(exc).upper()
        return (
            code == 429
            or status == "RESOURCE_EXHAUSTED"
            or "429" in msg
            or "RESOURCE_EXHAUSTED" in msg
            or "QUOTA_EXCEEDED" in msg
        )

    @staticmethod
    def _is_daily_or_excessive_limit(exc: Exception) -> bool:
        """Check if the quota error is a daily quota limit or requires >60s retry delay."""
        msg = str(exc).lower()
        if "daily" in msg or "per day" in msg or "per_day" in msg:
            return True

        match = re.search(
            r"(?:retry|wait|again)(?:\s+after|\s+in)?\s*[:=]?\s*(\d+)\s*(?:seconds?|s)\b",
            msg,
        )
        if match:
            delay = int(match.group(1))
            if delay > 60:
                return True

        return False

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list["Tool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        """Generate response from Gemini, disabling SDK auto-execution so the agent controls loops."""
        system_text = next(
            (m.get("content") for m in messages if m.get("role") == "system"),
            None,
        )
        non_system_messages = [m for m in messages if m.get("role") != "system"]
        contents = self._convert_messages(non_system_messages)
        gemini_tools = self._convert_tools(tools)

        # Build config: attach tools and system instruction, disabling SDK auto-execution
        config: Optional[types.GenerateContentConfig] = None
        if gemini_tools or system_text:
            config = types.GenerateContentConfig(
                tools=gemini_tools,
                system_instruction=system_text,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            )

        quota_attempts = 0
        server_attempts = 0

        while True:
            delta_delivered = False
            accumulated_text = ""
            parsed_tool_calls: list[ToolCall] = []

            try:
                if on_delta is not None:
                    if should_stop and should_stop():
                        return LLMResponse(text="", tool_calls=[], interrupted=True)

                    response_stream = self.client.models.generate_content_stream(
                        model=self.model_name,
                        contents=contents,
                        config=config,
                    )

                    for chunk in response_stream:
                        if should_stop and should_stop():
                            if hasattr(response_stream, "close") and callable(response_stream.close):
                                try:
                                    response_stream.close()
                                except Exception:
                                    pass
                            return LLMResponse(
                                text=accumulated_text,
                                tool_calls=[],
                                interrupted=True,
                            )

                        chunk_text = ""
                        parts = []
                        if getattr(chunk, "candidates", None):
                            try:
                                content = chunk.candidates[0].content
                                if content and getattr(content, "parts", None):
                                    parts = content.parts
                            except (IndexError, AttributeError, TypeError):
                                parts = []

                        if parts:
                            for part in parts:
                                txt = getattr(part, "text", None)
                                if txt:
                                    chunk_text += txt
                                fc = getattr(part, "function_call", None)
                                if fc is not None:
                                    parsed_tool_calls.append(
                                        ToolCall(
                                            name=fc.name,
                                            args=dict(fc.args or {}),
                                            id=getattr(fc, "id", None),
                                            thought_signature=getattr(part, "thought_signature", None),
                                        )
                                    )

                        if not chunk_text:
                            try:
                                txt = getattr(chunk, "text", None)
                                if txt and isinstance(txt, str):
                                    chunk_text = txt
                            except Exception:
                                pass

                        if not parsed_tool_calls and getattr(chunk, "function_calls", None):
                            try:
                                for fc in chunk.function_calls:
                                    parsed_tool_calls.append(
                                        ToolCall(
                                            name=fc.name,
                                            args=dict(fc.args or {}),
                                            id=getattr(fc, "id", None),
                                        )
                                    )
                            except Exception:
                                pass

                        if chunk_text:
                            accumulated_text += chunk_text
                            delta_delivered = True
                            try:
                                on_delta(chunk_text)
                            except Exception:
                                pass

                        if should_stop and should_stop():
                            if hasattr(response_stream, "close") and callable(response_stream.close):
                                try:
                                    response_stream.close()
                                except Exception:
                                    pass
                            return LLMResponse(
                                text=accumulated_text,
                                tool_calls=[],
                                interrupted=True,
                            )

                    return LLMResponse(
                        text=accumulated_text,
                        tool_calls=parsed_tool_calls,
                    )

                else:
                    response = self.client.models.generate_content(
                        model=self.model_name,
                        contents=contents,
                        config=config,
                    )

                    # Parse any tool calls requested by the model
                    parts_with_fc = []

                    if getattr(response, "candidates", None):
                        try:
                            content = response.candidates[0].content
                            if content and getattr(content, "parts", None):
                                parts_with_fc = [
                                    p for p in content.parts
                                    if getattr(p, "function_call", None) is not None
                                ]
                        except (IndexError, AttributeError, TypeError):
                            parts_with_fc = []

                    if parts_with_fc:
                        for part in parts_with_fc:
                            fc = part.function_call
                            parsed_tool_calls.append(
                                ToolCall(
                                    name=fc.name,
                                    args=dict(fc.args or {}),
                                    id=getattr(fc, "id", None),
                                    thought_signature=getattr(part, "thought_signature", None),
                                )
                            )
                    elif getattr(response, "function_calls", None):
                        for fc in response.function_calls:
                            parsed_tool_calls.append(
                                ToolCall(
                                    name=fc.name,
                                    args=dict(fc.args or {}),
                                    id=getattr(fc, "id", None),
                                )
                            )

                    # Extract generated text if present
                    response_text = ""
                    try:
                        response_text = response.text or ""
                    except Exception:
                        response_text = ""

                    return LLMResponse(
                        text=response_text,
                        tool_calls=parsed_tool_calls,
                    )

            except Exception as exc:
                if delta_delivered:
                    # Retries are allowed ONLY if no delta has been delivered yet
                    if self._is_quota_error(exc):
                        raise QuotaExceededError(
                            f"Quota limit reached for model '{self.model_name}'. "
                            f"Please wait a moment or switch MODEL_NAME in your .env file."
                        ) from None
                    if isinstance(exc, errors.APIError):
                        raise LLMError(f"Gemini API error: {exc.message}") from exc
                    raise LLMError(f"Failed to generate response: {exc}") from exc

                if self._is_quota_error(exc):
                    # Refuse retry for daily limits or delays > 60s
                    if self._is_daily_or_excessive_limit(exc):
                        raise QuotaExceededError(
                            f"Quota limit reached for model '{self.model_name}'. "
                            f"Please wait a moment or switch MODEL_NAME in your .env file."
                        ) from None

                    # Transient per-minute 429: retry up to 2 times (2s, then 4s)
                    if quota_attempts < 2:
                        delay = 2.0 if quota_attempts == 0 else 4.0
                        quota_attempts += 1
                        time.sleep(delay)
                        continue

                    # Retries exhausted
                    raise QuotaExceededError(
                        f"Rate limit exceeded for model '{self.model_name}'. "
                        f"Please wait a moment or switch MODEL_NAME in your .env file."
                    ) from None

                # Transient 503 / server errors retry
                code = getattr(exc, "code", None)
                msg = str(exc).lower()
                is_server_error = (
                    code == 503
                    or "503" in msg
                    or "unavailable" in msg
                    or "connection lost" in msg
                    or "connection reset" in msg
                )
                if is_server_error and server_attempts < 2:
                    server_attempts += 1
                    time.sleep(1.5 * server_attempts)
                    continue

                if isinstance(exc, errors.APIError):
                    raise LLMError(f"Gemini API error: {exc.message}") from exc

                raise LLMError(f"Failed to generate response: {exc}") from exc


class MockProvider(LLMProvider):
    """Mock LLM provider returning scripted responses or generating fake tool calls for offline testing."""

    def __init__(self, responses: Optional[list[LLMResponse]] = None):
        self.responses = list(responses) if responses else []
        self.call_history: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list["Tool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        """Return scripted responses, simulate get_current_time on 'time', or summarize tool execution."""
        self.call_history.append(list(messages))

        if self.responses:
            resp = self.responses.pop(0)
        elif messages and messages[-1].get("role") == "tool":
            tool_content = messages[-1].get("content", "")
            resp = LLMResponse(text=f"The tool result was: {tool_content}")
        else:
            last_user_content = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user_content = msg.get("content", "")
                    break

            lower_user = last_user_content.lower().strip()

            # Check for opening or closing allowed applications
            app_aliases = {
                "notepad": "notepad",
                "calculator": "calculator",
                "calc": "calculator",
                "explorer": "explorer",
                "chrome": "chrome",
                "vscode": "vscode",
                "code": "vscode",
            }
            open_match = None
            close_match = None
            for alias, canonical in app_aliases.items():
                if re.search(rf"\b(?:open|launch|start)\s+{alias}\b", lower_user):
                    open_match = canonical
                    break
                if re.search(rf"\b(?:close|quit|stop|exit)\s+{alias}\b", lower_user):
                    close_match = canonical
                    break

            if "time" in lower_user:
                resp = LLMResponse(
                    text="",
                    tool_calls=[ToolCall(name="get_current_time", args={})],
                )
            elif open_match:
                resp = LLMResponse(
                    text="",
                    tool_calls=[ToolCall(name="open_application", args={"name": open_match})],
                )
            elif close_match:
                resp = LLMResponse(
                    text="",
                    tool_calls=[ToolCall(name="close_application", args={"name": close_match})],
                )
            elif "list files" in lower_user or "show files" in lower_user:
                resp = LLMResponse(
                    text="",
                    tool_calls=[ToolCall(name="list_files", args={})],
                )
            elif "system info" in lower_user or "system status" in lower_user:
                resp = LLMResponse(
                    text="",
                    tool_calls=[ToolCall(name="system_info", args={})],
                )
            elif "list apps" in lower_user or "allowed apps" in lower_user:
                resp = LLMResponse(
                    text="",
                    tool_calls=[ToolCall(name="list_allowed_apps", args={})],
                )
            else:
                resp = LLMResponse(text="[Mock] Hello! How can I assist you today?")

        if on_delta and resp.text:
            words = resp.text.split(" ")
            accumulated = []
            for i, word in enumerate(words):
                if should_stop and should_stop():
                    return LLMResponse(
                        text=" ".join(accumulated),
                        tool_calls=[],
                        interrupted=True,
                    )
                chunk = word if i == len(words) - 1 else word + " "
                accumulated.append(word)
                try:
                    on_delta(chunk)
                except Exception:
                    pass
                if should_stop and should_stop():
                    return LLMResponse(
                        text=" ".join(accumulated),
                        tool_calls=[],
                        interrupted=True,
                    )

        return resp

