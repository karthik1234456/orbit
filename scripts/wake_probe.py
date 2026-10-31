"""Live evaluation probe for hands-free wake word detection in real acoustic environments.

Evaluates wake word detection sensitivity and ambient false-wake rejection:
- Stage (a) Detection: Prompts user to speak the wake phrase 10 times and measures
  detections, misses, and median detection latency.
- Stage (b) Ambient: Listens for 120 seconds during background audio (TV/music)
  and reports false wake triggers, segment count, CPU load, and peak STT decode latency.
- Stage (c) Summary: Reports configuration settings, measured metrics, and guidance.

PRIVACY & SAFETY:
- Stores NO audio and NO text.
- Prints counts, percentages, and latencies only.
- Never run by automated tests (pytest).
"""

from __future__ import annotations

from pathlib import Path
import statistics
import sys
import time
from typing import Any

# Path bootstrap: ensure project root is in sys.path
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import settings
from voice.audio import list_input_devices
from voice.stt import WhisperTranscriber
from voice.vad import UtteranceSegmenter, capture_utterance
from voice.wake import match_wake_phrase

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]


def run_stage_a(
    transcriber: Any,
    wake_phrase: str,
    sensitivity: float,
    sample_rate: int = 16000,
) -> tuple[int, int, float]:
    """Stage (a): Prompt user to speak wake phrase 10 times and record detection latency."""
    print("\n" + "=" * 60)
    print("STAGE (a): Wake Phrase Detection (10 Trials)")
    print(f"Target Phrase: '{wake_phrase}' | Sensitivity: {sensitivity:.2f}")
    print("=" * 60)
    print("When prompted, speak the wake phrase clearly into your microphone.\n")

    detections = 0
    misses = 0
    latencies: list[float] = []

    for trial in range(1, 11):
        print(f"Trial {trial}/10: Get ready...")
        for count in range(3, 0, -1):
            print(f"  {count}...", end="\r", flush=True)
            time.sleep(0.7)
        print(f"  >>> SAY '{wake_phrase.upper()}' NOW! <<<        ")

        segmenter = UtteranceSegmenter(
            threshold=getattr(settings, "VAD_THRESHOLD", 0.01),
            silence_seconds=getattr(settings, "SILENCE_SECONDS", 0.8),
            sample_rate=sample_rate,
        )

        t_start = time.perf_counter()
        audio = capture_utterance(
            stream_factory=None,
            segmenter=segmenter,
            start_timeout=4.0,
        )

        if audio is None or len(audio) == 0:
            print("  [MISS] No speech detected within 4.0 seconds.")
            misses += 1
            time.sleep(1.0)
            continue

        dur_s = len(audio) / float(sample_rate)
        if dur_s < 0.3 or dur_s > 4.0:
            print(f"  [MISS] Segment duration ({dur_s:.2f}s) outside 0.3s-4.0s window.")
            misses += 1
            time.sleep(1.0)
            continue

        crop = audio[: int(3.0 * sample_rate)] if len(audio) > int(3.0 * sample_rate) else audio
        t_trans_start = time.perf_counter()
        t_res = transcriber.transcribe(crop, sample_rate=sample_rate)
        t_trans_end = time.perf_counter()
        t_detect = (t_trans_end - t_start)

        raw_text = t_res.text if hasattr(t_res, "text") else str(t_res)
        matched = match_wake_phrase(raw_text, phrase=wake_phrase, threshold=sensitivity)

        # Discard transcript immediately
        del raw_text, t_res, crop, audio

        if matched is not None:
            print(f"  [DETECTED] Latency: {t_detect * 1000.0:.0f} ms (Decode: {(t_trans_end - t_trans_start)*1000.0:.0f} ms)")
            detections += 1
            latencies.append(t_detect)
        else:
            print("  [MISS] Wake phrase did not match.")
            misses += 1

        time.sleep(1.0)

    median_lat = statistics.median(latencies) if latencies else 0.0
    return detections, misses, median_lat


def run_stage_b(
    transcriber: Any,
    wake_phrase: str,
    sensitivity: float,
    duration_s: float = 120.0,
    sample_rate: int = 16000,
) -> tuple[int, int, float, float]:
    """Stage (b): Listen for 120 seconds during background ambient noise (TV/music)."""
    print("\n" + "=" * 60)
    print("STAGE (b): Ambient False-Wake Evaluation (120 Seconds)")
    print("=" * 60)
    print("Play your normal room background audio (music, podcast, or TV).")
    print("Do NOT speak the wake phrase. Evaluating false wakes...\n")

    false_wakes = 0
    segments_checked = 0
    cpu_samples: list[float] = []
    longest_decode_s: float = 0.0

    proc = psutil.Process() if psutil is not None else None
    if proc is not None:
        try:
            proc.cpu_percent()
        except Exception:
            pass

    t_end = time.monotonic() + duration_s
    t_last_cpu = time.monotonic()

    while time.monotonic() < t_end:
        rem_s = int(t_end - time.monotonic())
        print(f"Listening... {rem_s}s remaining | False wakes: {false_wakes} | Segments: {segments_checked} ", end="\r", flush=True)

        if proc is not None and (time.monotonic() - t_last_cpu) >= 1.0:
            t_last_cpu = time.monotonic()
            try:
                cpu_samples.append(proc.cpu_percent())
            except Exception:
                pass

        segmenter = UtteranceSegmenter(
            threshold=getattr(settings, "VAD_THRESHOLD", 0.01),
            silence_seconds=getattr(settings, "SILENCE_SECONDS", 0.8),
            sample_rate=sample_rate,
        )

        audio = capture_utterance(
            stream_factory=None,
            segmenter=segmenter,
            start_timeout=1.5,
        )

        if audio is None or len(audio) == 0:
            continue

        dur_s = len(audio) / float(sample_rate)
        if dur_s < 0.3 or dur_s > 4.0:
            continue

        segments_checked += 1
        crop = audio[: int(3.0 * sample_rate)] if len(audio) > int(3.0 * sample_rate) else audio

        t_dec_start = time.perf_counter()
        t_res = transcriber.transcribe(crop, sample_rate=sample_rate)
        t_dec = time.perf_counter() - t_dec_start
        if t_dec > longest_decode_s:
            longest_decode_s = t_dec

        raw_text = t_res.text if hasattr(t_res, "text") else str(t_res)
        matched = match_wake_phrase(raw_text, phrase=wake_phrase, threshold=sensitivity)

        # Discard transcript immediately
        del raw_text, t_res, crop, audio

        if matched is not None:
            false_wakes += 1
            print(f"\n  [ALERT] False wake trigger recorded! (Decode: {t_dec * 1000.0:.0f} ms)")

    avg_cpu = (sum(cpu_samples) / len(cpu_samples)) if cpu_samples else 0.0
    print("\nStage (b) completed.")
    return false_wakes, segments_checked, avg_cpu, longest_decode_s


def main() -> int:
    """Execute live wake word detection probe."""
    print("=" * 60)
    print("ORBIT Hands-Free Wake Word Probe")
    print("=" * 60)

    devs = list_input_devices()
    if not devs:
        print("ERROR: No microphone or audio input device detected.")
        return 1

    wake_phrase = getattr(settings, "WAKE_PHRASE", "hey orbit")
    sensitivity = getattr(settings, "WAKE_SENSITIVITY", 0.80)
    wake_model = getattr(settings, "WAKE_STT_MODEL", "tiny.en")
    stt_lang = getattr(settings, "STT_LANGUAGE", "en")

    print(f"Loading wake model '{wake_model}'...")
    try:
        transcriber = WhisperTranscriber(model_name=wake_model, language=stt_lang)
    except Exception as exc:
        print(f"ERROR: Failed to initialize WhisperTranscriber ({exc})")
        return 1

    # Stage (a)
    detections, misses, median_lat = run_stage_a(transcriber, wake_phrase, sensitivity)

    # Stage (b)
    false_wakes, segments, avg_cpu, longest_decode = run_stage_b(transcriber, wake_phrase, sensitivity, duration_s=120.0)

    # Stage (c) Summary
    print("\n" + "=" * 60)
    print("PROBE RESULTS SUMMARY")
    print("=" * 60)
    print(f"Wake Phrase:                  '{wake_phrase}'")
    print(f"Wake Sensitivity:              {sensitivity:.2f}")
    print(f"Wake STT Model:                {wake_model}")
    print("-" * 60)
    print(f"Detection Success Rate:        {detections}/10 ({(detections / 10.0) * 100.0:.1f}%)")
    print(f"Missed Detections:             {misses}/10")
    print(f"Median Detection Latency:      {median_lat * 1000.0:.0f} ms")
    print(f"Ambient False Wakes:           {false_wakes} (in 120s ambient audio)")
    print(f"Speech Segments Checked:       {segments}")
    print(f"Process Average CPU:           {avg_cpu:.1f}%")
    print(f"Longest STT Decode Time:       {longest_decode * 1000.0:.0f} ms")
    print("-" * 60)
    print("Guidance Targets (from docs/HANDS_FREE.md):")
    print("  - Detection: >= 9 of 10 trials")
    print("  - False Wakes: <= 2 in 10 minutes (<= 1 in 2 minutes)")
    print("The user decides whether to enable hands-free after measuring.")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
