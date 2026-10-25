from pathlib import Path

from core.llm import LLMProvider, LLMResponse
from evals.__main__ import parse_args
from evals.report import render_markdown
from evals.runner import run_case, run_suite
from evals.schema import EvalCase


class FakeProvider(LLMProvider):
    """Fake LLM provider for recording initialized parameters."""

    def __init__(self, seed: int = 42, temperature: float = 0.0, provider_name: str = "fake"):
        self.seed = seed
        self.temperature = temperature
        self.provider_name = provider_name
        self.provider = provider_name
        self.model = "fake-model"

    def generate(self, messages, tools=None, on_delta=None, should_stop=None) -> LLMResponse:
        return LLMResponse(text="Done.", tool_calls=[])


def test_cli_repeat_mode_and_temperature_args():
    """Verify that --repeat-mode and --temperature CLI flags parse properly."""
    args = parse_args(["--repeat-mode", "seeds", "--temperature", "0.7"])
    assert args.repeat_mode == "seeds"
    assert args.temperature == 0.7

    default_args = parse_args([])
    assert default_args.repeat_mode == "same"
    assert default_args.temperature is None


def test_provider_factory_receives_case_and_repeat_index():
    """Verify that run_case and run_suite pass repeat index k to provider_factory."""
    case = EvalCase(id="c_fake", category="chat", turns=["hello"])
    received_repeats: list[int] = []

    def factory(c: EvalCase, repeat: int = 0) -> FakeProvider:
        received_repeats.append(repeat)
        return FakeProvider(seed=100 + repeat, temperature=0.5)

    res = run_case(case, factory, repeat=3)
    assert res.status == "passed"
    assert received_repeats == [3]


def test_seeds_mode_with_fake_ollama_provider_seeds_and_temperature(tmp_path: Path):
    """Verify that repeat k uses seed = base_seed + k and temperature in seeds mode."""
    cases = [
        EvalCase(id="case1", category="chat", turns=["hi"]),
        EvalCase(id="case2", category="chat", turns=["hey"]),
    ]

    base_seed = 1000
    temperature = 0.3
    instantiated_providers: list[FakeProvider] = []

    def ollama_fake_factory(case: EvalCase, repeat: int = 0) -> FakeProvider:
        eff_seed = base_seed + repeat
        provider = FakeProvider(seed=eff_seed, temperature=temperature, provider_name="ollama")
        instantiated_providers.append(provider)
        return provider

    out_file = tmp_path / "results.json"
    results, quota_hit = run_suite(
        cases=cases,
        provider_factory=ollama_fake_factory,
        out_path=out_file,
        repeats=3,
        extra_meta={
            "repeat_mode": "seeds",
            "base_seed": base_seed,
            "seed": base_seed,
            "temperature": temperature,
            "provider": "ollama",
        },
    )

    assert not quota_hit
    assert len(results) == 6

    # Verify repeats 0, 1, 2 have corresponding seeds
    # planned sequence: repeat 0 (case1, case2), repeat 1 (case1, case2), repeat 2 (case1, case2)
    # Plus sample_provider check at beginning (repeat 0)
    # Filter only those used during run_case
    r0_providers = [p for p in instantiated_providers if p.seed == 1000]
    r1_providers = [p for p in instantiated_providers if p.seed == 1001]
    r2_providers = [p for p in instantiated_providers if p.seed == 1002]

    assert len(r0_providers) >= 2
    assert len(r1_providers) == 2
    assert len(r2_providers) == 2
    for p in instantiated_providers:
        assert p.temperature == 0.3


def test_meta_and_report_header_records_repeat_mode_base_seed_and_temp():
    """Verify mode, base seed, seed, and temperature appear in results meta and report header."""
    case = EvalCase(id="c_meta", category="chat", turns=["hi"])
    results_dict = {
        "meta": {
            "mode": "live",
            "provider": "ollama",
            "model": "qwen3:8b",
            "repeat_mode": "seeds",
            "temperature": 0.3,
            "base_seed": 42,
            "seed": 42,
            "repeats": 3,
        },
        "results": [
            {"id": "c_meta", "category": "chat", "repeat": 0, "status": "passed", "failed_checks": []},
            {"id": "c_meta", "category": "chat", "repeat": 1, "status": "passed", "failed_checks": []},
            {"id": "c_meta", "category": "chat", "repeat": 2, "status": "passed", "failed_checks": []},
        ],
    }

    report = render_markdown(results_dict, [case])
    assert "- **Repeat Mode**: `seeds`" in report
    assert "- **Temperature**: `0.3`" in report
    assert "- **Base Seed**: `42`" in report
    assert "- **Seed**: `42`" in report
    assert "repeats are identical" not in report


def test_note_printed_and_rendered_for_identical_repeats(capsys, monkeypatch, tmp_path: Path):
    """Verify that when repeats run at temp 0 with fixed seed, note is displayed and in report."""
    case = EvalCase(id="c_ident", category="chat", turns=["test"])
    results_dict = {
        "meta": {
            "mode": "live",
            "provider": "ollama",
            "model": "qwen3:8b",
            "repeat_mode": "same",
            "temperature": 0.0,
            "base_seed": 42,
            "seed": 42,
            "repeats": 2,
        },
        "results": [
            {"id": "c_ident", "category": "chat", "repeat": 0, "status": "passed", "failed_checks": []},
            {"id": "c_ident", "category": "chat", "repeat": 1, "status": "passed", "failed_checks": []},
        ],
    }

    report = render_markdown(results_dict, [case])
    expected_note = "repeats are identical; use --repeat-mode seeds to measure variation"
    assert f"> Note: {expected_note}" in report

    # Also test CLI main prints this message
    from evals import __main__ as evals_main

    test_argv = [
        "--repeat", "2",
        "--repeat-mode", "same",
        "--temperature", "0.0",
        "--ids", "c_ident",
        "--out", str(tmp_path),
    ]

    monkeypatch.setattr(evals_main, "load_cases", lambda p: [case])
    monkeypatch.setattr(evals_main, "run_suite", lambda **kw: ([], False))
    monkeypatch.setattr(evals_main, "render_markdown", lambda *a, **k: "")
    monkeypatch.setattr(evals_main, "aggregate", lambda *a, **k: {
        "overall": {
            "pass_rate": 1.0,
            "passed": 1,
            "n": 1,
            "avg_tool_calls": 0,
            "avg_llm_calls": 0,
            "p50_latency": 0,
            "p95_latency": 0,
            "not_run": 0,
        }
    })

    evals_main.main(test_argv)
    captured = capsys.readouterr()
    assert expected_note in captured.out
