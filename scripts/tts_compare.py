"""ORBIT Text-to-Speech Engine Comparison Benchmark.

Speaks 5 fixed sentences with each available engine (SAPI, Piper),
measures time to first audio and real-time factor (RTF),
and prints exact .env configuration lines. Speaks nothing else.
"""

import importlib.util
import sys
import time
import wave
from pathlib import Path

# Ensure project root is in sys.path when executed directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.paths import voices_dir
from voice.voices import list_voices
from voice.tts import SubprocessSpeaker

TEST_SENTENCES = [
    "Welcome to the ORBIT speech comparison benchmark.",
    "The local desktop assistant runs private models with zero telemetry.",
    "Fast response times ensure natural conversational interaction.",
    "Neural speech offers natural cadence while Windows SAPI provides reliable fallback.",
    "Voice check complete.",
]


def _measure_sapi(sentences: list[str]) -> dict[str, float]:
    speaker = SubprocessSpeaker(rate=180)
    t0 = time.perf_counter()
    # SAPI speaks synchronously per call if blocking=True
    t_first = 0.0
    for idx, sentence in enumerate(sentences):
        s_start = time.perf_counter()
        speaker.speak(sentence, blocking=True)
        s_end = time.perf_counter()
        if idx == 0:
            t_first = s_end - s_start

    total_time = time.perf_counter() - t0
    # Approximate audio duration (at ~150 words per minute, 55 words ~= 22s)
    word_count = sum(len(s.split()) for s in sentences)
    approx_audio_duration = max(word_count / 2.5, 1.0)
    rtf = total_time / approx_audio_duration
    return {
        "time_to_first_audio_s": t_first,
        "total_time_s": total_time,
        "rtf": rtf,
    }


def _measure_piper(sentences: list[str], voice_path: Path) -> dict[str, float]:
    from voice.piper_worker import synthesize_sentence_to_wav
    from piper import PiperVoice

    voice = PiperVoice.load(str(voice_path), config_path=str(voice_path.with_suffix(".onnx.json")))

    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="orbit_bench_")

    total_synth_time = 0.0
    total_audio_duration = 0.0
    t_first = 0.0

    try:
        import winsound
        for idx, sentence in enumerate(sentences):
            wav_path = Path(tmp_dir) / f"bench_{idx}.wav"
            s0 = time.perf_counter()
            synthesize_sentence_to_wav(voice, sentence, wav_path, length_scale=1.0)
            s_dur = time.perf_counter() - s0
            total_synth_time += s_dur

            # Read audio duration from WAV header
            with wave.open(str(wav_path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                dur = frames / float(rate) if rate else 0.0
                total_audio_duration += dur

            if idx == 0:
                t_first = s_dur

            # Play audio
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)

            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    rtf = total_synth_time / max(total_audio_duration, 0.001)
    return {
        "time_to_first_audio_s": t_first,
        "total_time_s": total_synth_time,
        "audio_duration_s": total_audio_duration,
        "rtf": rtf,
    }


def main() -> None:
    print("=" * 60)
    print("ORBIT Text-to-Speech Engine Comparison Benchmark")
    print("=" * 60)

    # 1. SAPI
    print("\n--- Testing Windows SAPI Engine ---")
    sapi_res = _measure_sapi(TEST_SENTENCES)
    print(f"  Time to first audio: {sapi_res['time_to_first_audio_s']:.3f} s")
    print(f"  Total time:          {sapi_res['total_time_s']:.3f} s")
    print(f"  Estimated RTF:       {sapi_res['rtf']:.3f}")

    # 2. Piper
    has_piper = importlib.util.find_spec("piper") is not None
    v_dir = voices_dir()
    voices = list_voices(v_dir) if v_dir.exists() else []

    piper_tested = False
    chosen_voice = None
    if has_piper and voices:
        chosen_voice = voices[0]
        voice_file = v_dir / chosen_voice
        print(f"\n--- Testing Piper Engine ({chosen_voice}) ---")
        try:
            piper_res = _measure_piper(TEST_SENTENCES, voice_file)
            print(f"  Time to first audio: {piper_res['time_to_first_audio_s']:.3f} s")
            print(f"  Synthesis time:      {piper_res['total_time_s']:.3f} s")
            print(f"  Audio duration:      {piper_res.get('audio_duration_s', 0.0):.3f} s")
            print(f"  RTF:                 {piper_res['rtf']:.3f}")
            piper_tested = True
        except Exception as exc:
            print(f"  Piper test failed: {exc}")
    else:
        print("\n--- Piper Engine ---")
        if not has_piper:
            print("  piper-tts package is not installed.")
        if not voices:
            print(f"  No valid voice models found in {v_dir}.")

    # Summary and configuration recommendations
    print("\n" + "=" * 60)
    print("Recommended .env Settings:")
    print("=" * 60)
    if piper_tested and chosen_voice:
        print(f"TTS_ENGINE=piper\nPIPER_VOICE={chosen_voice}\nTTS_SPEED=1.0\nTTS_VOLUME=1.0")
    else:
        print("TTS_ENGINE=sapi\nTTS_SPEED=1.0\nTTS_VOLUME=1.0")
    print("=" * 60)


if __name__ == "__main__":
    main()
