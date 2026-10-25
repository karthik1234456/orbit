"""Dry-run intent probe for testing model tool-calling without executing tools.

Sends user phrases to the local LLM through the standard application system prompt,
tool declarations, and settings (RAG_AUTO off, temperature 0.2). Uses a fake executor
that runs ONLY argument validation and tool prechecks, returning 'ok (dry run)'
to the model to inspect its intent routing and final text.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Optional, Sequence, Union

# Ensure project root is in sys.path when executed directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from config.apps import ALLOWED_APPS
from core.bootstrap import AppPaths, build_app, default_system_prompt
from core.llm import LLMProvider, LLMResponse, ToolCall
from core.ollama_provider import OllamaProvider
from tools.registry import ToolRegistry

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


@dataclass
class PhraseSummary:
    """Aggregated probe results for a single phrase across all tested seeds."""

    phrase: str
    accept_list: list[str]
    pass_count: int
    total_runs: int
    runs: list[SingleRunResult] = field(default_factory=list)

    @property
    def is_none_phrase(self) -> bool:
        """Return True if phrase expects no tool call."""
        return self.accept_list == ["none"]


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


def build_probe_registry(workspace_path: Union[str, Path]) -> ToolRegistry:
    """Build ToolRegistry with standard desktop, file, app, memory, and doc tools."""
    paths = AppPaths.under(workspace_path)

    class _DummyProvider(LLMProvider):
        def generate(self, messages, tools=None, on_delta=None, should_stop=None) -> LLMResponse:
            return LLMResponse(text="")

    app = build_app(provider=_DummyProvider(), paths=paths)
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


def run_single_phrase(
    provider: LLMProvider,
    registry: ToolRegistry,
    system_prompt: str,
    phrase: str,
    accept_list: list[str],
    seed: int,
) -> SingleRunResult:
    """Send one message to the model and record tool intent without executing tools.

    Runs ONLY the tool's precheck (if any) and the registry's argument validation,
    and returns 'ok (dry run)' to the model to retrieve its final conversational text.
    """
    tools = registry.list_tools()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": phrase},
    ]

    response = provider.generate(messages, tools=tools)

    if response.tool_calls:
        first_tc = response.tool_calls[0]
        tool_name = first_tc.name
        tool_args = first_tc.args if isinstance(first_tc.args, dict) else {}

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

        followup_resp = provider.generate(followup_messages, tools=tools)
        raw_final = followup_resp.text or ""
        final_text = raw_final[:150]

        # Pass criteria:
        # Pass if the first tool call is in the phrase's accept list, validation passes,
        # and (for "none" phrases) no tool was called.
        if accept_list == ["none"]:
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
        )
    else:
        # No tool was called by the model
        raw_final = response.text or ""
        final_text = raw_final[:150]

        val_passed = True
        val_result = "n/a"

        if accept_list == ["none"]:
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
        )


def format_markdown_report(report: ProbeReport) -> str:
    """Format full probe results into a clean markdown document."""
    lines: list[str] = []
    lines.append(f"# ORBIT Intent Probe Report: {report.label}\n")
    lines.append(f"- **Timestamp**: `{report.timestamp}`")
    lines.append(f"- **Git Commit**: `{report.commit}`")
    lines.append(f"- **Model**: `{report.model}`")
    lines.append(f"- **Temperature**: `{report.temperature}`")
    lines.append(f"- **RAG_AUTO**: `False`")
    lines.append(f"- **Seeds Evaluated**: `{report.seeds}`")
    lines.append(f"- **System Prompt Source**: `{report.system_prompt_source}`\n")
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
    lines.append("| Metric | Passed / Total | Pass Rate |")
    lines.append("|---|---|---|")
    lines.append(
        f"| **Overall Action Pass Rate** (all except [none]) | "
        f"{report.action_pass_count} / {report.action_total_count} | {action_pct:.1f}% |"
    )
    lines.append(
        f"| **No-Tool Pass Rate** ([none] phrases) | "
        f"{report.notool_pass_count} / {report.notool_total_count} | {notool_pct:.1f}% |"
    )
    lines.append(
        f"| **Overall Pass Rate** (all phrases) | "
        f"{report.total_pass_count} / {report.total_count} | {total_pct:.1f}% |\n"
    )

    lines.append("## Per-Phrase Results\n")
    lines.append("| # | Phrase | Accept List | Pass Count | Status |")
    lines.append("|---|---|---|---|---|")

    for idx, ps in enumerate(report.phrase_summaries, start=1):
        status = "PASS" if ps.pass_count == ps.total_runs else ("PARTIAL" if ps.pass_count > 0 else "FAIL")
        accept_str = ", ".join(f"`{a}`" for a in ps.accept_list)
        lines.append(f"| {idx} | {ps.phrase} | {accept_str} | {ps.pass_count}/{ps.total_runs} | {status} |")

    lines.append("\n## Detailed Run Logs\n")
    for idx, ps in enumerate(report.phrase_summaries, start=1):
        accept_str = ", ".join(ps.accept_list)
        lines.append(f"### {idx}. {ps.phrase}")
        lines.append(f"- **Accept List**: `{accept_str}`")
        lines.append(f"- **Pass Rate**: {ps.pass_count}/{ps.total_runs}")
        for run in ps.runs:
            status_badge = "**PASS**" if run.passed else "**FAIL**"
            tool_str = f"`{run.tool_called}`" if run.tool_called else "*(none)*"
            args_str = json.dumps(run.args, ensure_ascii=False)
            safe_final = run.final_text.replace("\n", " ").strip()
            lines.append(
                f"  - **Seed {run.seed}**: {status_badge} | Tool: {tool_str} | "
                f"Validation: `{run.validation_result}`\n"
                f"    - Args: `{args_str}`\n"
                f"    - Final text (first 150 chars): {json.dumps(safe_final)}"
            )
        lines.append("")

    return "\n".join(lines)


def run_intent_probe(
    provider_factory: Union[Callable[[int], LLMProvider], LLMProvider],
    registry: ToolRegistry,
    system_prompt: str,
    phrases: Sequence[tuple[str, list[str]]] = PROBE_PHRASES,
    seeds: Sequence[int] = (1, 2, 3),
    on_single_result: Optional[Callable[[SingleRunResult], None]] = None,
    label: str = "baseline",
    model_name: str = "qwen3:8b",
    system_prompt_source: str = "default",
    write_report: bool = True,
    output_path: Optional[Union[Path, str]] = None,
) -> ProbeReport:
    """Execute intent probe across phrases and seeds, recording dry-run tool calls and summary metrics."""
    phrase_summaries: list[PhraseSummary] = []

    for phrase, accept_list in phrases:
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
            )
        )

    action_pass_count = sum(ps.pass_count for ps in phrase_summaries if not ps.is_none_phrase)
    action_total_count = sum(ps.total_runs for ps in phrase_summaries if not ps.is_none_phrase)
    notool_pass_count = sum(ps.pass_count for ps in phrase_summaries if ps.is_none_phrase)
    notool_total_count = sum(ps.total_runs for ps in phrase_summaries if ps.is_none_phrase)
    total_pass_count = sum(ps.pass_count for ps in phrase_summaries)
    total_count = sum(ps.total_runs for ps in phrase_summaries)

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
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Run intent probe CLI."""
    args = parse_args(argv)

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

    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = build_probe_registry(tmp_dir)

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
        print(f"Phrases: {len(PROBE_PHRASES)} | Total turns: {len(PROBE_PHRASES) * len(seeds)}")
        print("=" * 70)

        def on_single_result(res: SingleRunResult) -> None:
            status = "PASS" if res.passed else "FAIL"
            tool_info = f"tool={res.tool_called or '(none)'}"
            val_info = f"val={res.validation_result[:30]}"
            final_snip = res.final_text.replace("\n", " ").strip()[:60]
            print(f"  [{status}] '{res.phrase}' (seed {res.seed}) -> {tool_info} {val_info} final=\"{final_snip}\"")

        report = run_intent_probe(
            provider_factory=provider_factory,
            registry=registry,
            system_prompt=system_prompt,
            phrases=PROBE_PHRASES,
            seeds=seeds,
            on_single_result=on_single_result,
            label=args.label,
            model_name=model,
            system_prompt_source=prompt_source,
            write_report=True,
            output_path=args.output,
        )

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
        print("-" * 70)
        print("Phrase Pass Counts:")
        for ps in report.phrase_summaries:
            status = (
                "PASS"
                if ps.pass_count == ps.total_runs
                else ("PARTIAL" if ps.pass_count > 0 else "FAIL")
            )
            print(
                f"  [{status:7s}] {ps.pass_count}/{ps.total_runs} - "
                f"'{ps.phrase}' (accept: {', '.join(ps.accept_list)})"
            )

        if report.output_path:
            print(f"\nReport written to: {report.output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
