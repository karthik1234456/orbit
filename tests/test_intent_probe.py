"""Unit tests for the ORBIT dry-run intent probe.

Validates that:
1. A correct call passes.
2. A wrong tool fails.
3. A call with invalid args fails validation.
4. A none-phrase with a tool call fails.
5. The executor never runs a tool.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any, Optional

import pytest

from core.llm import LLMProvider, LLMResponse, ToolCall
from scripts.intent_probe import (
    PROBE_PHRASES,
    ProbeReport,
    build_probe_registry,
    get_git_info,
    main,
    run_intent_probe,
    run_single_phrase,
)
from tools.registry import Tool


class ScriptedFakeProvider(LLMProvider):
    """Scripted fake LLM provider that yields pre-determined responses in order."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.idx = 0
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        messages: list[dict],
        tools: Optional[list[Tool]] = None,
        on_delta: Optional[Any] = None,
        should_stop: Optional[Any] = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if self.idx < len(self.responses):
            resp = self.responses[self.idx]
            self.idx += 1
            return resp
        return LLMResponse(text="default scripted reply")


def test_correct_call_passes() -> None:
    """A tool call matching the accept list with valid arguments passes."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)
        provider = ScriptedFakeProvider([
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="open_website", args={"url": "https://www.youtube.com"})],
            ),
            LLMResponse(text="Opening YouTube for you now."),
        ])

        res = run_single_phrase(
            provider=provider,
            registry=registry,
            system_prompt="You are ORBIT.",
            phrase="open youtube",
            accept_list=["open_website", "search_site"],
            seed=1,
        )

        assert res.passed is True
        assert res.tool_called == "open_website"
        assert res.args == {"url": "https://www.youtube.com"}
        assert res.validation_passed is True
        assert res.validation_result == "valid"
        assert res.final_text == "Opening YouTube for you now."
        assert len(provider.calls) == 2

        # Check that 'ok (dry run)' was sent back to the model in call 2
        call_2_msgs = provider.calls[1]["messages"]
        assert any(
            m.get("role") == "tool" and m.get("content") == "ok (dry run)"
            for m in call_2_msgs
        )


def test_wrong_tool_fails() -> None:
    """A tool call that succeeds validation but is not in the accept list fails."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)
        provider = ScriptedFakeProvider([
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="calculate", args={"expression": "2+2"})],
            ),
            LLMResponse(text="Calculated 4."),
        ])

        res = run_single_phrase(
            provider=provider,
            registry=registry,
            system_prompt="You are ORBIT.",
            phrase="open youtube",
            accept_list=["open_website", "search_site"],
            seed=1,
        )

        assert res.passed is False
        assert res.tool_called == "calculate"
        # calculate is valid in registry, but is not an acceptable tool for opening youtube
        assert res.validation_passed is True
        assert res.validation_result == "valid"


def test_invalid_args_fails_validation() -> None:
    """Tool calls with invalid schema arguments or failed prechecks fail validation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)

        # 1. Schema violation (unexpected extra argument)
        provider_schema = ScriptedFakeProvider([
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="open_website", args={"url": "https://youtube.com", "extra_bad": 123})],
            ),
            LLMResponse(text="Failed"),
        ])
        res_schema = run_single_phrase(
            provider=provider_schema,
            registry=registry,
            system_prompt="You are ORBIT.",
            phrase="open youtube",
            accept_list=["open_website", "search_site"],
            seed=1,
        )
        assert res_schema.passed is False
        assert res_schema.validation_passed is False
        assert "Unexpected extra argument 'extra_bad'" in res_schema.validation_result

        # 2. Tool precheck violation (application not in allowlist)
        provider_precheck = ScriptedFakeProvider([
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="open_application", args={"name": "malicious_binary"})],
            ),
            LLMResponse(text="Failed app launch"),
        ])
        res_precheck = run_single_phrase(
            provider=provider_precheck,
            registry=registry,
            system_prompt="You are ORBIT.",
            phrase="open whatsapp",
            accept_list=["open_application"],
            seed=1,
        )
        assert res_precheck.passed is False
        assert res_precheck.validation_passed is False
        assert "not in the allowed list" in res_precheck.validation_result


def test_none_phrase_with_tool_call_fails() -> None:
    """A conversational 'none' phrase must fail if the model attempts to call any tool."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)
        provider = ScriptedFakeProvider([
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="get_current_time", args={})],
            ),
            LLMResponse(text="The time is 12:00."),
        ])

        res = run_single_phrase(
            provider=provider,
            registry=registry,
            system_prompt="You are ORBIT.",
            phrase="hi",
            accept_list=["none"],
            seed=1,
        )

        assert res.passed is False
        assert res.tool_called == "get_current_time"
        assert res.validation_passed is True


def test_none_phrase_without_tool_call_passes() -> None:
    """A conversational 'none' phrase passes when no tool is called."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)
        provider = ScriptedFakeProvider([
            LLMResponse(text="Hello! How can I assist you today?", tool_calls=[]),
        ])

        res = run_single_phrase(
            provider=provider,
            registry=registry,
            system_prompt="You are ORBIT.",
            phrase="hi",
            accept_list=["none"],
            seed=1,
        )

        assert res.passed is True
        assert res.tool_called is None
        assert res.validation_passed is True
        assert res.validation_result == "n/a"
        assert res.final_text == "Hello! How can I assist you today?"
        assert len(provider.calls) == 1


def test_executor_never_runs_a_tool() -> None:
    """The dry-run executor must never execute tool.func under any circumstance."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)

        # Wrap every tool's func with a spy that records any invocations
        invoked_tools: list[tuple[str, tuple, dict]] = []
        for tool in registry.list_tools():
            orig_func = tool.func

            def make_spy(name: str, fn: Any):
                def spy(*args: Any, **kwargs: Any) -> Any:
                    invoked_tools.append((name, args, kwargs))
                    return fn(*args, **kwargs)

                return spy

            tool.func = make_spy(tool.name, orig_func)

        # Script responses for multiple tools
        provider = ScriptedFakeProvider([
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="create_file", args={"path": "notes.txt", "content": "hello"})],
            ),
            LLMResponse(text="Created notes.txt"),
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="open_application", args={"name": "notepad"})],
            ),
            LLMResponse(text="Opened notepad"),
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="open_website", args={"url": "https://google.com"})],
            ),
            LLMResponse(text="Opened google"),
        ])

        res1 = run_single_phrase(
            provider, registry, "ORBIT", "create a file notes.txt saying hello", ["create_file"], seed=1
        )
        res2 = run_single_phrase(provider, registry, "ORBIT", "open notepad", ["open_application"], seed=1)
        res3 = run_single_phrase(provider, registry, "ORBIT", "open google", ["open_website"], seed=1)

        assert res1.passed is True
        assert res2.passed is True
        assert res3.passed is True

        # CRITICAL ASSERTION: The executor must NEVER execute tool.func
        assert len(invoked_tools) == 0


def test_run_intent_probe_full_flow() -> None:
    """Test full intent probe aggregation, metrics computation, and report file generation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)

        test_phrases = [
            ("open youtube", ["open_website", "search_site"]),
            ("hi", ["none"]),
        ]

        def fake_provider_factory(seed: int) -> LLMProvider:
            class PerSeedProvider(LLMProvider):
                def __init__(self, s: int) -> None:
                    self.s = s
                    self.call_count = 0

                def generate(self, messages: list[dict], tools: Optional[list[Tool]] = None, **kwargs: Any) -> LLMResponse:
                    user_msg = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
                    if "youtube" in user_msg:
                        # First turn calls open_website, second turn returns text
                        if any(m.get("role") == "tool" for m in messages):
                            return LLMResponse(text=f"Opening YouTube for seed {self.s}")
                        return LLMResponse(
                            text="",
                            tool_calls=[ToolCall(name="open_website", args={"url": "https://youtube.com"})],
                        )
                    else:
                        # "hi" -> no tool call
                        return LLMResponse(text=f"Hello for seed {self.s}", tool_calls=[])

            return PerSeedProvider(seed)

        out_report = Path(tmp_dir) / "test_report.md"

        report = run_intent_probe(
            provider_factory=fake_provider_factory,
            registry=registry,
            system_prompt="Test system prompt",
            phrases=test_phrases,
            seeds=[1, 2, 3],
            label="test-run",
            model_name="mock-model",
            system_prompt_source="unit-test",
            write_report=True,
            output_path=out_report,
        )

        assert report.action_total_count == 3
        assert report.action_pass_count == 3
        assert report.notool_total_count == 3
        assert report.notool_pass_count == 3
        assert report.total_count == 6
        assert report.total_pass_count == 6

        assert out_report.exists()
        content = out_report.read_text(encoding="utf-8")
        assert "ORBIT Intent Probe Report: test-run" in content
        assert "The probe is a dev tool (not a held-out set)" in content
        assert "Overall Action Pass Rate" in content
        assert "No-Tool Pass Rate" in content
        assert "open youtube" in content
        assert "hi" in content
        assert get_git_info() in content


def test_cli_system_prompt_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test --system-prompt-file flag CLI loading."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        custom_prompt_file = Path(tmp_dir) / "custom_prompt.txt"
        custom_prompt_file.write_text("Custom instructions for ORBIT.", encoding="utf-8")
        out_report = Path(tmp_dir) / "cli_report.md"

        # Mock run_intent_probe to inspect passed system_prompt
        captured: dict[str, Any] = {}

        def mock_run_intent_probe(**kwargs: Any) -> ProbeReport:
            captured.update(kwargs)
            return ProbeReport(
                commit="test",
                timestamp="now",
                label=kwargs.get("label", "baseline"),
                model=kwargs.get("model_name", "model"),
                temperature=0.2,
                seeds=[1],
                system_prompt=kwargs.get("system_prompt", ""),
                system_prompt_source=kwargs.get("system_prompt_source", ""),
                phrase_summaries=[],
                action_pass_count=0,
                action_total_count=0,
                notool_pass_count=0,
                notool_total_count=0,
                total_pass_count=0,
                total_count=0,
                output_path=Path(out_report),
            )

        monkeypatch.setattr("scripts.intent_probe.run_intent_probe", mock_run_intent_probe)

        exit_code = main([
            "--system-prompt-file",
            str(custom_prompt_file),
            "--label",
            "custom-prompt",
            "--seeds",
            "1",
            "--output",
            str(out_report),
        ])

        assert exit_code == 0
        assert captured["system_prompt"] == "Custom instructions for ORBIT."
        assert captured["label"] == "custom-prompt"
        assert str(custom_prompt_file) in captured["system_prompt_source"]


def test_intent_probe_browser_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that --browser includes BROWSER_PROBE_PHRASES and sets up browser tools."""
    from scripts.intent_probe import BROWSER_PROBE_PHRASES

    expected_phrases = [
        ("scroll down", ["browser_scroll"]),
        ("pause the video", ["browser_media"]),
        ("play the first video", ["browser_click_result"]),
        ("mute", ["browser_media"]),
        ("go back", ["browser_history"]),
        ("open youtube and play free fire videos", ["open_website", "search_site"]),
        ("play lofi music on youtube", ["search_site"]),
        ("what's on this page", ["browser_read_page"]),
        ("close the browser", ["browser_close"]),
    ]
    assert BROWSER_PROBE_PHRASES == expected_phrases

    # Verify registry has browser tools when browser_enabled=True
    with tempfile.TemporaryDirectory() as tmp_dir:
        reg = build_probe_registry(tmp_dir, browser_enabled=True)
        assert reg.get("browser_scroll") is not None
        assert reg.get("browser_media") is not None
        assert reg.get("browser_click_result") is not None
        assert reg.get("browser_history") is not None
        assert reg.get("browser_read_page") is not None
        assert reg.get("browser_close") is not None

        # Test CLI invocation with --browser
        captured: dict[str, Any] = {}

        def mock_run_intent_probe(**kwargs: Any) -> ProbeReport:
            captured.update(kwargs)
            return ProbeReport(
                commit="test",
                timestamp="now",
                label=kwargs.get("label", "baseline"),
                model=kwargs.get("model_name", "model"),
                temperature=0.2,
                seeds=[1],
                system_prompt=kwargs.get("system_prompt", ""),
                system_prompt_source=kwargs.get("system_prompt_source", ""),
                phrase_summaries=[],
                action_pass_count=0,
                action_total_count=0,
                notool_pass_count=0,
                notool_total_count=0,
                total_pass_count=0,
                total_count=0,
                output_path=Path(tmp_dir) / "rep.md",
            )

        monkeypatch.setattr("scripts.intent_probe.run_intent_probe", mock_run_intent_probe)

        exit_code = main(["--browser", "--seeds", "1"])
        assert exit_code == 0
        assert captured["browser_enabled"] is True
        assert len(captured["phrases"]) == len(PROBE_PHRASES) + len(BROWSER_PROBE_PHRASES)

