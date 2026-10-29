"""CLI tool to record voice benchmark phrases for the measurement kit.

Saves 16 kHz mono 16-bit PCM WAV recordings under data/voice_eval/<split>/<kind>/<id>.wav.
Resumable: allows redoing takes or picking up where left off.
Refuses to run in demo mode.
Plays NO audio.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
from typing import Optional
import wave
import numpy as np

# Standard path bootstrap
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.paths import is_demo_mode
from voice.audio import Recorder
from voice.enhance import estimate_snr_db


def save_wav(file_path: Path, audio: np.ndarray, sample_rate: int = 16000) -> None:
    """Save 1-D float32 audio as 16 kHz mono 16-bit PCM WAV using stdlib wave."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    int16_audio = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16_audio.tobytes())


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record speech phrases for ORBIT voice eval.")
    parser.add_argument(
        "--split",
        choices=["dev", "test"],
        default="dev",
        help="Evaluation split to record ('dev' or 'test').",
    )
    parser.add_argument(
        "--kind",
        choices=["clean", "real_noise"],
        default="clean",
        help="Recording acoustic environment ('clean' or 'real_noise').",
    )
    return parser.parse_args(argv)


def record_phrase(
    recorder: Recorder,
    phrase_id: str,
    phrase_text: str,
    target_path: Path,
    is_clean: bool,
) -> bool:
    """Record a single phrase with user confirmation and noise quality check."""
    print(f"\n[{phrase_id}] Phrase: \"{phrase_text}\"")
    if target_path.exists():
        print(f"Existing recording found: {target_path}")
        ans = input("Redo this take? (y/N): ").strip().lower()
        if ans not in ("y", "yes"):
            print("Skipped.")
            return True

    while True:
        input("Press Enter to start recording...")
        stop_event = threading.Event()
        recorded: list[np.ndarray] = []

        def worker():
            try:
                audio = recorder.record(stop_event=stop_event)
                recorded.append(audio)
            except Exception as e:
                print(f"Recording error: {e}")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        print("Recording... Press Enter to stop.")
        try:
            input()
        finally:
            stop_event.set()
            t.join()

        if not recorded or len(recorded[0]) == 0:
            print("No audio captured. Try again.")
            continue

        audio = recorded[0]
        duration_s = float(len(audio)) / float(recorder.sample_rate)
        if duration_s < 0.3:
            print("Recording too short (< 0.3s). Try again.")
            continue

        snr = estimate_snr_db(audio, sr=recorder.sample_rate)
        print(f"Recorded {duration_s:.1f}s | Estimated SNR: {snr:.1f} dB")

        if is_clean and snr < 15.0:
            print("WARNING: Take detected significant background noise (SNR < 15 dB).")
            print("Recommendation: Use a headset or record in a quieter space.")

        choice = input("Accept take? (Y/n/redo): ").strip().lower()
        if choice in ("n", "no", "redo", "r"):
            print("Discarded. Redoing take...")
            continue

        save_wav(target_path, audio, sample_rate=recorder.sample_rate)
        print(f"Saved take to: {target_path}")
        return True


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if is_demo_mode():
        print("ERROR: voice_record.py refuses to run in demo mode.", file=sys.stderr)
        return 1

    phrases_file = Path("evals/voice_phrases.json")
    if not phrases_file.exists():
        print(f"ERROR: Phrases file not found: {phrases_file}", file=sys.stderr)
        return 1

    data = json.loads(phrases_file.read_text(encoding="utf-8"))
    items = data.get(args.split, [])

    if args.kind == "real_noise":
        items = items[:12]
        print("\n" + "=" * 60)
        print("REAL NOISE RECORDING MODE (12 PHRASES)")
        print("Please play music or TV from your speakers at normal room volume.")
        print("=" * 60)

    print(f"\nStarting recording session: split={args.split}, kind={args.kind}, count={len(items)}")
    recorder = Recorder(sample_rate=16000)

    out_base = Path("data/voice_eval") / args.split / args.kind
    out_base.mkdir(parents=True, exist_ok=True)

    for item in items:
        pid = item["id"]
        phrase = item["phrase"]
        target = out_base / f"{pid}.wav"
        record_phrase(recorder, pid, phrase, target, is_clean=(args.kind == "clean"))

    print("\nRecording session complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())

