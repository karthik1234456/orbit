"""Subprocess worker for Piper neural text-to-speech synthesis and streaming playback.

Runs as a standalone process:
    python -m voice.piper_worker --voice <path> --speed <float> --volume <float>

Reads sentences line-by-line from stdin.
A producer thread synthesizes sentences to temporary WAV files (at most 2 sentences ahead),
while the main thread sequentially plays them using winsound.PlaySound and removes them.
Volume is applied by scaling int16 samples with the standard library array module (no audioop).
Piper is imported lazily inside the worker.
"""

from __future__ import annotations

import argparse
import array
import atexit
import os
from pathlib import Path
import queue
import signal
import sys
import tempfile
import threading
from typing import Any, Callable, Optional, Union
import wave

_SENTINEL = object()


def apply_volume_to_wav(wav_path: Union[str, Path], volume: float) -> None:
    """Scale 16-bit PCM WAV samples by volume using array module without clipping or overflow.

    Does NOT use audioop (removed in Python 3.13).
    """
    if abs(volume - 1.0) < 1e-4:
        return

    str_path = str(wav_path)
    try:
        with wave.open(str_path, "rb") as wf:
            params = wf.getparams()
            sampwidth = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

        if sampwidth != 2:
            return

        samples = array.array("h")
        samples.frombytes(frames)

        for i in range(len(samples)):
            val = int(samples[i] * volume)
            if val > 32767:
                val = 32767
            elif val < -32768:
                val = -32768
            samples[i] = val

        with wave.open(str_path, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(samples.tobytes())
    except Exception as exc:
        sys.stderr.write(f"Warning: volume scaling failed: {exc}\n")


def synthesize_sentence_to_wav(
    voice: Any,
    sentence: str,
    wav_path: Union[str, Path],
    length_scale: float = 1.0,
) -> None:
    """Synthesize a single sentence to a 16-bit PCM WAV file using Piper.

    Wraps differences between Piper API versions behind a single function.
    """
    str_path = str(wav_path)
    sample_rate = 22050
    try:
        sr = getattr(getattr(voice, "config", None), "sample_rate", None)
        if isinstance(sr, int) and sr > 0:
            sample_rate = sr
    except Exception:
        pass

    if hasattr(voice, "synthesize_wav"):
        with wave.open(str_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            voice.synthesize_wav(sentence, wf, length_scale=length_scale)
    elif hasattr(voice, "synthesize"):
        try:
            chunks = voice.synthesize(sentence, length_scale=length_scale)
            if hasattr(chunks, "__iter__"):
                with wave.open(str_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    for chunk in chunks:
                        raw = getattr(chunk, "audio_int16_bytes", None)
                        if raw is None:
                            raw = getattr(chunk, "audio", chunk)
                        if isinstance(raw, bytes):
                            wf.writeframes(raw)
            else:
                with wave.open(str_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    voice.synthesize(sentence, wf, length_scale=length_scale)
        except TypeError:
            with wave.open(str_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                voice.synthesize(sentence, wf, length_scale=length_scale)
    else:
        raise RuntimeError("Piper voice object has no synthesize or synthesize_wav method")


def default_play_sound(wav_path: str) -> None:
    """Play a WAV file synchronously using winsound on Windows."""
    try:
        import winsound

        winsound.PlaySound(wav_path, winsound.SND_FILENAME)
    except ImportError:
        # Fallback for headless or non-Windows test environments
        pass
    except Exception as exc:
        sys.stderr.write(f"Playback error: {exc}\n")


def run_worker_loop(
    voice_obj: Any,
    speed: float = 1.0,
    volume: float = 1.0,
    stdin_lines: Optional[Any] = None,
    synthesizer_fn: Optional[Callable[..., None]] = None,
    player_fn: Optional[Callable[[str], None]] = None,
    temp_dir_override: Optional[str] = None,
) -> int:
    """Execute the producer-consumer synthesis and playback loop."""
    synth_fn = synthesizer_fn or synthesize_sentence_to_wav
    play_fn = player_fn or default_play_sound
    lines_source = stdin_lines if stdin_lines is not None else sys.stdin

    # Temporary directory for synthesized WAV chunks
    temp_dir = temp_dir_override or tempfile.mkdtemp(prefix="orbit_piper_")

    def _cleanup() -> None:
        try:
            if os.path.exists(temp_dir):
                import shutil

                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    atexit.register(_cleanup)

    # Piper length_scale is inversely proportional to speed: higher speed = shorter duration
    length_scale = 1.0 / max(0.1, speed)

    # Queue bounded to maxsize=2 strictly ensures synthesis stays at most 2 sentences ahead
    playback_queue: queue.Queue = queue.Queue(maxsize=2)
    producer_error: list[Exception] = []
    stop_event = threading.Event()

    def producer_target() -> None:
        idx = 0
        try:
            for raw_line in lines_source:
                if stop_event.is_set():
                    break
                line = raw_line.strip() if isinstance(raw_line, str) else ""
                if not line:
                    continue

                idx += 1
                wav_file = os.path.join(temp_dir, f"chunk_{idx}.wav")
                synth_fn(voice_obj, line, wav_file, length_scale=length_scale)
                apply_volume_to_wav(wav_file, volume)

                # Blocks if playback queue already holds 2 unplayed sentences
                while not stop_event.is_set():
                    try:
                        playback_queue.put(wav_file, timeout=0.1)
                        break
                    except queue.Full:
                        continue

            playback_queue.put(_SENTINEL)
        except Exception as exc:
            producer_error.append(exc)
            playback_queue.put(_SENTINEL)

    prod_thread = threading.Thread(target=producer_target, name="PiperProducer", daemon=True)
    prod_thread.start()

    # Main thread: consume and play WAV files in order
    try:
        while True:
            item = playback_queue.get()
            if item is _SENTINEL:
                playback_queue.task_done()
                break

            wav_path = str(item)
            try:
                play_fn(wav_path)
            finally:
                # Remove temporary WAV immediately after playing
                try:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except OSError:
                    pass
                playback_queue.task_done()

        prod_thread.join(timeout=2.0)

        if producer_error:
            raise producer_error[0]

        return 0
    except Exception as exc:
        sys.stderr.write(f"Piper worker error: {exc}\n")
        return 1
    finally:
        stop_event.set()
        _cleanup()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ORBIT Piper Neural TTS Subprocess Worker")
    parser.add_argument("--voice", required=True, help="Absolute path to Piper ONNX voice file")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed multiplier (0.8 - 1.3)")
    parser.add_argument("--volume", type=float, default=1.0, help="Speech volume level (0.3 - 1.0)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    voice_path = Path(args.voice).resolve()
    if not voice_path.is_file():
        sys.stderr.write(f"Voice file not found: {voice_path}\n")
        return 1

    config_path = Path(str(voice_path) + ".json")
    if not config_path.is_file():
        config_path = voice_path.with_suffix(".onnx.json")
    if not config_path.is_file():
        sys.stderr.write(f"Voice configuration JSON not found for: {voice_path}\n")
        return 1

    # Lazy import of Piper INSIDE the worker main
    try:
        from piper.voice import PiperVoice  # type: ignore[import-not-found]
    except ImportError as exc:
        sys.stderr.write(f"Piper is not installed: {exc}\n")
        return 1

    try:
        voice = PiperVoice.load(str(voice_path), config_path=str(config_path))
    except Exception as exc:
        sys.stderr.write(f"Failed to load Piper voice: {exc}\n")
        return 1

    # Register OS signal handlers for graceful stop
    def _sig_handler(signum: int, frame: Any) -> None:
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)
    except Exception:
        pass

    return run_worker_loop(voice, speed=args.speed, volume=args.volume)


if __name__ == "__main__":
    sys.exit(main())
