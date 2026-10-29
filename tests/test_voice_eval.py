"""Unit tests for the voice evaluation suite, noise synthesis, eligibility, and verdict logic."""

import json
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np

from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.chains import parse_chain
from core.shortcuts import parse_shortcut
from evals.voice_eval import (
    EvalMetrics,
    apply_eligibility_rules,
    compute_wer,
    evaluate_verdict_criteria,
    run_voice_final_test,
    run_voice_sweep,
    synthesize_noise,
)
from voice.vocab import build_vocab_prompt


def test_vocab_prompt_only_config_keys_and_length():
    """Verify vocabulary prompt fits 200 characters and contains only registered keys/verbs."""
    prompt = build_vocab_prompt(max_chars=200)
    assert len(prompt) <= 200
    assert prompt.startswith("ORBIT")

    words = [w.strip() for w in prompt.split(",")]
    allowed_vocab = (
        {k.lower() for k in ALLOWED_APPS.keys()}
        | {s.lower() for s in SITES.keys()}
        | {
            "orbit", "open", "close", "launch", "search", "click", "read",
            "scroll", "pause", "play", "remind", "timer", "routine"
        }
    )
    for w in words:
        assert w.lower() in allowed_vocab, f"Word '{w}' not in allowed vocabulary"


def test_defaults_reproduce_transcribe_arguments():
    """Verify WhisperTranscriber defaults pass the exact same arguments as previous version."""
    from voice.stt import WhisperTranscriber

    trans = WhisperTranscriber()
    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([], MagicMock(language="en"))
    trans._model = mock_model

    dummy_audio = np.zeros(16000, dtype=np.float32)
    trans.transcribe(dummy_audio, sample_rate=16000)

    # Check kwargs passed to model.transcribe
    call_args, call_kwargs = mock_model.transcribe.call_args
    assert call_kwargs == {
        "language": "en",
        "vad_filter": True,
        "beam_size": 1,
        "condition_on_previous_text": False,
    }


def test_noise_synthesis_target_snr_and_determinism():
    """Verify synthetic noise achieves target SNR within 0.5 dB and is deterministic."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
    speech = np.zeros(sr, dtype=np.float32)
    # Active speech in center
    speech[4000:12000] = 0.4 * np.sin(2 * np.pi * 300 * t[:8000])

    for ntype in ("white", "pink", "hum"):
        for target in (10.0, 5.0):
            mixed1, snr1 = synthesize_noise(speech, noise_type=ntype, target_snr_db=target, seed=123, sr=sr)
            mixed2, snr2 = synthesize_noise(speech, noise_type=ntype, target_snr_db=target, seed=123, sr=sr)

            assert np.array_equal(mixed1, mixed2), f"Non-deterministic for {ntype}"
            assert abs(snr1 - target) <= 0.5, f"SNR error {snr1} vs {target} for {ntype}"


def test_wer_normalization_and_distance():
    """Verify WER computation handles digits, punctuation, and synonyms."""
    assert compute_wer("open notepad", "open notepad") == 0.0
    assert compute_wer("timer for 5 minutes", "timer for five minutes") == 0.0
    assert compute_wer("ok orbit", "okay orbit") == 0.0
    assert compute_wer("launch calculator.", "Launch Calculator!") == 0.0

    # 1 word wrong out of 4 words = 25% WER
    wer_one_diff = compute_wer("open youtube then search", "open google then search")
    assert abs(wer_one_diff - 0.25) < 1e-4


def test_eligibility_and_combined_assembly_rules():
    """Verify candidate eligibility rules, including when highest-scoring config is ineligible."""
    baseline = EvalMetrics(
        command_match=0.70,
        wrong_command_rate=0.02,
        wer=0.15,
        median_time=1.8,
        clean_command_match=0.95,
        noisy_command_match=0.50,
        counts=48,
    )

    candidates = {
        "baseline": baseline,
        # Eligible: improves noisy match, clean preserved, wrong low, latency good
        "filter": EvalMetrics(0.75, 0.02, 0.12, 1.9, 0.95, 0.58, counts=48),
        # Eligible: improves noisy match, clean drop <= 1 phrase (1/24 = ~0.04), latency good
        "gate": EvalMetrics(0.76, 0.025, 0.11, 2.1, 0.92, 0.60, counts=48),
        # INELIGIBLE: highest noisy score (0.75), but latency 5.8s exceeds 5.0s ceiling!
        "beam5": EvalMetrics(0.85, 0.02, 0.08, 5.8, 0.96, 0.75, counts=48),
        # INELIGIBLE: improves noisy score, but wrong-command rate jumped by 3 points (> 1 pt higher)!
        "small": EvalMetrics(0.78, 0.06, 0.10, 3.5, 0.95, 0.62, counts=48),
    }

    eligibility, reasons, combined = apply_eligibility_rules(baseline, candidates)

    assert eligibility["filter"] is True
    assert eligibility["gate"] is True
    assert eligibility["beam5"] is False  # Ineligible due to latency > 5.0s
    assert "latency" in reasons["beam5"].lower()
    assert eligibility["small"] is False  # Ineligible due to wrong-command rate
    assert "wrong-command" in reasons["small"].lower()

    # Combined configuration only includes eligible components
    assert combined.highpass == 1
    assert combined.denoise == 1
    assert combined.beam == 1  # beam5 was ineligible!
    assert combined.model == "base.en"  # small was ineligible!


def test_sweep_refuses_test_split():
    """Verify sweep refuses to run against test split."""
    ret = run_voice_sweep(split="test")
    assert ret == 1


def test_final_test_marker_and_force_logic(tmp_path):
    """Verify final-test marker recording, refusal of second run without --force, and force tag."""
    eval_dir = tmp_path / "voice_eval"
    results_dir = tmp_path / "results"

    # 1. Refuses without --final-test flag
    assert run_voice_final_test("baseline", split="test", final_test=False, data_dir=str(eval_dir)) == 1

    # 2. First run creates marker file
    ret1 = run_voice_final_test(
        "baseline",
        split="test",
        final_test=True,
        force=False,
        data_dir=str(eval_dir),
        out_dir=str(results_dir),
    )
    assert ret1 == 0
    marker = eval_dir / ".ran-baseline"
    assert marker.exists()
    marker_text = marker.read_text(encoding="utf-8")
    assert "config: baseline" in marker_text

    # 3. Second run without --force is refused
    ret2 = run_voice_final_test(
        "baseline",
        split="test",
        final_test=True,
        force=False,
        data_dir=str(eval_dir),
        out_dir=str(results_dir),
    )
    assert ret2 == 1

    # 4. Second run with --force succeeds and marks report RERUN: NOT UNSEEN
    ret3 = run_voice_final_test(
        "baseline",
        split="test",
        final_test=True,
        force=True,
        data_dir=str(eval_dir),
        out_dir=str(results_dir),
    )
    assert ret3 == 0
    report_files = list(results_dir.glob("voice-test-baseline-*.md"))
    assert len(report_files) >= 2
    rerun_reports = [f for f in report_files if "RERUN: NOT UNSEEN" in f.read_text(encoding="utf-8")]
    assert len(rerun_reports) == 1


def test_verdict_criteria_pass_and_failures():
    """Verify pre-registered verdict criteria: passing and failing each specific condition."""
    base = EvalMetrics(0.70, 0.02, 0.15, 2.0, clean_command_match=0.95, noisy_command_match=0.50, real_noise_command_match=0.50)

    # 1. PASS: Noisy +8 pts, wrong +0.5 pt, clean drop 0, latency 2.5s, real noise +2 pts
    cand_pass = EvalMetrics(0.78, 0.025, 0.10, 2.5, clean_command_match=0.95, noisy_command_match=0.58, real_noise_command_match=0.52)
    passed, checks = evaluate_verdict_criteria(base, cand_pass)
    assert passed is True
    assert all(p for p, _ in checks.values())

    # 2. FAIL: Noisy gain only +4 pts (required: >= +6.0)
    cand_low_noisy = EvalMetrics(0.74, 0.02, 0.12, 2.2, clean_command_match=0.95, noisy_command_match=0.54, real_noise_command_match=0.50)
    p_c1, checks_c1 = evaluate_verdict_criteria(base, cand_low_noisy)
    assert p_c1 is False
    assert checks_c1["noisy_improvement"][0] is False

    # 3. FAIL: Wrong-command rate increased by 2 points (allowed: <= +1.0)
    cand_high_wrong = EvalMetrics(0.78, 0.045, 0.10, 2.5, clean_command_match=0.95, noisy_command_match=0.58, real_noise_command_match=0.50)
    p_c2, checks_c2 = evaluate_verdict_criteria(base, cand_high_wrong)
    assert p_c2 is False
    assert checks_c2["wrong_command_rate"][0] is False

    # 4. FAIL: Clean match dropped by 3 phrases (drop > 1/24)
    cand_clean_drop = EvalMetrics(0.75, 0.02, 0.12, 2.2, clean_command_match=0.80, noisy_command_match=0.58, real_noise_command_match=0.50)
    p_c3, checks_c3 = evaluate_verdict_criteria(base, cand_clean_drop)
    assert p_c3 is False
    assert checks_c3["clean_preservation"][0] is False

    # 5. FAIL: Median latency 5.5s (allowed: <= 5.0s)
    cand_slow = EvalMetrics(0.78, 0.02, 0.10, 5.5, clean_command_match=0.95, noisy_command_match=0.58, real_noise_command_match=0.50)
    p_c4, checks_c4 = evaluate_verdict_criteria(base, cand_slow)
    assert p_c4 is False
    assert checks_c4["median_latency"][0] is False

    # 6. FAIL: Real-noise subset got worse (0.45 vs 0.50)
    cand_bad_real = EvalMetrics(0.78, 0.02, 0.10, 2.5, clean_command_match=0.95, noisy_command_match=0.58, real_noise_command_match=0.45)
    p_c5, checks_c5 = evaluate_verdict_criteria(base, cand_bad_real)
    assert p_c5 is False
    assert checks_c5["real_noise_subset"][0] is False


def test_voice_phrases_file_integrity_and_disjoint_splits():
    """Verify all 48 phrases parse with shortcuts/chains and splits use disjoint sites/apps."""
    phrases_path = Path("evals/voice_phrases.json")
    assert phrases_path.exists()

    data = json.loads(phrases_path.read_text(encoding="utf-8"))
    dev_items = data.get("dev", [])
    test_items = data.get("test", [])

    assert len(dev_items) == 24
    assert len(test_items) == 24

    for item in dev_items + test_items:
        text = item["phrase"]
        parsed = parse_shortcut(text, allowed_apps=ALLOWED_APPS, sites=SITES, browser_enabled=True)
        if parsed is None:
            parsed = parse_chain(text, allowed_apps=ALLOWED_APPS, sites=SITES, browser_enabled=True)
        assert parsed is not None, f"Phrase failed to parse: '{text}'"

    # Verify disjoint apps and sites between splits
    dev_phrases = [it["phrase"].lower() for it in dev_items]
    test_phrases = [it["phrase"].lower() for it in test_items]

    dev_sites_used = {s for s in SITES if any(s in p for p in dev_phrases)}
    test_sites_used = {s for s in SITES if any(s in p for p in test_phrases)}
    assert dev_sites_used.isdisjoint(test_sites_used), (
        f"Sites must be disjoint: dev={dev_sites_used}, test={test_sites_used}"
    )

    dev_apps_used = {a for a in ALLOWED_APPS if any(a in p for p in dev_phrases)}
    test_apps_used = {a for a in ALLOWED_APPS if any(a in p for p in test_phrases)}
    assert dev_apps_used.isdisjoint(test_apps_used), (
        f"Apps must be disjoint: dev={dev_apps_used}, test={test_apps_used}"
    )


def test_recording_script_refuses_demo_mode(monkeypatch):
    """Verify scripts/voice_record.py refuses to run when in demo mode."""
    import scripts.voice_record as record_mod

    monkeypatch.setattr(record_mod, "is_demo_mode", lambda: True)
    ret = record_mod.main([])
    assert ret == 1


def test_diagnostics_voice_section_no_audio_or_text():
    """Verify diagnostics includes enhancement settings and SNR, but no audio or user text."""
    from core.diagnostics import build_diagnostics_text

    mock_vc = MagicMock()
    mock_vc.last_snr_db = 18.5
    mock_vc.last_review_reason = "Noisy room: a headset helps"

    diag_text = build_diagnostics_text(voice_controller=mock_vc)
    assert "=== VOICE ===" in diag_text
    assert "Last Estimated SNR: 18.5 dB" in diag_text
    assert "Last Review Reason: Noisy room: a headset helps" in diag_text
    assert "Highpass=" in diag_text
    assert "AGC=" in diag_text
    assert "Denoise=" in diag_text

    # Must never include private audio array representations or transcripts
    assert "numpy.ndarray" not in diag_text
    assert "array([" not in diag_text
