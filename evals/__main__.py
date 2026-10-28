"""CLI entry point for running ORBIT evaluations (python -m evals)."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Optional

from config import settings
from core.llm import GeminiProvider
from evals.metrics import aggregate
from evals.providers import GoldenProvider
from evals.report import render_markdown
from evals.runner import run_suite
from evals.schema import load_cases


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Configure and parse command line arguments for the evaluation harness."""
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Run evaluation benchmark suites for the ORBIT assistant.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Execute in LIVE mode using the specified --provider (default is MOCK mode with GoldenProvider).",
    )
    parser.add_argument(
        "--provider",
        type=str,
        choices=["gemini", "ollama"],
        default=None,
        help="LLM provider for live mode: 'gemini' or 'ollama' (default from LLM_PROVIDER if gemini/ollama, else gemini).",
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Filter evaluation cases by category.",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default=None,
        help="Comma-separated list of case IDs to run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of cases to evaluate.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Number of repetitions per case (default 1).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=None,
        help="Sleep duration in seconds between cases (default 0 in mock, 2 in live).",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Path to an existing results JSON file to resume from.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="evals/results",
        help="Output directory for results and markdown reports (default: evals/results).",
    )
    parser.add_argument(
        "--cases",
        type=str,
        default="evals/cases",
        help="Path or directory containing evaluation case JSON files (default: evals/cases).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip confirmation prompt before running live mode evaluations.",
    )
    parser.add_argument(
        "--holdout",
        nargs="?",
        const=True,
        default=False,
        help="Execute evaluation using holdout cases in evals/holdout (or specified directory e.g. --holdout holdout2, --holdout holdout3, or --holdout holdout4) instead of evals/cases.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force execution even if holdout integrity checks fail (e.g. rerun on holdout4).",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        default=False,
        help="Copy resulting markdown report to evals/REPORT.md (refused for mock, holdout, or quota stops).",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("A", "B"),
        help="Compare two evaluation result JSON files (e.g. --compare A.json B.json).",
    )
    parser.add_argument(
        "--repeat-mode",
        choices=["same", "seeds"],
        default="same",
        help="Repeat mode for evaluation: 'same' or 'seeds' (default: same).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature for the provider (default 0.0 in same mode, 0.3 in seeds mode).",
    )
    parser.add_argument(
        "--retrieval",
        type=str,
        choices=["dev", "test", "hard-dev", "hard-test", "guard-dev", "guard-test"],
        default=None,
        help="Execute retrieval evaluation on the specified split ('dev', 'test', 'hard-dev', 'hard-test', 'guard-dev', or 'guard-test').",
    )
    parser.add_argument(
        "--commands",
        type=str,
        choices=["dev", "test"],
        default=None,
        help="Execute command benchmark evaluation on the specified split ('dev' or 'test').",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        default=False,
        help="Run threshold parameter sweep across min_terms and coverage (allowed for dev splits only).",
    )
    parser.add_argument(
        "--sweep-methods",
        action="store_true",
        default=False,
        help="Run scoring methods sweep over fraction@0.5, rarest3@0.5, idf@0.4, idf@0.5, idf@0.6 (allowed for dev splits only; refuses test splits).",
    )
    parser.add_argument(
        "--report-name",
        type=str,
        default=None,
        help="Custom output filename for the retrieval markdown report.",
    )
    parser.add_argument(
        "--shortcuts",
        action="store_true",
        default=False,
        help="Enable command shortcuts for this evaluation run (default: disabled to evaluate the model directly).",
    )
    parser.add_argument(
        "--context",
        action="store_true",
        default=False,
        help="Enable session context tracking for this evaluation run (default: disabled to evaluate the model directly).",
    )
    parser.add_argument(
        "--routing",
        action="store_true",
        default=False,
        help="Enable optional tool routing for this evaluation run (default: disabled).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Main execution entry point for python -m evals."""
    args = parse_args(argv)

    import os
    if args.shortcuts:
        os.environ["COMMAND_SHORTCUTS"] = "1"
    else:
        os.environ["COMMAND_SHORTCUTS"] = "0"

    if args.context:
        os.environ["SESSION_CONTEXT"] = "1"
    else:
        os.environ["SESSION_CONTEXT"] = "0"

    if getattr(args, "routing", False):
        os.environ["TOOL_ROUTING"] = "1"
    else:
        os.environ["TOOL_ROUTING"] = "0"

    if args.retrieval:
        from evals.retrieval_eval import run_retrieval_eval

        return run_retrieval_eval(
            split=args.retrieval,
            sweep=args.sweep,
            sweep_methods=args.sweep_methods,
            out_dir=args.out,
            report_filename=args.report_name,
        )

    if args.commands:
        from evals.commands_eval import run_commands_eval

        return run_commands_eval(
            split=args.commands,
            force=args.force,
            out_dir=args.out,
            report_filename=args.report_name,
        )


    if args.compare:
        from evals.compare import compare_files

        code, _ = compare_files(args.compare[0], args.compare[1], out_dir=args.out)
        return code

    mode = "live" if args.live else "mock"

    # Determine provider
    if args.provider:
        provider_name = args.provider.lower()
    elif settings.LLM_PROVIDER in ("gemini", "ollama"):
        provider_name = settings.LLM_PROVIDER.lower()
    else:
        provider_name = "gemini"

    if args.live:
        if provider_name == "ollama":
            model_name = settings.OLLAMA_MODEL
        else:
            model_name = settings.MODEL_NAME
    else:
        model_name = "mock-golden-provider"

    ollama_server_ver = "unknown"
    base_seed = settings.OLLAMA_SEED if settings.OLLAMA_SEED is not None else 42
    if args.temperature is not None:
        eval_temperature = args.temperature
    else:
        eval_temperature = 0.3 if args.repeat_mode == "seeds" else 0.0

    # Resolve cases path
    if args.holdout:
        if isinstance(args.holdout, str) and args.holdout.strip():
            holdout_dir_str = args.holdout.strip()
            p = Path(holdout_dir_str)
            if not p.exists() and (Path("evals") / holdout_dir_str).exists():
                cases_path = Path("evals") / holdout_dir_str
            else:
                cases_path = p
        elif args.cases == "evals/cases":
            cases_path = Path("evals/holdout")
        else:
            cases_path = Path(args.cases)
    else:
        cases_path = Path(args.cases)

    if not cases_path.exists():
        print(f"Cases path '{cases_path}' not found. No cases to evaluate.", file=sys.stderr)
        return 1

    is_holdout4 = "holdout4" in str(cases_path).replace("\\", "/").lower()
    rerun_not_unseen = False
    if is_holdout4 and args.live:
        marker_file = Path("evals/holdout4/.ran")
        if marker_file.exists():
            if not args.force:
                print(
                    "Error: holdout4 has already been run live (marker evals/holdout4/.ran exists). "
                    "A second live run on holdout4 is refused to maintain unseen holdout integrity. "
                    "Use --force to override.",
                    file=sys.stderr,
                )
                return 1
            else:
                print(
                    "WARNING: holdout4 has already been run live. "
                    "With --force, holdout4 is now considered a dev set and is no longer an unseen test set."
                )
                rerun_not_unseen = True

    # In live mode, verify provider connectivity/keys
    if args.live:
        if provider_name == "gemini":
            try:
                GeminiProvider()
            except ValueError as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1
        elif provider_name == "ollama":
            from core.ollama_provider import (
                ModelNotFound,
                OllamaError,
                OllamaProvider,
                OllamaUnavailable,
                server_version,
            )

            try:
                warmup_provider = OllamaProvider(
                    model=model_name,
                    host=settings.OLLAMA_HOST,
                    num_ctx=settings.OLLAMA_NUM_CTX,
                    temperature=eval_temperature,
                    seed=base_seed,
                    think=settings.OLLAMA_THINK,
                    keep_alive=settings.OLLAMA_KEEP_ALIVE,
                    timeout_s=settings.OLLAMA_TIMEOUT_S,
                    allow_remote=settings.OLLAMA_ALLOW_REMOTE,
                    inline_tools=settings.OLLAMA_INLINE_TOOLS,
                )
                warmup_s = warmup_provider.warmup()
                print(f"Ollama warmup completed in {warmup_s:.2f}s")
                ver = server_version(settings.OLLAMA_HOST)
                if ver:
                    ollama_server_ver = ver
            except (OllamaUnavailable, ModelNotFound) as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1
            except OllamaError as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1

    # Load cases
    try:
        all_cases = load_cases(cases_path)
    except Exception as exc:
        print(f"Error loading evaluation cases: {exc}", file=sys.stderr)
        return 1

    total_dataset_cases = len(all_cases)
    cases = all_cases

    # Filter cases
    if args.category:
        cases = [c for c in cases if c.category.lower() == args.category.lower()]

    if args.ids:
        requested_ids = {i.strip() for i in args.ids.split(",") if i.strip()}
        cases = [c for c in cases if c.id in requested_ids]

    if args.limit and args.limit > 0:
        cases = cases[:args.limit]

    if not cases:
        print("No matching evaluation cases found.", file=sys.stderr)
        return 1

    is_partial = (len(cases) < total_dataset_cases) or bool(args.category or args.limit or args.ids)

    planned_runs = len(cases) * args.repeat
    estimated_requests = planned_runs * 3

    # Live mode interactive prompt
    if args.live and not args.yes:
        if provider_name == "ollama":
            print("ORBIT Evaluation Harness - Live Mode (Ollama)")
            print(f"Model: {model_name}")
            print(f"Context: {settings.OLLAMA_NUM_CTX}")
            print(f"Temperature: {eval_temperature}")
            print(f"Planned runs: {planned_runs} ({len(cases)} cases x {args.repeat} repeats)")
        else:
            print("ORBIT Evaluation Harness - Live Mode")
            print(f"Model: {model_name}")
            print(f"Planned runs: {planned_runs} ({len(cases)} cases x {args.repeat} repeats)")
            print(f"Estimated requests: ~{estimated_requests}")
        try:
            answer = input("Proceed? (y/n): ").strip().lower()
            if answer != "y":
                print("Aborted by user.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 0

    # Determine sleep
    if args.sleep is not None:
        sleep_s = args.sleep
    else:
        if not args.live or provider_name == "ollama":
            sleep_s = 0.0
        else:
            sleep_s = 2.0

    # Determine output path
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        out_file = Path(args.resume).resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = out_dir / f"{ts}_{mode}.json"

    # Define provider factory
    if args.live:
        if provider_name == "ollama":
            from core.ollama_provider import OllamaProvider

            def ollama_factory(c, r=0):
                eff_seed = (base_seed + r) if args.repeat_mode == "seeds" else base_seed
                return OllamaProvider(
                    model=model_name,
                    host=settings.OLLAMA_HOST,
                    num_ctx=settings.OLLAMA_NUM_CTX,
                    temperature=eval_temperature,
                    seed=eff_seed,
                    think=settings.OLLAMA_THINK,
                    keep_alive=settings.OLLAMA_KEEP_ALIVE,
                    timeout_s=settings.OLLAMA_TIMEOUT_S,
                    allow_remote=settings.OLLAMA_ALLOW_REMOTE,
                    inline_tools=settings.OLLAMA_INLINE_TOOLS,
                )

            provider_factory = ollama_factory
        else:
            provider_factory = lambda c, r=0: GeminiProvider()
    else:
        provider_factory = lambda c, r=0: GoldenProvider(c)

    is_holdout = bool(args.holdout or "holdout" in str(cases_path).replace("\\", "/").lower())
    from evals.schema import compute_cases_sha256
    cases_sha256 = compute_cases_sha256(cases_path)
    extra_meta: dict[str, Any] = {
        "mode": mode,
        "provider": provider_name if args.live else "mock",
        "model": model_name,
        "repeats": args.repeat,
        "repeat_mode": args.repeat_mode,
        "temperature": eval_temperature,
        "base_seed": base_seed,
        "seed": base_seed,
        "holdout": is_holdout,
        "total_cases": total_dataset_cases,
        "is_partial": is_partial,
        "cases_sha256": cases_sha256,
        "cases_version": 6,
        "RAG_AUTO": getattr(settings, "RAG_AUTO", True),
        "RAG_AUTO_MIN_TERMS": getattr(settings, "RAG_AUTO_MIN_TERMS", 2),
        "RAG_AUTO_MIN_COVERAGE": getattr(settings, "RAG_AUTO_MIN_COVERAGE", 0.5),
        "RAG_AUTO_TOP_K": getattr(settings, "RAG_AUTO_TOP_K", 3),
        "RAG_AUTO_MAX_CHARS": getattr(settings, "RAG_AUTO_MAX_CHARS", 1500),
        "COMMAND_SHORTCUTS": bool(args.shortcuts),
        "SESSION_CONTEXT": bool(args.context),
    }
    if rerun_not_unseen:
        extra_meta["rerun_not_unseen"] = True
    if args.live and provider_name == "ollama":
        extra_meta.update({
            "num_ctx": settings.OLLAMA_NUM_CTX,
            "think": settings.OLLAMA_THINK,
            "ollama_server_version": ollama_server_ver,
            "ollama_version": ollama_server_ver,
        })

    if args.repeat > 1 and eval_temperature == 0.0 and args.repeat_mode == "same":
        print("repeats are identical; use --repeat-mode seeds to measure variation")

    holdout_tag = " [HOLDOUT]" if is_holdout else ""
    print(f"\nStarting {mode.upper()} evaluation on {len(cases)} case(s) (repeats={args.repeat}){holdout_tag}...")
    print(f"Output destination: {out_file}\n")

    results, quota_hit = run_suite(
        cases=cases,
        provider_factory=provider_factory,
        out_path=out_file,
        repeats=args.repeat,
        sleep_s=sleep_s,
        on_progress=print,
        extra_meta=extra_meta,
    )

    # Render report
    try:
        data = json.loads(out_file.read_text(encoding="utf-8"))
        data.setdefault("meta", {})
        data["meta"].update(extra_meta)
        out_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        data = {
            "meta": extra_meta,
            "results": [r.to_dict() for r in results],
        }

    report_content = render_markdown(data, cases)
    report_file = out_file.with_suffix(".md")
    report_file.write_text(report_content, encoding="utf-8")
    print(f"\nReport written to: {report_file}")

    # Write holdout4 integrity marker after live run
    if is_holdout4 and args.live:
        marker_file = Path("evals/holdout4/.ran")
        try:
            from datetime import timezone
            date_str = datetime.now(timezone.utc).isoformat()
        except Exception:
            date_str = datetime.now().isoformat()
        import subprocess
        try:
            commit_res = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            commit_res = "unknown"
        marker_content = f"date: {date_str}\ncommit: {commit_res}\nmodel: {model_name}\n"
        try:
            marker_file.write_text(marker_content, encoding="utf-8")
        except Exception as e:
            print(f"Warning: Failed to write marker file {marker_file}: {e}", file=sys.stderr)

    # Print headline summary
    agg = aggregate(results, cases)
    overall = agg["overall"]

    def _fmt_pct(val: Optional[float]) -> str:
        if val is None:
            return "n/a (no cases in this run)"
        return f"{val * 100:.1f}%"

    print("\n" + "=" * 40)
    print("HEADLINE METRICS")
    print("=" * 40)
    print(f"Task success (all checks): {overall['pass_rate'] * 100:.1f}%")
    print(f"Outcome success (language checks not counted): {overall.get('outcome_pass_rate', overall['pass_rate']) * 100:.1f}%")
    print(f"Refusals clearly worded:   {overall.get('refusals_clearly_worded_headline', 'n/a')}")
    print(f"Tool Selection Accuracy:   {_fmt_pct(overall.get('tool_selection_accuracy'))}")
    print(f"Unnecessary Tool Rate:     {_fmt_pct(overall.get('unnecessary_tool_rate'))}")
    print(f"Safety Pass Rate:          {_fmt_pct(overall.get('safety_pass_rate'))}")
    print(f"Unverified Action Claims:  {overall.get('unverified_claims_headline', 'n/a')}")
    print(f"Average Tool Calls:        {overall['avg_tool_calls']:.2f}")
    print(f"Average LLM Calls:         {overall['avg_llm_calls']:.2f}")
    print(f"p50 Latency:               {overall['p50_latency']:.2f}s")
    print(f"p95 Latency:               {overall['p95_latency']:.2f}s")
    if overall["not_run"] > 0:
        print(f"Not Run:                   {overall['not_run']}")
    print("=" * 40)

    # Quota stop alert & resume command
    if quota_hit and provider_name != "ollama":
        print("\n[WARNING] Suite stopped early due to QuotaExceededError.")
        resume_cmd = (
            f"python -m evals --resume \"{out_file}\" "
            f"--cases \"{args.cases}\""
        )
        if args.live:
            resume_cmd += f" --live --provider {provider_name}"
        print("To resume remaining runs, execute:")
        print(f"  {resume_cmd}")

    # Handle publish
    if args.publish:
        if is_holdout:
            print("\nCannot publish report: holdout runs cannot be published to evals/REPORT.md.")
        elif mode == "mock":
            print("\nCannot publish report: mock runs cannot be published to evals/REPORT.md.")
        elif quota_hit:
            print("\nCannot publish report: run was stopped due to quota limit.")
        else:
            publish_dest = Path("evals/REPORT.md")
            publish_dest.write_text(report_content, encoding="utf-8")
            print(f"\nSuccessfully published report to: {publish_dest}")

    failures = [r for r in results if r.status in ("failed", "error")]
    if failures:
        from evals.checks import classify_check_failure
        from evals.schema import lookup_case_kind

        case_lookup = {c.id: c for c in cases}
        r_outcome = 0
        r_lang = 0
        for r in failures:
            case_kind = lookup_case_kind(r.id, case_lookup.get(r.id))
            if case_kind in ("refusal", "denial"):
                c_classes = [classify_check_failure(fc) for fc in r.failed_checks]
                if any(c == "outcome" for c in c_classes):
                    r_outcome += 1
                elif any(c == "language" for c in c_classes):
                    r_lang += 1
        print(f"Refusal-case failures: {r_outcome} outcome, {r_lang} language-only")

    # One-line PASS or FAIL summary
    passed = overall["passed"]
    total = overall["n"]
    pct = overall["pass_rate"] * 100

    if overall["pass_rate"] >= 1.0 and total > 0:
        print(f"\nPASS: {passed}/{total} cases passed ({pct:.1f}%)")
        status_pass = True
    else:
        print(f"\nFAIL: {passed}/{total} cases passed ({pct:.1f}%)")
        status_pass = False

    if not args.live and not status_pass:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
