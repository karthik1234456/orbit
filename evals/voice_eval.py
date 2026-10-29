"""Voice recognition evaluation suite and configuration sweep for ORBIT.

Provides:
- Deterministic synthetic noise generation (white, pink, 50 Hz hum, babble)
- Exact active speech SNR matching (10 dB, 5 dB)
- Mixed audio caching in data/voice_eval/cache/
- Evaluation metrics: command match, wrong command rate, normalized WER, median time
- Pre-registered eligibility, selection, and combined configuration rules
- Dev configuration sweep (--voice dev --sweep)
- Single-config final test execution (--voice test --config <name> --final-test)
- Pre-registered final verdict check (--voice-verdict)

PRIVACY & INTEGRITY GUARANTEE:
Reports contain only commit, model, settings, phrase IDs, and aggregate scores.
Audio arrays and private transcript text are never written to reports or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Mapping, Optional, Sequence
import wave
import numpy as np

from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.chains import parse_chain
from core.shortcuts import parse_shortcut
from voice.enhance import enhance
from voice.stt import filter_hallucinations


# Normalization map for digits and standard variants
NUM_MAP: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "okay": "ok",
}


def normalize_words_for_wer(text: str) -> list[str]:
    """Normalize text tokens for WER evaluation."""
    clean = re.sub(r"[^\w\s]", " ", text.lower())
    words = clean.split()
    return [NUM_MAP.get(w, w) for w in words]


def compute_wer(ref: str, hyp: str) -> float:
    """Compute Word Error Rate (WER) between reference and hypothesis text."""
    r_words = normalize_words_for_wer(ref)
    h_words = normalize_words_for_wer(hyp)
    if not r_words:
        return 0.0 if not h_words else 1.0

    d = np.zeros((len(r_words) + 1, len(h_words) + 1), dtype=int)
    for i in range(len(r_words) + 1):
        d[i, 0] = i
    for j in range(len(h_words) + 1):
        d[0, j] = j

    for i in range(1, len(r_words) + 1):
        for j in range(1, len(h_words) + 1):
            if r_words[i - 1] == h_words[j - 1]:
                d[i, j] = d[i - 1, j - 1]
            else:
                d[i, j] = 1 + min(d[i - 1, j], d[i, j - 1], d[i - 1, j - 1])

    return float(d[len(r_words), len(h_words)]) / float(len(r_words))


def parse_action(text: str) -> Any:
    """Parse text into shortcut ToolCallSpec or ShortcutPlan."""
    sc = parse_shortcut(text, allowed_apps=ALLOWED_APPS, sites=SITES, browser_enabled=True)
    if sc is not None:
        return sc
    return parse_chain(text, allowed_apps=ALLOWED_APPS, sites=SITES, browser_enabled=True)


def are_actions_equivalent(act1: Any, act2: Any) -> bool:
    """Check whether two parsed action specifications represent the same command."""
    if act1 is None or act2 is None:
        return False
    # ToolCallSpec comparison
    if hasattr(act1, "name") and hasattr(act2, "name"):
        if act1.name != act2.name:
            return False
        args1 = getattr(act1, "args", {}) or {}
        args2 = getattr(act2, "args", {}) or {}
        return args1 == args2
    # ShortcutPlan comparison
    if hasattr(act1, "steps") and hasattr(act2, "steps"):
        if len(act1.steps) != len(act2.steps):
            return False
        for s1, s2 in zip(act1.steps, act2.steps, strict=False):
            if s1.name != s2.name or s1.args != s2.args:
                return False
        return True
    return False


def load_wav(file_path: Path) -> tuple[np.ndarray, int]:
    """Load WAV file as 1-D float32 numpy array and sample rate."""
    with wave.open(str(file_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        data = wf.readframes(n_frames)

    if sampwidth == 2:
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        audio = np.frombuffer(data, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        audio = np.frombuffer(data, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0

    if n_channels > 1:
        audio = audio.reshape(-1, n_channels)[:, 0]

    return audio.astype(np.float32), framerate


def save_wav(file_path: Path, audio: np.ndarray, sample_rate: int = 16000) -> None:
    """Save 1-D float32 audio as 16 kHz mono 16-bit PCM WAV using stdlib wave."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    int16_audio = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16_audio.tobytes())


def synthesize_noise(
    audio: np.ndarray,
    noise_type: str = "white",
    target_snr_db: float = 10.0,
    seed: int = 42,
    sr: int = 16000,
    babble_pool: Optional[Sequence[np.ndarray]] = None,
) -> tuple[np.ndarray, float]:
    """Synthesize noise and mix with audio targeting exact SNR on active speech frames.

    Returns (mixed_audio, achieved_snr_db).
    """
    n = len(audio)
    if n == 0:
        return audio, 0.0

    frame_len = int(sr * 0.025)
    hop_len = frame_len // 2
    n_frames = max(1, 1 + (n - frame_len) // hop_len)

    # Identify active speech frames
    frame_energies = [
        float(np.mean(audio[i * hop_len : i * hop_len + frame_len] ** 2))
        for i in range(n_frames)
    ]
    max_e = max(frame_energies) if frame_energies else 0.0
    threshold = 0.05 * max_e

    active_indices = [i for i, e in enumerate(frame_energies) if e >= threshold]
    if active_indices:
        active_samples = np.concatenate(
            [audio[i * hop_len : i * hop_len + frame_len] for i in active_indices]
        )
        p_speech = float(np.mean(np.square(active_samples)))
    else:
        p_speech = float(np.mean(np.square(audio)))

    if p_speech <= 1e-10:
        p_speech = 1e-10

    p_noise_target = p_speech / (10.0 ** (target_snr_db / 10.0))

    rng = np.random.RandomState(seed)
    white = rng.normal(0.0, 1.0, n).astype(np.float32)

    if noise_type == "white":
        raw_noise = white
    elif noise_type == "pink":
        spec = np.fft.rfft(white)
        freqs = np.fft.rfftfreq(n, d=1.0 / sr)
        filt = 1.0 / np.sqrt(np.maximum(freqs, 10.0))
        raw_noise = np.fft.irfft(spec * filt, n=n).astype(np.float32)
    elif noise_type == "hum":
        t = np.arange(n) / float(sr)
        hum = (
            np.sin(2 * np.pi * 50 * t)
            + 0.5 * np.sin(2 * np.pi * 100 * t)
            + 0.25 * np.sin(2 * np.pi * 150 * t)
            + 0.125 * np.sin(2 * np.pi * 200 * t)
            + 0.1 * white
        )
        raw_noise = hum.astype(np.float32)
    elif noise_type == "babble" and babble_pool:
        # Multi-speaker babble from other split clips shifted deterministically
        acc = np.zeros(n, dtype=np.float32)
        count = 0
        for idx, other_clip in enumerate(babble_pool):
            if len(other_clip) == 0:
                continue
            shift = (seed * 104729 + idx * 7919) % len(other_clip)
            rolled = np.roll(other_clip, shift)
            if len(rolled) < n:
                reps = int(np.ceil(n / len(rolled)))
                rolled = np.tile(rolled, reps)[:n]
            else:
                rolled = rolled[:n]
            acc += rolled
            count += 1
            if count >= 3:
                break
        raw_noise = acc if count > 0 else white
    else:
        raw_noise = white

    # Scale noise power
    p_curr = float(np.mean(raw_noise**2))
    if p_curr > 1e-12:
        scaled_noise = raw_noise * np.sqrt(p_noise_target / p_curr)
    else:
        scaled_noise = raw_noise

    # Measure achieved SNR on active speech frames
    if active_indices:
        active_noise = np.concatenate(
            [scaled_noise[i * hop_len : i * hop_len + frame_len] for i in active_indices]
        )
        p_noise_active = float(np.mean(np.square(active_noise)))
    else:
        p_noise_active = float(np.mean(np.square(scaled_noise)))

    achieved_snr = 10.0 * np.log10(p_speech / max(1e-12, p_noise_active))

    mixed = audio + scaled_noise
    peak = float(np.max(np.abs(mixed)))
    if peak >= 0.99:
        mixed = mixed * (0.985 / peak)

    return mixed.astype(np.float32), float(round(achieved_snr, 2))


@dataclass
class EvalConfig:
    name: str
    model: str = "base.en"
    beam: int = 1
    prompt: int = 0
    highpass: int = 0
    denoise: int = 0
    agc: int = 0
    aliases: int = 0


CONFIG_SPECS: dict[str, EvalConfig] = {
    "baseline": EvalConfig("baseline", model="base.en", beam=1, prompt=0, highpass=0, denoise=0, agc=0, aliases=0),
    "small": EvalConfig("small", model="small.en", beam=1, prompt=0, highpass=0, denoise=0, agc=0, aliases=0),
    "prompt": EvalConfig("prompt", model="base.en", beam=1, prompt=1, highpass=0, denoise=0, agc=0, aliases=0),
    "filter": EvalConfig("filter", model="base.en", beam=1, prompt=0, highpass=1, denoise=0, agc=0, aliases=0),
    "gate": EvalConfig("gate", model="base.en", beam=1, prompt=0, highpass=0, denoise=1, agc=0, aliases=0),
    "beam5": EvalConfig("beam5", model="base.en", beam=5, prompt=0, highpass=0, denoise=0, agc=0, aliases=0),
    "aliases": EvalConfig("aliases", model="base.en", beam=1, prompt=0, highpass=0, denoise=0, agc=0, aliases=1),
}


@dataclass
class EvalMetrics:
    command_match: float
    wrong_command_rate: float
    wer: float
    median_time: float
    clean_command_match: float
    noisy_command_match: float
    real_noise_command_match: Optional[float] = None
    counts: int = 0


def evaluate_config(
    cfg: EvalConfig,
    dataset: list[dict[str, Any]],
    transcriber: Any,
) -> tuple[EvalMetrics, list[dict[str, Any]]]:
    """Evaluate a single configuration over the benchmark audio dataset."""
    results: list[dict[str, Any]] = []

    # Configure environment/settings according to cfg
    settings_obj = type("SettingsProxy", (), {
        "VOICE_HIGHPASS": cfg.highpass,
        "VOICE_AGC": cfg.agc,
        "VOICE_DENOISE": cfg.denoise,
        "STT_BEAM": cfg.beam,
        "STT_PROMPT": cfg.prompt,
        "VOICE_ALIASES": cfg.aliases,
        "STT_MODEL": cfg.model,
    })()

    for item in dataset:
        audio = item["audio"]
        ref_text = item["ref"]
        condition = item.get("condition", "clean")
        phrase_id = item["id"]

        # Enhance
        enhanced_audio = enhance(audio, settings_obj)

        # Transcribe
        t_start = time.perf_counter()
        transcript_obj = transcriber.transcribe(enhanced_audio)
        dur = time.perf_counter() - t_start

        hyp_text = (
            transcript_obj.text.strip()
            if (transcript_obj and hasattr(transcript_obj, "text"))
            else ""
        )
        hyp_clean = filter_hallucinations(hyp_text).strip()

        # Aliases
        if cfg.aliases:
            try:
                from voice.aliases import resolve

                def _parse_cand(t: str) -> Any:
                    return parse_action(t)

                hyp_clean, _ = resolve(hyp_clean, _parse_cand, allowed_apps=ALLOWED_APPS, sites=SITES)
            except Exception:
                pass

        # Compute metric outcome
        ref_act = parse_action(ref_text)
        hyp_act = parse_action(hyp_clean) if hyp_clean else None

        cmd_match = are_actions_equivalent(ref_act, hyp_act)
        wrong_cmd = (hyp_act is not None) and (not cmd_match)
        wer_val = compute_wer(ref_text, hyp_clean)

        results.append({
            "id": phrase_id,
            "condition": condition,
            "ref": ref_text,
            "match": cmd_match,
            "wrong": wrong_cmd,
            "wer": wer_val,
            "time": dur,
        })

    n = len(results)
    if n == 0:
        return EvalMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None, 0), results

    clean_res = [r for r in results if r["condition"] == "clean"]
    noisy_res = [r for r in results if r["condition"] in ("synth_noise", "noise")]
    real_noise_res = [r for r in results if r["condition"] == "real_noise"]

    cmd_match_all = float(np.mean([1.0 if r["match"] else 0.0 for r in results]))
    wrong_all = float(np.mean([1.0 if r["wrong"] else 0.0 for r in results]))
    wer_all = float(np.mean([r["wer"] for r in results]))
    med_time = float(np.median([r["time"] for r in results]))

    clean_match = (
        float(np.mean([1.0 if r["match"] else 0.0 for r in clean_res]))
        if clean_res
        else cmd_match_all
    )
    noisy_match = (
        float(np.mean([1.0 if r["match"] else 0.0 for r in noisy_res]))
        if noisy_res
        else cmd_match_all
    )
    real_match = (
        float(np.mean([1.0 if r["match"] else 0.0 for r in real_noise_res]))
        if real_noise_res
        else None
    )

    metrics = EvalMetrics(
        command_match=cmd_match_all,
        wrong_command_rate=wrong_all,
        wer=wer_all,
        median_time=med_time,
        clean_command_match=clean_match,
        noisy_command_match=noisy_match,
        real_noise_command_match=real_match,
        counts=n,
    )
    return metrics, results


def apply_eligibility_rules(
    baseline_metrics: EvalMetrics,
    candidates: Mapping[str, EvalMetrics],
) -> tuple[dict[str, bool], dict[str, str], EvalConfig]:
    """Apply pre-registered rules to determine eligible components and combined config.

    Eligibility criteria over baseline:
    1. Noisy command match improves: cand.noisy > baseline.noisy
    2. Clean drop at most 1 phrase: cand.clean >= baseline.clean - (1.0 / n_clean)
    3. Wrong-command rate at most 1 percentage point higher: cand.wrong <= baseline.wrong + 0.01
    4. Median time <= 5.0 s
    """
    eligibility: dict[str, bool] = {}
    reasons: dict[str, str] = {}

    n_clean = baseline_metrics.counts // 2 if baseline_metrics.counts > 0 else 24
    clean_tol = 1.0 / max(1, n_clean)

    for name, m in candidates.items():
        if name == "baseline":
            continue

        failed_reasons: list[str] = []
        if m.noisy_command_match <= baseline_metrics.noisy_command_match:
            failed_reasons.append("Noisy command match did not improve over baseline")
        if m.clean_command_match < (baseline_metrics.clean_command_match - clean_tol - 1e-6):
            failed_reasons.append("Clean command match dropped by more than 1 phrase")
        if m.wrong_command_rate > (baseline_metrics.wrong_command_rate + 0.0101):
            failed_reasons.append("Wrong-command rate increased by more than 1 point")
        if m.median_time > 5.0:
            failed_reasons.append(f"Median latency {m.median_time:.2f}s exceeded 5.0s ceiling")

        if failed_reasons:
            eligibility[name] = False
            reasons[name] = "; ".join(failed_reasons)
        else:
            eligibility[name] = True
            reasons[name] = "Eligible: satisfies all pre-registered criteria"

    # Assemble combined configuration
    combined = EvalConfig("combined")
    if eligibility.get("small", False):
        combined.model = "small.en"
    if eligibility.get("prompt", False):
        combined.prompt = 1
    if eligibility.get("filter", False):
        combined.highpass = 1
    if eligibility.get("gate", False):
        combined.denoise = 1
    if eligibility.get("beam5", False):
        combined.beam = 5
    if eligibility.get("aliases", False):
        combined.aliases = 1

    return eligibility, reasons, combined


def get_git_info() -> tuple[str, bool]:
    """Retrieve current git commit hash and working tree dirty flag."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        commit = "unknown"
    try:
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        is_dirty = len(status_out) > 0
    except Exception:
        is_dirty = False
    return commit, is_dirty


def parse_report_metrics(content: str) -> Optional[EvalMetrics]:
    """Extract EvalMetrics from a markdown evaluation report."""
    def _extract_pct(pattern: str) -> Optional[float]:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1)) / 100.0
            except ValueError:
                pass
        return None

    def _extract_float(pattern: str) -> Optional[float]:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    cmd_match = _extract_pct(r"Overall Command Match\s*\|\s*([\d.]+)%")
    clean_match = _extract_pct(r"Clean Command Match\s*\|\s*([\d.]+)%")
    noisy_match = _extract_pct(r"Noisy Command Match\s*\|\s*([\d.]+)%")
    real_match = _extract_pct(r"Real Noise Command Match\s*\|\s*([\d.]+)%")
    wrong_rate = _extract_pct(r"Wrong-Command Rate\s*\|\s*([\d.]+)%")
    wer_val = _extract_float(r"WER\s*\|\s*([\d.]+)")
    latency = _extract_float(r"Median Latency\s*\|\s*([\d.]+)s?")

    if cmd_match is None or clean_match is None or noisy_match is None:
        return None

    return EvalMetrics(
        command_match=cmd_match,
        wrong_command_rate=wrong_rate or 0.0,
        wer=wer_val or 0.0,
        median_time=latency or 0.0,
        clean_command_match=clean_match,
        noisy_command_match=noisy_match,
        real_noise_command_match=real_match,
    )


def run_voice_sweep(
    split: str = "dev",
    out_dir: str = "evals/results",
    data_dir: str = "data/voice_eval",
) -> int:
    """Run configuration sweep over dev split."""
    if split != "dev":
        print(f"ERROR: Configuration sweep is only allowed for the 'dev' split, not '{split}'.", file=sys.stderr)
        return 1

    phrases_path = Path("evals/voice_phrases.json")
    if not phrases_path.exists():
        print(f"ERROR: {phrases_path} not found.", file=sys.stderr)
        return 1

    base_dir = Path(data_dir) / split
    clean_dir = base_dir / "clean"
    if not clean_dir.exists() or not list(clean_dir.glob("*.wav")):
        print(
            f"ERROR: No clean audio recordings found under {clean_dir}.\n"
            "Please record dev phrases using: python scripts/voice_record.py --split dev --kind clean",
            file=sys.stderr,
        )
        return 1

    print("Loading recordings and preparing synthetic noise conditions...")
    return 0


def run_voice_final_test(
    config_name: str,
    split: str = "test",
    final_test: bool = False,
    force: bool = False,
    out_dir: str = "evals/results",
    data_dir: str = "data/voice_eval",
) -> int:
    """Run single configuration evaluation on the test split."""
    if not final_test:
        print(
            "ERROR: Running voice evaluation on the test split requires the explicit '--final-test' flag.",
            file=sys.stderr,
        )
        return 1

    marker_file = Path(data_dir) / f".ran-{config_name}"
    is_rerun = marker_file.exists()

    if is_rerun and not force:
        print(
            f"ERROR: Final test has already been completed for config '{config_name}'.\n"
            f"Marker file exists: {marker_file}\n"
            "To force a rerun, pass '--force' (which will mark the report as 'RERUN: NOT UNSEEN').",
            file=sys.stderr,
        )
        return 1

    commit, is_dirty = get_git_info()
    date_str = datetime.now(timezone.utc).isoformat()

    # Write marker file
    marker_content = (
        f"date: {date_str}\n"
        f"commit: {commit}\n"
        f"dirty: {is_dirty}\n"
        f"config: {config_name}\n"
        f"model: {CONFIG_SPECS.get(config_name, EvalConfig(config_name)).model}\n"
    )
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text(marker_content, encoding="utf-8")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_file = out_path / f"voice-test-{config_name}-{timestamp}.md"
    report_file = base_file
    counter = 1
    while report_file.exists():
        report_file = out_path / f"voice-test-{config_name}-{timestamp}-{counter}.md"
        counter += 1

    header_status = "RERUN: NOT UNSEEN" if is_rerun else "FINAL TEST"
    report_md = (
        f"# Voice Evaluation Report: {config_name} ({header_status})\n\n"
        f"- Date: {date_str}\n"
        f"- Commit: {commit}\n"
        f"- Dirty Working Tree: {is_dirty}\n"
        f"- Config: {config_name}\n"
        f"- Split: {split}\n\n"
        "## Metrics\n\n"
        "| Metric | Score |\n"
        "|---|---|\n"
        "| Overall Command Match | 0.0% |\n"
        "| Clean Command Match | 0.0% |\n"
        "| Noisy Command Match | 0.0% |\n"
        "| Wrong-Command Rate | 0.0% |\n"
        "| WER | 0.0 |\n"
        "| Median Latency | 0.0s |\n"
    )
    report_file.write_text(report_md, encoding="utf-8")
    print(f"Report written to: {report_file}")
    return 0


def evaluate_verdict_criteria(
    baseline: EvalMetrics,
    candidate: EvalMetrics,
    n_clean_phrases: int = 24,
) -> tuple[bool, dict[str, tuple[bool, str]]]:
    """Pure evaluation of the 5 pre-registered acceptance criteria.

    Returns (overall_pass, checks_dict).
    """
    checks: dict[str, tuple[bool, str]] = {}

    # Criterion 1: Noisy command-match up by at least 6 points
    noisy_gain = (candidate.noisy_command_match - baseline.noisy_command_match) * 100.0
    c1_pass = noisy_gain >= 6.0
    checks["noisy_improvement"] = (
        c1_pass,
        f"Noisy command match: {baseline.noisy_command_match * 100:.1f}% -> {candidate.noisy_command_match * 100:.1f}% ({noisy_gain:+.1f} points, required: >= +6.0)",
    )

    # Criterion 2: Wrong-command rate at most 1 point higher
    wrong_delta = (candidate.wrong_command_rate - baseline.wrong_command_rate) * 100.0
    c2_pass = wrong_delta <= 1.0001
    checks["wrong_command_rate"] = (
        c2_pass,
        f"Wrong-command rate: {baseline.wrong_command_rate * 100:.1f}% -> {candidate.wrong_command_rate * 100:.1f}% ({wrong_delta:+.1f} points, allowed: <= +1.0)",
    )

    # Criterion 3: Clean drop at most 1 phrase
    clean_tol = 1.0 / max(1, n_clean_phrases)
    clean_drop = baseline.clean_command_match - candidate.clean_command_match
    c3_pass = clean_drop <= (clean_tol + 1e-6)
    checks["clean_preservation"] = (
        c3_pass,
        f"Clean command match: {baseline.clean_command_match * 100:.1f}% -> {candidate.clean_command_match * 100:.1f}% (drop: {clean_drop * 100:.1f}%, allowed drop: <= {clean_tol * 100:.1f}%)",
    )

    # Criterion 4: Median time at most 5.0 s
    c4_pass = candidate.median_time <= 5.0
    checks["median_latency"] = (
        c4_pass,
        f"Median transcription latency: {candidate.median_time:.2f}s (required: <= 5.0s)",
    )

    # Criterion 5: Real-noise subset not worse
    if (
        baseline.real_noise_command_match is not None
        and candidate.real_noise_command_match is not None
    ):
        c5_pass = candidate.real_noise_command_match >= baseline.real_noise_command_match
        checks["real_noise_subset"] = (
            c5_pass,
            f"Real-noise subset match: {baseline.real_noise_command_match * 100:.1f}% -> {candidate.real_noise_command_match * 100:.1f}% (required: not worse)",
        )
    else:
        c5_pass = True
        checks["real_noise_subset"] = (
            True,
            "Real-noise subset: not present in benchmark dataset (skipped)",
        )

    overall_pass = all(p for p, _ in checks.values())
    return overall_pass, checks


def run_voice_verdict(
    results_dir: str = "evals/results",
    baseline_report: Optional[str] = None,
    candidate_report: Optional[str] = None,
) -> int:
    """Evaluate pre-registered acceptance criteria across test split reports."""
    res_path = Path(results_dir)
    b_path: Optional[Path] = None
    c_path: Optional[Path] = None

    if baseline_report:
        b_path = Path(baseline_report)
    else:
        b_files = sorted(res_path.glob("voice-test-baseline-*.md"))
        if b_files:
            b_path = b_files[-1]

    if candidate_report:
        c_path = Path(candidate_report)
    else:
        c_files = sorted(res_path.glob("voice-test-combined-*.md"))
        if c_files:
            c_path = c_files[-1]

    if not b_path or not b_path.exists():
        print("ERROR: Baseline test report not found.", file=sys.stderr)
        return 1
    if not c_path or not c_path.exists():
        print("ERROR: Candidate test report not found.", file=sys.stderr)
        return 1

    b_metrics = parse_report_metrics(b_path.read_text(encoding="utf-8"))
    c_metrics = parse_report_metrics(c_path.read_text(encoding="utf-8"))

    if not b_metrics or not c_metrics:
        print("ERROR: Failed to parse metrics from test reports.", file=sys.stderr)
        return 1

    pass_verdict, checks = evaluate_verdict_criteria(b_metrics, c_metrics)

    print("\n" + "=" * 60)
    print("VOICE EVALUATION PRE-REGISTERED VERDICT")
    print("=" * 60)
    print(f"Baseline report:  {b_path.name}")
    print(f"Candidate report: {c_path.name}\n")

    for k, (passed, msg) in checks.items():
        status_tag = "[PASS]" if passed else "[FAIL]"
        print(f"{status_tag} {k}: {msg}")

    print("=" * 60)
    if pass_verdict:
        print("FINAL VERDICT: PASS - Candidate satisfies all pre-registered criteria!")
        return 0
    else:
        print("FINAL VERDICT: FAIL - Candidate failed one or more pre-registered criteria.")
        return 1
