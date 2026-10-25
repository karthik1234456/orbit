"""Evaluation runner executing test cases and test suites in isolated sandboxes."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Optional, Sequence, Union

from config import settings
from core.bootstrap import Limits, build_app
from core.llm import LLMProvider, QuotaExceededError
from evals.checks import ConfirmationRecord, RunObservation, ToolEvent, run_checks
from evals.metrics import CaseResult
from evals.providers import CountingProvider
from evals.sandbox import Recorder, make_sandbox, no_real_launches
from evals.schema import EvalCase


def get_gpu_info(timeout: float = 3.0) -> tuple[str, str]:
    """Retrieve GPU name and total VRAM via nvidia-smi, or ('unknown', 'unknown') on failure."""
    try:
        res = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        line = res.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",", 1)]
        name = parts[0] if parts and parts[0] else "unknown"
        vram = parts[1] if len(parts) > 1 and parts[1] else "unknown"
        return name, vram
    except Exception:
        return "unknown", "unknown"


def run_case(
    case: EvalCase,
    provider_factory: Callable[[EvalCase], LLMProvider],
    repeat: int = 0,
    task_timeout: Optional[float] = None,
) -> CaseResult:
    """Execute a single evaluation case within an isolated sandbox and verify checks.

    Re-raises QuotaExceededError so caller can handle quota depletion across a suite.
    Any other LLMError or unexpected exception results in status='error'.
    """
    sandbox = make_sandbox(case)
    try:
        try:
            raw_provider = provider_factory(case, repeat)
        except TypeError:
            raw_provider = provider_factory(case)
        counting_provider = CountingProvider(raw_provider)

        is_ollama = (
            getattr(raw_provider, "provider", "") == "ollama"
            or type(raw_provider).__name__ == "OllamaProvider"
            or "ollama" in type(raw_provider).__name__.lower()
        )

        if task_timeout is not None:
            max_s = task_timeout
        elif "MAX_TASK_SECONDS" in os.environ:
            max_s = float(os.environ["MAX_TASK_SECONDS"])
        elif is_ollama:
            max_s = 300.0
        else:
            max_s = settings.MAX_TASK_SECONDS

        confirmations: list[ConfirmationRecord] = []
        current_turn: list[int] = [0]

        # Automatic answer for every CONFIRM prompt while recording requests
        def confirm_callback(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
            answer = getattr(case, "confirm", "approve") == "approve"
            confirmations.append(
                ConfirmationRecord(
                    tool=tool_name,
                    args=args,
                    warning=warning,
                    answer=answer,
                    turn=current_turn[0],
                )
            )
            return answer

        app = build_app(
            provider=counting_provider,
            confirm_callback=confirm_callback,
            paths=sandbox.paths,
            limits=Limits(
                max_tool_calls=settings.MAX_TOOL_CALLS,
                max_consecutive_errors=settings.MAX_CONSECUTIVE_ERRORS,
                max_seconds=max_s,
            ),
        )

        recorder = Recorder()
        final_texts: list[str] = []
        events: list[ToolEvent] = []
        last_run_status: Optional[str] = None
        error_msg: Optional[str] = None
        unverified_claims_count: int = 0

        t0 = time.perf_counter()

        with no_real_launches(recorder):
            for turn_idx, turn in enumerate(case.turns):
                current_turn[0] = turn_idx
                try:
                    reply = app.agent.chat(turn)
                    final_texts.append(reply)
                except QuotaExceededError:
                    raise
                except Exception as exc:
                    error_msg = str(exc)
                    final_texts.append(f"(error: {exc})")
                    break

                if app.agent.last_run:
                    if app.agent.last_run.status:
                        last_run_status = app.agent.last_run.status.value
                    unverified_claims_count += getattr(app.agent.last_run, "unverified_claims", 0)
                    for step in app.agent.last_run.steps:
                        events.append(
                            ToolEvent(
                                tool=step.tool,
                                args=step.args,
                                status=step.status,
                                turn=turn_idx,
                            )
                        )

        latency_s = time.perf_counter() - t0

        memory_texts: list[str] = []
        if app.memory_store:
            try:
                memory_texts = [m.content for m in app.memory_store.list_all(limit=1000)]
            except Exception:
                memory_texts = []

        obs = RunObservation(
            final_texts=final_texts,
            events=events,
            launches=recorder.launches,
            llm_calls=counting_provider.calls,
            latency_s=latency_s,
            workspace=sandbox.paths.workspace,
            sentinel_path=sandbox.sentinel_path,
            sentinel_text=sandbox.sentinel_text,
            root=sandbox.root,
            expected_root_entries=sandbox.expected_root_entries,
            memory_texts=memory_texts,
            confirmations=confirmations,
            unverified_claims=unverified_claims_count,
        )

        failed_checks = run_checks(case, obs)

        if error_msg is not None:
            status = "error"
        elif failed_checks:
            status = "failed"
        else:
            status = "passed"

        tools_called = [e.tool for e in events]
        tools_executed = [e.tool for e in events if e.status == "success"]
        final_text = final_texts[-1] if final_texts else ""

        return CaseResult(
            id=case.id,
            category=case.category,
            repeat=repeat,
            status=status,
            failed_checks=failed_checks,
            tools_called=tools_called,
            tools_executed=tools_executed,
            llm_calls=counting_provider.calls,
            latency_s=latency_s,
            run_status=last_run_status,
            final_text=final_text,
            error=error_msg,
            unverified_claims=obs.unverified_claims,
        )
    finally:
        sandbox.cleanup()


def _save_suite_results(out_path: Path, results: list[CaseResult], meta: dict[str, Any]) -> None:
    """Save evaluation results and metadata to JSON file, creating parent directories if needed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "results": [r.to_dict() for r in results],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_suite(
    cases: Sequence[EvalCase],
    provider_factory: Callable[[EvalCase], LLMProvider],
    out_path: Union[str, Path],
    repeats: int = 1,
    sleep_s: float = 0.0,
    already_done: Optional[set[tuple[str, int]]] = None,
    on_progress: Optional[Callable[[str], None]] = print,
    extra_meta: Optional[dict[str, Any]] = None,
) -> tuple[list[CaseResult], bool]:
    """Execute a collection of cases over multiple repeats, updating JSON output after each run.

    Returns:
        tuple[list[CaseResult], bool]: (all_results, quota_hit)
    """
    out_file = Path(out_path).resolve()
    done_set: set[tuple[str, int]] = set(already_done) if already_done else set()
    results: list[CaseResult] = []
    meta: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(cases),
        "repeats": repeats,
        "cases_version": 3,
    }
    if extra_meta:
        meta.update(extra_meta)

    meta.setdefault("cases_version", 3)

    if "cases_sha256" not in meta:
        from evals.schema import compute_cases_sha256
        if Path("evals/cases").exists():
            meta.setdefault("cases_sha256", compute_cases_sha256("evals/cases"))

    # Gather GPU info via nvidia-smi
    gpu_name, total_vram = get_gpu_info()
    meta.setdefault("gpu_name", gpu_name)
    meta.setdefault("total_vram", total_vram)
    meta.setdefault("gpu_vram", total_vram)
    if gpu_name != "unknown":
        meta.setdefault("gpu", f"{gpu_name} ({total_vram})")
    else:
        meta.setdefault("gpu", "unknown")

    # Detect provider metadata if not fully populated
    sample_provider = None
    if cases:
        try:
            try:
                sample_provider = provider_factory(cases[0], 0)
            except TypeError:
                sample_provider = provider_factory(cases[0])
        except Exception:
            pass

    is_ollama = False
    prov_name = meta.get("provider")
    if prov_name:
        is_ollama = (prov_name.lower() == "ollama")
    elif sample_provider:
        p_name = getattr(sample_provider, "provider", "") or type(sample_provider).__name__
        if p_name == "OllamaProvider" or "ollama" in p_name.lower():
            is_ollama = True
            meta.setdefault("provider", "ollama")
        elif "gemini" in p_name.lower():
            meta.setdefault("provider", "gemini")
        else:
            meta.setdefault("provider", "mock")

    if is_ollama:
        meta.setdefault("provider", "ollama")
        if sample_provider:
            model_val = getattr(
                sample_provider,
                "model",
                getattr(sample_provider, "model_name", settings.OLLAMA_MODEL),
            )
            meta.setdefault("model", model_val)
            meta.setdefault("num_ctx", getattr(sample_provider, "num_ctx", settings.OLLAMA_NUM_CTX))
            meta.setdefault("temperature", getattr(sample_provider, "temperature", 0.0))
            eff_seed = getattr(
                sample_provider,
                "seed",
                settings.OLLAMA_SEED if settings.OLLAMA_SEED is not None else 42,
            )
            meta.setdefault("seed", eff_seed)
            meta.setdefault("base_seed", getattr(sample_provider, "base_seed", eff_seed))
            meta.setdefault("repeat_mode", getattr(sample_provider, "repeat_mode", "same"))
            meta.setdefault("think", getattr(sample_provider, "think", settings.OLLAMA_THINK))
            host = getattr(sample_provider, "host", settings.OLLAMA_HOST)
            server_ver = getattr(sample_provider, "server_version", None)
            if server_ver is None:
                try:
                    from core.ollama_provider import server_version
                    server_ver = server_version(host)
                except Exception:
                    server_ver = None
            ver_str = server_ver or "unknown"
            meta.setdefault("ollama_server_version", ver_str)
            meta.setdefault("ollama_version", ver_str)

    if "MAX_TASK_SECONDS" in os.environ:
        task_timeout = float(os.environ["MAX_TASK_SECONDS"])
    elif is_ollama:
        task_timeout = 300.0
    else:
        task_timeout = settings.MAX_TASK_SECONDS

    meta.setdefault("task_timeout", task_timeout)
    meta.setdefault("task_timeout_s", task_timeout)

    # Load existing results if resuming
    if out_file.exists():
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                meta.update(data.get("meta", {}))
                for item in data.get("results", []):
                    res = CaseResult.from_dict(item)
                    results.append(res)
                    done_set.add((res.id, res.repeat))
        except Exception:
            pass

    # Build sequence of planned runs
    planned: list[tuple[EvalCase, int]] = []
    for r in range(repeats):
        for c in cases:
            if (c.id, r) not in done_set:
                planned.append((c, r))

    quota_hit = False

    for idx, (case, repeat) in enumerate(planned):
        try:
            res = run_case(case, provider_factory, repeat=repeat, task_timeout=task_timeout)
            results.append(res)
            _save_suite_results(out_file, results, meta)
            if on_progress:
                on_progress(
                    f"[{res.status.upper()}] {case.id} (repeat {repeat}) - {len(res.failed_checks)} failure(s)"
                )
        except QuotaExceededError:
            quota_hit = True
            if on_progress:
                on_progress(f"[QUOTA] Hit quota limit during {case.id} (repeat {repeat}). Stopping suite.")

            # Mark this case and all remaining planned cases as not_run
            for rem_case, rem_r in planned[idx:]:
                results.append(
                    CaseResult(
                        id=rem_case.id,
                        category=rem_case.category,
                        repeat=rem_r,
                        status="not_run",
                        error="QuotaExceededError",
                    )
                )
            _save_suite_results(out_file, results, meta)
            break

        if sleep_s > 0 and idx < len(planned) - 1:
            time.sleep(sleep_s)

    return results, quota_hit
