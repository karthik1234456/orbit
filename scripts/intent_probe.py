"""Dry-run intent probe for testing model tool-calling without executing tools.

Sends user phrases to the local LLM through the standard application system prompt,
tool declarations, and settings (RAG_AUTO off, temperature 0.2). Uses a fake executor
that runs ONLY argument validation and tool prechecks, returning 'ok (dry run)'
to the model to inspect its intent routing and final text.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Optional, Sequence, Union

# Ensure project root is in sys.path when executed directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from core.bootstrap import AppPaths, build_app, default_system_prompt
from core.llm import LLMProvider, LLMResponse
from core.ollama_provider import OllamaProvider
from core.routing import matches_inability, offered_tools, select_groups
from tools.registry import ToolRegistry


def compute_percentile(data: Sequence[Union[int, float]], pct: float) -> float:
    """Compute percentile value from sequence."""
    if not data:
        return 0.0
    sorted_d = sorted(data)
    if len(sorted_d) == 1:
        return float(sorted_d[0])
    k = (len(sorted_d) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_d) - 1)
    d = k - f
    return float(sorted_d[f] + d * (sorted_d[c] - sorted_d[f]))


# Fixed probe evaluation phrases and their acceptable tool calls.
# Phrases with ["none"] expect no tool calls from the model.
PROBE_PHRASES: list[tuple[str, list[str]]] = [
    ("open youtube", ["open_website", "search_site"]),
    ("can u open youtube", ["open_website"]),
    ("open gmail", ["open_website"]),
    ("go to github.com", ["open_website"]),
    ("take me to wikipedia", ["open_website"]),
    ("open google", ["open_website"]),
    ("open whatsapp", ["open_application"]),
    ("launch chrome", ["open_application"]),
    ("start vscode", ["open_application"]),
    ("open notepad", ["open_application"]),
    ("open calculator", ["open_application"]),
    ("open file explorer", ["open_application", "open_workspace_folder"]),
    ("open folders", ["open_workspace_folder", "list_files"]),
    ("trending videos on youtube", ["search_site", "search_web", "open_website"]),
    ("open youtube in chrome and search for trending videos", ["search_site", "open_website"]),
    ("search for python tutorials", ["search_site", "search_web"]),
    ("google rtx 4050 specs", ["search_site", "search_web"]),
    ("look up the weather in delhi", ["search_web", "search_site"]),
    ("create a file notes.txt saying hello", ["create_file"]),
    ("what files do i have", ["list_files"]),
    ("what time is it", ["get_current_time"]),
    ("remember that i like python", ["remember_fact"]),
    ("what is 15% of 240", ["calculate"]),
    ("hi", ["none"]),
    ("explain recursion in two sentences", ["none"]),
    ("thanks, that was helpful", ["none"]),
    ("tell me a joke", ["none"]),
    ("what is the capital of france", ["none"]),
]

BROWSER_PROBE_PHRASES: list[tuple[str, list[str]]] = [
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


@dataclass
class SingleRunResult:
    """Outcome of probing a single phrase with a single random seed."""

    phrase: str
    accept_list: list[str]
    seed: int
    tool_called: Optional[str]
    args: dict[str, Any]
    validation_result: str
    validation_passed: bool
    final_text: str
    passed: bool
    tools_offered_count: int = 0
    tools_registered_count: int = 0
    routing_retry: bool = False
    off_route_call: bool = False
    prompt_tokens: Optional[int] = None
    ttft_ms: Optional[float] = None


@dataclass
class PhraseSummary:
    """Aggregated probe results for a single phrase across all tested seeds."""

    phrase: str
    accept_list: list[str]
    pass_count: int
    total_runs: int
    runs: list[SingleRunResult] = field(default_factory=list)
    prior_tools: Optional[list[str]] = None

    @property
    def is_none_phrase(self) -> bool:
        """Return True if phrase expects no tool call."""
        return self.accept_list == ["none"] or len(self.accept_list) == 0

    @property
    def median_tools_offered(self) -> float:
        counts = [r.tools_offered_count for r in self.runs]
        return compute_percentile(counts, 50.0) if counts else 0.0

    @property
    def has_retry(self) -> bool:
        return any(r.routing_retry for r in self.runs)

    @property
    def has_off_route(self) -> bool:
        return any(r.off_route_call for r in self.runs)


@dataclass
class ProbeReport:
    """Complete summary and dataset produced by an intent probe run."""

    commit: str
    timestamp: str
    label: str
    model: str
    temperature: float
    seeds: list[int]
    system_prompt: str
    system_prompt_source: str
    phrase_summaries: list[PhraseSummary]
    action_pass_count: int
    action_total_count: int
    notool_pass_count: int
    notool_total_count: int
    total_pass_count: int
    total_count: int
    output_path: Optional[Path] = None
    browser_enabled: bool = False
    reminders_enabled: bool = False
    routing_enabled: bool = False
    routines_enabled: bool = False
    system_controls_enabled: bool = False,
    messaging_enabled: bool = False
    median_tools_offered: float = 0.0
    p95_tools_offered: float = 0.0
    median_prompt_size: Optional[float] = None
    median_ttft: Optional[float] = None
    retry_count: int = 0
    off_route_call_count: int = 0
    rerun_warning: Optional[str] = None


def get_git_info() -> str:
    """Retrieve current git HEAD commit hash with '-dirty' suffix if uncommitted changes exist."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        commit = "unknown"

    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if status:
            commit = f"{commit}-dirty"
    except Exception:
        pass

    return commit


def build_probe_registry(
    workspace_path: Union[str, Path],
    browser_enabled: bool = False,
) -> ToolRegistry:
    """Build ToolRegistry with standard desktop, file, app, memory, and doc tools."""
    paths = AppPaths.under(workspace_path)

    class _DummyProvider(LLMProvider):
        def generate(self, messages, tools=None, on_delta=None, should_stop=None) -> LLMResponse:
            return LLMResponse(text="")

    if browser_enabled:
        from browser.backend import FakeBackend
        app = build_app(
            provider=_DummyProvider(),
            paths=paths,
            browser_backend_factory=lambda: FakeBackend(),
        )
    else:
        app = build_app(provider=_DummyProvider(), paths=paths, browser_controller=None)
    return app.registry


def validate_tool_call(registry: ToolRegistry, tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Validate tool call using registry argument schema checks and tool precheck.

    Guaranteed to NEVER execute tool.func.
    Returns (is_valid, validation_result_string).
    """
    val_err = registry.validate_arguments(tool_name, args)
    if val_err is not None:
        return False, str(val_err)

    tool = registry.get(tool_name)
    if tool and tool.precheck is not None:
        try:
            pre_err = tool.precheck(args)
            if pre_err:
                return False, str(pre_err)
        except Exception as exc:
            return False, f"ERROR: Precheck failed with exception: {exc}"

    return True, "valid"


def check_test_split_protection(
    phrases_path: Optional[Union[str, Path]],
    final_test: bool = False,
    force: bool = False,
    routing_enabled: bool = False,
    ran_path: Optional[Path] = None,
) -> tuple[bool, Optional[str]]:
    """Check test split protection for evals/routing/test.json.

    Returns (is_test_split, rerun_warning).
    Raises PermissionError if access is refused.
    """
    if not phrases_path:
        return False, None

    p_str = str(phrases_path).replace("\\", "/")
    test_target = (Path(__file__).resolve().parent.parent / "evals" / "routing" / "test.json").resolve()
    is_test_split = (
        p_str.endswith("evals/routing/test.json")
        or Path(phrases_path).resolve() == test_target
    )
    if not is_test_split:
        return False, None

    if not final_test:
        raise PermissionError(
            "Refusing to run reserved test split 'evals/routing/test.json' without --final-test."
        )

    if ran_path is None:
        ran_path = (Path(__file__).resolve().parent.parent / "evals" / "routing" / ".ran").resolve()

    setting_str = "routing_on" if routing_enabled else "routing_off"
    rerun_warning = None

    if ran_path.exists():
        try:
            content = ran_path.read_text(encoding="utf-8")
            already_ran = any(setting_str in line for line in content.splitlines())
            if already_ran:
                if not force:
                    raise PermissionError(
                        f"Refusing second --final-test for '{setting_str}'. Use --force to rerun."
                    )
                rerun_warning = "RERUN: NOT UNSEEN"
        except PermissionError:
            raise
        except Exception:
            pass

    return True, rerun_warning


def record_test_run(
    routing_enabled: bool = False,
    ran_path: Optional[Path] = None,
) -> None:
    """Record execution in evals/routing/.ran."""
    if ran_path is None:
        ran_path = (Path(__file__).resolve().parent.parent / "evals" / "routing" / ".ran").resolve()

    now_iso = datetime.now(timezone.utc).isoformat()
    commit_info = get_git_info()
    is_dirty = commit_info.endswith("-dirty")
    setting_str = "routing_on" if routing_enabled else "routing_off"
    line = f"date={now_iso} commit={commit_info} dirty={is_dirty} setting={setting_str}\n"
    ran_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ran_path, "a", encoding="utf-8") as f:
        f.write(line)


def load_probe_phrases(
    phrases_path: Optional[Union[str, Path]],
    browser_enabled: bool = False,
) -> list[tuple[str, list[str], Optional[list[str]]]]:
    """Load phrases for probing from file or built-in lists."""
    if phrases_path:
        path = Path(phrases_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Phrase file not found: {phrases_path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        result: list[tuple[str, list[str], Optional[list[str]]]] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    txt = item.get("text") or item.get("phrase", "")
                    accept = item.get("accept", [])
                    prior = item.get("prior_tools")
                    result.append((txt, accept, prior))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    txt = str(item[0])
                    accept = list(item[1])
                    prior = list(item[2]) if len(item) > 2 and item[2] else None
                    result.append((txt, accept, prior))
        return result

    base = [(p, acc, None) for p, acc in PROBE_PHRASES]
    if browser_enabled:
        base.extend([(p, acc, None) for p, acc in BROWSER_PROBE_PHRASES])
    return base


def run_single_phrase(
    provider: LLMProvider,
    registry: ToolRegistry,
    system_prompt: str,
    phrase: str,
    accept_list: list[str],
    seed: int,
    prior_tools: Optional[list[str]] = None,
    routing_enabled: bool = False,
) -> SingleRunResult:
    """Send one message to the model and record tool intent without executing tools."""
    all_tools = registry.list_tools(model_visible_only=True)
    registered_count = len(all_tools)

    if routing_enabled and all_tools:
        groups = select_groups(phrase, prior_tools=prior_tools, state=None)
        tools = offered_tools(all_tools, groups)
    else:
        tools = all_tools

    offered_names = {t.name for t in tools} if tools else set()
    offered_count = len(tools) if tools else 0

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": phrase},
    ]

    t0 = time.perf_counter()
    response = provider.generate(messages, tools=tools or None)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    prompt_tokens = getattr(response, "prompt_tokens", None)

    routing_retry = False
    raw_text = response.text or ""

    # Miss recovery retry: retry ONCE with full tool list if routing narrowed tools and model expresses inability
    if (
        routing_enabled
        and not response.tool_calls
        and len(tools) < registered_count
        and matches_inability(raw_text)
    ):
        routing_retry = True
        tools = all_tools
        offered_names = {t.name for t in tools}
        t1 = time.perf_counter()
        response = provider.generate(messages, tools=tools or None)
        elapsed_ms = (time.perf_counter() - t1) * 1000.0
        prompt_tokens = getattr(response, "prompt_tokens", None)
        raw_text = response.text or ""

    is_none = (accept_list == ["none"] or len(accept_list) == 0)

    if response.tool_calls:
        first_tc = response.tool_calls[0]
        tool_name = first_tc.name
        tool_args = first_tc.args if isinstance(first_tc.args, dict) else {}
        off_route_call = bool(routing_enabled and (tool_name not in offered_names))

        val_passed, val_result = validate_tool_call(registry, tool_name, tool_args)

        # Return "ok (dry run)" to the model for all tool calls in the turn
        followup_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": phrase},
            {"role": "assistant", "content": response.text or "", "tool_calls": response.tool_calls},
        ]
        for tc in response.tool_calls:
            followup_messages.append({
                "role": "tool",
                "name": tc.name,
                "content": "ok (dry run)",
            })

        followup_resp = provider.generate(followup_messages, tools=tools or None)
        raw_final = followup_resp.text or ""
        final_text = raw_final[:150]

        if is_none:
            passed = False
        else:
            passed = (tool_name in accept_list) and val_passed

        return SingleRunResult(
            phrase=phrase,
            accept_list=accept_list,
            seed=seed,
            tool_called=tool_name,
            args=tool_args,
            validation_result=val_result,
            validation_passed=val_passed,
            final_text=final_text,
            passed=passed,
            tools_offered_count=offered_count,
            tools_registered_count=registered_count,
            routing_retry=routing_retry,
            off_route_call=off_route_call,
            prompt_tokens=prompt_tokens,
            ttft_ms=round(elapsed_ms, 2),
        )
    else:
        # No tool called
        final_text = raw_text[:150]
        val_passed = True
        val_result = "n/a"
        off_route_call = False

        if is_none:
            passed = True
        else:
            passed = False

        return SingleRunResult(
            phrase=phrase,
            accept_list=accept_list,
            seed=seed,
            tool_called=None,
            args={},
            validation_result=val_result,
            validation_passed=val_passed,
            final_text=final_text,
            passed=passed,
            tools_offered_count=offered_count,
            tools_registered_count=registered_count,
            routing_retry=routing_retry,
            off_route_call=off_route_call,
            prompt_tokens=prompt_tokens,
            ttft_ms=round(elapsed_ms, 2),
        )


def format_markdown_report(report: ProbeReport) -> str:
    """Format full probe results into a clean markdown document."""
    lines: list[str] = []
    title = f"# ORBIT Intent Probe Report: {report.label}"
    if report.rerun_warning:
        title += f" - {report.rerun_warning}"
    lines.append(f"{title}\n")
    lines.append(f"- **Timestamp**: `{report.timestamp}`")
    lines.append(f"- **Git Commit**: `{report.commit}`")
    lines.append(f"- **Model**: `{report.model}`")
    lines.append(f"- **Temperature**: `{report.temperature}`")
    lines.append("- **RAG_AUTO**: `False`")
    lines.append(f"- **BROWSER_ENABLED**: `{'on' if report.browser_enabled else 'off'}`")
    lines.append(f"- **REMINDERS_ENABLED**: `{'on' if report.reminders_enabled else 'off'}`")
    lines.append(f"- **TOOL_ROUTING**: `{'on' if report.routing_enabled else 'off'}`")
    lines.append(f"- **ROUTINES_ENABLED**: `{'on' if report.routines_enabled else 'off'}`")
    lines.append(f"- **SYSTEM_CONTROLS_ENABLED**: `{'on' if report.system_controls_enabled else 'off'}`")
    lines.append(f"- **MESSAGING_ENABLED**: `{'on' if getattr(report, 'messaging_enabled', False) else 'off'}`")
    lines.append(f"- **Seeds Evaluated**: `{report.seeds}`")
    lines.append(f"- **System Prompt Source**: `{report.system_prompt_source}`\n")

    if report.rerun_warning:
        lines.append(f"> [!WARNING]\n> **{report.rerun_warning}**: This is a rerun of a reserved test set.\n")

    lines.append(
        "> [!NOTE]\n"
        "> **Notice**: The probe is a dev tool (not a held-out set). "
        "It provides rapid iteration feedback on model intent routing and argument validation "
        "across common desktop phrases without executing real tool functions.\n"
    )

    action_pct = (
        (report.action_pass_count / report.action_total_count * 100)
        if report.action_total_count
        else 0.0
    )
    notool_pct = (
        (report.notool_pass_count / report.notool_total_count * 100)
        if report.notool_total_count
        else 0.0
    )
    total_pct = (
        (report.total_pass_count / report.total_count * 100)
        if report.total_count
        else 0.0
    )

    lines.append("## Summary Metrics\n")
    lines.append("| Metric | Value | Detail |")
    lines.append("|---|---|---|")
    lines.append(
        f"| **Overall Action Pass Rate** (action phrases) | "
        f"{action_pct:.1f}% | {report.action_pass_count} / {report.action_total_count} |"
    )
    lines.append(
        f"| **No-Tool Pass Rate** ([none] phrases) | "
        f"{notool_pct:.1f}% | {report.notool_pass_count} / {report.notool_total_count} |"
    )
    lines.append(
        f"| **Overall Pass Rate** (all phrases) | "
        f"{total_pct:.1f}% | {report.total_pass_count} / {report.total_count} |"
    )
    lines.append(f"| **Median Tools Offered** | {report.median_tools_offered:.1f} | per turn |")
    lines.append(f"| **P95 Tools Offered** | {report.p95_tools_offered:.1f} | per turn |")
    prompt_str = f"{report.median_prompt_size:.0f} tokens" if report.median_prompt_size is not None else "n/a"
    lines.append(f"| **Median Prompt Size** | {prompt_str} | input tokens |")
    ttft_str = f"{report.median_ttft:.1f} ms" if report.median_ttft is not None else "n/a"
    lines.append(f"| **Median Time to First Token / Latency** | {ttft_str} | generation latency |")
    lines.append(f"| **Routing Retry Count** | {report.retry_count} | miss recovery retries |")
    lines.append(f"| **Off-Route Calls** | {report.off_route_call_count} | unoffered tools called |")
    lines.append("")

    lines.append("## Per-Phrase Results\n")
    lines.append("| # | Phrase | Accept List | Tools Offered | Tool Called | Pass | Retry | Off-Route |")
    lines.append("|---|---|---|---|---|---|---|---|")

    for idx, ps in enumerate(report.phrase_summaries, start=1):
        status = "PASS" if ps.pass_count == ps.total_runs else ("PARTIAL" if ps.pass_count > 0 else "FAIL")
        accept_str = ", ".join(f"`{a}`" for a in ps.accept_list) if ps.accept_list else "*(none)*"
        offered_str = f"{int(ps.median_tools_offered)}"
        last_tool = ps.runs[-1].tool_called if ps.runs else None
        called_str = f"`{last_tool}`" if last_tool else "*(none)*"
        retry_str = "yes" if ps.has_retry else "no"
        off_route_str = "yes" if ps.has_off_route else "no"
        lines.append(
            f"| {idx} | {ps.phrase} | {accept_str} | {offered_str} | {called_str} | {status} | {retry_str} | {off_route_str} |"
        )

    lines.append("\n## Detailed Run Logs\n")
    for idx, ps in enumerate(report.phrase_summaries, start=1):
        accept_str = ", ".join(ps.accept_list) if ps.accept_list else "(none)"
        lines.append(f"### {idx}. {ps.phrase}")
        lines.append(f"- **Accept List**: `{accept_str}`")
        if ps.prior_tools:
            lines.append(f"- **Prior Tools**: `{ps.prior_tools}`")
        lines.append(f"- **Pass Rate**: {ps.pass_count}/{ps.total_runs}")
        lines.append(f"- **Tools Offered**: {int(ps.median_tools_offered)}")
        lines.append(f"- **Routing Retry**: {'yes' if ps.has_retry else 'no'}")
        lines.append(f"- **Off-Route Call**: {'yes' if ps.has_off_route else 'no'}")
        for run in ps.runs:
            status_badge = "**PASS**" if run.passed else "**FAIL**"
            tool_str = f"`{run.tool_called}`" if run.tool_called else "*(none)*"
            args_str = json.dumps(run.args, ensure_ascii=False)
            safe_final = run.final_text.replace("\n", " ").strip()
            extra_bits = []
            if run.prompt_tokens is not None:
                extra_bits.append(f"prompt_tokens={run.prompt_tokens}")
            if run.ttft_ms is not None:
                extra_bits.append(f"latency={run.ttft_ms}ms")
            extra_line = f" ({', '.join(extra_bits)})" if extra_bits else ""
            lines.append(
                f"  - **Seed {run.seed}**: {status_badge} | Tool: {tool_str} | "
                f"Validation: `{run.validation_result}`{extra_line}\n"
                f"    - Args: `{args_str}`\n"
                f"    - Final text (first 150 chars): {json.dumps(safe_final)}"
            )
        lines.append("")

    return "\n".join(lines)


def run_intent_probe(
    provider_factory: Union[Callable[[int], LLMProvider], LLMProvider],
    registry: ToolRegistry,
    system_prompt: str,
    phrases: Sequence[Union[tuple[str, list[str]], tuple[str, list[str], Optional[list[str]]]]] = PROBE_PHRASES,
    seeds: Sequence[int] = (1, 2, 3),
    on_single_result: Optional[Callable[[SingleRunResult], None]] = None,
    label: str = "baseline",
    model_name: str = "qwen3:8b",
    system_prompt_source: str = "default",
    write_report: bool = True,
    output_path: Optional[Union[Path, str]] = None,
    browser_enabled: bool = False,
    reminders_enabled: bool = False,
    routing_enabled: bool = False,
    routines_enabled: bool = False,
    system_controls_enabled: bool = False,
    messaging_enabled: bool = False,
    rerun_warning: Optional[str] = None,
) -> ProbeReport:
    """Execute intent probe across phrases and seeds, recording dry-run tool calls and summary metrics."""
    phrase_summaries: list[PhraseSummary] = []

    for item in phrases:
        if len(item) == 2:
            phrase, accept_list = item  # type: ignore[misc]
            prior_tools = None
        else:
            phrase, accept_list, prior_tools = item  # type: ignore[misc]

        runs: list[SingleRunResult] = []
        for seed in seeds:
            if callable(provider_factory):
                provider = provider_factory(seed)
            else:
                provider = provider_factory

            res = run_single_phrase(
                provider=provider,
                registry=registry,
                system_prompt=system_prompt,
                phrase=phrase,
                accept_list=accept_list,
                seed=seed,
                prior_tools=prior_tools,
                routing_enabled=routing_enabled,
            )
            runs.append(res)
            if on_single_result:
                on_single_result(res)

        pass_count = sum(1 for r in runs if r.passed)
        phrase_summaries.append(
            PhraseSummary(
                phrase=phrase,
                accept_list=accept_list,
                pass_count=pass_count,
                total_runs=len(runs),
                runs=runs,
                prior_tools=prior_tools,
            )
        )

    action_pass_count = sum(ps.pass_count for ps in phrase_summaries if not ps.is_none_phrase)
    action_total_count = sum(ps.total_runs for ps in phrase_summaries if not ps.is_none_phrase)
    notool_pass_count = sum(ps.pass_count for ps in phrase_summaries if ps.is_none_phrase)
    notool_total_count = sum(ps.total_runs for ps in phrase_summaries if ps.is_none_phrase)
    total_pass_count = sum(ps.pass_count for ps in phrase_summaries)
    total_count = sum(ps.total_runs for ps in phrase_summaries)

    all_tools_offered = [r.tools_offered_count for ps in phrase_summaries for r in ps.runs]
    all_prompt_tokens = [r.prompt_tokens for ps in phrase_summaries for r in ps.runs if r.prompt_tokens is not None]
    all_ttft = [r.ttft_ms for ps in phrase_summaries for r in ps.runs if r.ttft_ms is not None]
    retry_count = sum(1 for ps in phrase_summaries for r in ps.runs if r.routing_retry)
    off_route_call_count = sum(1 for ps in phrase_summaries for r in ps.runs if r.off_route_call)

    median_tools_offered = compute_percentile(all_tools_offered, 50.0) if all_tools_offered else 0.0
    p95_tools_offered = compute_percentile(all_tools_offered, 95.0) if all_tools_offered else 0.0
    median_prompt_size = compute_percentile(all_prompt_tokens, 50.0) if all_prompt_tokens else None
    median_ttft = compute_percentile(all_ttft, 50.0) if all_ttft else None

    commit = get_git_info()
    now = datetime.now()
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    file_timestamp = now.strftime("%Y%m%d_%H%M%S")

    dest_file: Optional[Path] = None
    if write_report:
        if output_path is not None:
            dest_file = Path(output_path).resolve()
        else:
            clean_label = re.sub(r"[^\w\-]", "_", label.strip()) or "baseline"
            filename = f"intent-probe-{clean_label}-{file_timestamp}.md"
            dest_file = (Path(__file__).resolve().parent.parent / "evals" / "results" / filename).resolve()

        dest_file.parent.mkdir(parents=True, exist_ok=True)

    report = ProbeReport(
        commit=commit,
        timestamp=timestamp_str,
        label=label,
        model=model_name,
        temperature=0.2,
        seeds=list(seeds),
        system_prompt=system_prompt,
        system_prompt_source=system_prompt_source,
        phrase_summaries=phrase_summaries,
        action_pass_count=action_pass_count,
        action_total_count=action_total_count,
        notool_pass_count=notool_pass_count,
        notool_total_count=notool_total_count,
        total_pass_count=total_pass_count,
        total_count=total_count,
        output_path=dest_file,
        browser_enabled=browser_enabled,
        reminders_enabled=reminders_enabled,
        routing_enabled=routing_enabled,
        routines_enabled=routines_enabled,
        messaging_enabled=messaging_enabled,
        system_controls_enabled=system_controls_enabled,
        median_tools_offered=median_tools_offered,
        p95_tools_offered=p95_tools_offered,
        median_prompt_size=median_prompt_size,
        median_ttft=median_ttft,
        retry_count=retry_count,
        off_route_call_count=off_route_call_count,
        rerun_warning=rerun_warning,
    )

    if write_report and dest_file is not None:
        md_content = format_markdown_report(report)
        dest_file.write_text(md_content, encoding="utf-8")

    return report


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments for the intent probe tool."""
    parser = argparse.ArgumentParser(
        description="ORBIT dry-run intent probe: evaluate model intent routing without tool execution."
    )
    parser.add_argument(
        "--label",
        type=str,
        default="baseline",
        help="Label identifier for this probe run (default: 'baseline').",
    )
    parser.add_argument(
        "--system-prompt-file",
        type=str,
        default=None,
        help="Path to file containing custom system prompt to override the default.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Ollama model name (default: {getattr(settings, 'OLLAMA_MODEL', 'qwen3:8b')}).",
    )
    parser.add_argument(
        "--seeds",
        type=str,
        default="1,2,3",
        help="Comma-separated list of integer random seeds to run (default: '1,2,3').",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Explicit output path for markdown report (default: evals/results/intent-probe-<label>-<timestamp>.md).",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        default=False,
        help="Enable browser automation tools using FakeBackend (dry run).",
    )
    parser.add_argument(
        "--system-controls",
        action="store_true",
        default=False,
        help="Enable Windows system controls tools in the probe (default: disabled).",
    )
    parser.add_argument(
        "--reminders",
        action="store_true",
        default=False,
        help="Enable reminders tools in the probe (default: disabled).",
    )
    parser.add_argument(
        "--routing",
        action="store_true",
        default=False,
        help="Enable optional tool routing using core.routing (default: disabled).",
    )
    parser.add_argument(
        "--phrases",
        type=str,
        default=None,
        help="Path to JSON file containing phrases to evaluate (e.g. evals/routing/dev.json).",
    )
    parser.add_argument(
        "--final-test",
        action="store_true",
        default=False,
        help="Authorize running the reserved test split (evals/routing/test.json).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force rerun of reserved test split if already executed.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Run intent probe CLI with environment isolation."""
    env_keys = ["BROWSER_ENABLED", "REMINDERS_ENABLED", "ROUTINES_ENABLED", "SYSTEM_CONTROLS_ENABLED", "MESSAGING_ENABLED", "WINDOW_CONTROL_ENABLED", "FILES_ENABLED", "SCREENSHOTS_ENABLED", "TYPING_ENABLED", "SLEEP_ENABLED"]
    prev_env = {k: os.environ.get(k) for k in env_keys}
    try:
        return _main_impl(argv)
    finally:
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _main_impl(argv: Optional[list[str]] = None) -> int:
    """Inner implementation of intent probe CLI."""
    args = parse_args(argv)

    routing_enabled = bool(args.routing or os.getenv("TOOL_ROUTING", "0").lower() in ("1", "true", "yes", "on"))

    # Verify test split protection
    try:
        is_test_split, rerun_warning = check_test_split_protection(
            phrases_path=args.phrases,
            final_test=args.final_test,
            force=args.force,
            routing_enabled=routing_enabled,
        )
    except PermissionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.system_prompt_file:
        prompt_path = Path(args.system_prompt_file)
        if not prompt_path.exists():
            print(f"Error: system prompt file not found: {args.system_prompt_file}", file=sys.stderr)
            return 1
        system_prompt = prompt_path.read_text(encoding="utf-8")
        prompt_source = str(prompt_path)
    else:
        system_prompt = default_system_prompt()
        prompt_source = "default (core.bootstrap.default_system_prompt)"

    try:
        seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
        if not seeds:
            raise ValueError("No seeds specified")
    except Exception as exc:
        print(f"Error parsing --seeds: {exc}", file=sys.stderr)
        return 1

    model = args.model or getattr(settings, "OLLAMA_MODEL", "qwen3:8b")

    if args.browser:
        os.environ["BROWSER_ENABLED"] = "1"
        browser_enabled = True
    else:
        if "BROWSER_ENABLED" not in os.environ:
            os.environ["BROWSER_ENABLED"] = "0"
        browser_enabled = os.getenv("BROWSER_ENABLED", "0").lower() in ("1", "true", "yes", "on")

    if args.reminders:
        os.environ["REMINDERS_ENABLED"] = "1"
        reminders_enabled = True
    else:
        os.environ["REMINDERS_ENABLED"] = "0"
        reminders_enabled = False

    os.environ["ROUTINES_ENABLED"] = "0"
    routines_enabled = False

    os.environ["MESSAGING_ENABLED"] = "0"
    messaging_enabled = False
    for k in ("WINDOW_CONTROL_ENABLED", "FILES_ENABLED", "SCREENSHOTS_ENABLED", "TYPING_ENABLED", "SLEEP_ENABLED"):
        os.environ[k] = "0"

    if args.system_controls:
        os.environ["SYSTEM_CONTROLS_ENABLED"] = "1"
        system_controls_enabled = True
    else:
        os.environ["SYSTEM_CONTROLS_ENABLED"] = "0"
        system_controls_enabled = False

    try:
        active_phrases = load_probe_phrases(args.phrases, browser_enabled=browser_enabled)
    except Exception as exc:
        print(f"Error loading phrases: {exc}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir, browser_enabled=browser_enabled)

        def provider_factory(seed: int) -> LLMProvider:
            return OllamaProvider(
                model=model,
                temperature=0.2,
                seed=seed,
                think=False,
            )

        print("=" * 70)
        print("ORBIT INTENT PROBE")
        print("Note: The probe is a dev tool (not a held-out set)")
        print(f"Model: {model} | Temp: 0.2 | Seeds: {seeds}")
        print(f"System prompt: {prompt_source}")
        print(f"Browser tools: {'ENABLED (FakeBackend)' if browser_enabled else 'DISABLED'}")
        print(f"Reminders tools: {'ENABLED' if reminders_enabled else 'DISABLED'}")
        print(f"System controls: {'ENABLED' if system_controls_enabled else 'DISABLED'}")
        print(f"Tool routing: {'ENABLED' if routing_enabled else 'DISABLED'}")
        if rerun_warning:
            print(f"Warning: {rerun_warning}")
        print(f"Phrases: {len(active_phrases)} | Total turns: {len(active_phrases) * len(seeds)}")
        print("=" * 70)

        def on_single_result(res: SingleRunResult) -> None:
            status = "PASS" if res.passed else "FAIL"
            tool_info = f"tool={res.tool_called or '(none)'}"
            offered_info = f"offered={res.tools_offered_count}"
            val_info = f"val={res.validation_result[:30]}"
            retry_info = " [RETRY]" if res.routing_retry else ""
            off_route_info = " [OFF_ROUTE]" if res.off_route_call else ""
            final_snip = res.final_text.replace("\n", " ").strip()[:60]
            print(
                f"  [{status}] '{res.phrase}' (seed {res.seed}) -> "
                f"{offered_info} {tool_info}{retry_info}{off_route_info} {val_info} final=\"{final_snip}\""
            )

        report = run_intent_probe(
            provider_factory=provider_factory,
            registry=registry,
            system_prompt=system_prompt,
            phrases=active_phrases,
            seeds=seeds,
            on_single_result=on_single_result,
            label=args.label,
            model_name=model,
            system_prompt_source=prompt_source,
            write_report=True,
            output_path=args.output,
            browser_enabled=browser_enabled,
            reminders_enabled=reminders_enabled,
            routing_enabled=routing_enabled,
            routines_enabled=routines_enabled,
            messaging_enabled=messaging_enabled,
            system_controls_enabled=system_controls_enabled,
            rerun_warning=rerun_warning,
        )

        if is_test_split:
            record_test_run(routing_enabled=routing_enabled)

        print("\n" + "=" * 70)
        print("INTENT PROBE SUMMARY")
        print("=" * 70)
        action_pct = (
            (report.action_pass_count / report.action_total_count * 100)
            if report.action_total_count
            else 0.0
        )
        notool_pct = (
            (report.notool_pass_count / report.notool_total_count * 100)
            if report.notool_total_count
            else 0.0
        )
        total_pct = (
            (report.total_pass_count / report.total_count * 100)
            if report.total_count
            else 0.0
        )

        print(
            f"Overall Action Pass Rate (all except [none]): "
            f"{report.action_pass_count}/{report.action_total_count} ({action_pct:.1f}%)"
        )
        print(
            f"No-Tool Pass Rate ([none] phrases):           "
            f"{report.notool_pass_count}/{report.notool_total_count} ({notool_pct:.1f}%)"
        )
        print(
            f"Total Pass Rate (all phrases):                "
            f"{report.total_pass_count}/{report.total_count} ({total_pct:.1f}%)"
        )
        print(f"Median Tools Offered:                         {report.median_tools_offered:.1f}")
        print(f"P95 Tools Offered:                            {report.p95_tools_offered:.1f}")
        if report.median_prompt_size is not None:
            print(f"Median Prompt Tokens:                         {report.median_prompt_size:.0f}")
        if report.median_ttft is not None:
            print(f"Median Generation Latency:                    {report.median_ttft:.1f} ms")
        print(f"Routing Retries:                              {report.retry_count}")
        print(f"Off-Route Calls:                              {report.off_route_call_count}")
        print("-" * 70)
        print("Phrase Pass Counts:")
        for ps in report.phrase_summaries:
            status = (
                "PASS"
                if ps.pass_count == ps.total_runs
                else ("PARTIAL" if ps.pass_count > 0 else "FAIL")
            )
            accept_display = ", ".join(ps.accept_list) if ps.accept_list else "(none)"
            last_tool = ps.runs[-1].tool_called if ps.runs else None
            tool_display = last_tool or "(none)"
            print(
                f"  [{status:7s}] {ps.pass_count}/{ps.total_runs} (offered={int(ps.median_tools_offered)}) - "
                f"'{ps.phrase}' -> called: {tool_display} (accept: {accept_display})"
            )

        if report.output_path:
            print(f"\nReport written to: {report.output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
