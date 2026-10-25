import sys
from pathlib import Path

# Ensure project root is in sys.path when executed directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import MIC_DEVICE, STT_LANGUAGE, STT_MODEL, TTS_RATE
from voice.audio import Recorder, audio_stats, list_input_devices
from voice.stt import WhisperTranscriber
from voice.tts import SubprocessSpeaker


def main() -> None:
    print("=" * 60)
    print("ORBIT Voice Hardware Check")
    print("=" * 60)

    # Stage 1: List Input Devices
    print("\n--- Stage 1: Audio Input Devices ---")
    devices = list_input_devices()
    if devices:
        for idx, name in devices:
            print(f"  [{idx}] {name}")
        print("[STAGE 1: INPUT DEVICES] PASS")
    else:
        print("  No audio input devices detected.")
        print("[STAGE 1: INPUT DEVICES] FAIL")

    # Stage 2: Record 4 Seconds
    print("\n--- Stage 2: Microphone Recording (4 seconds) ---")
    audio = None
    try:
        print(f"Recording 4 seconds from device: {MIC_DEVICE}...")
        recorder = Recorder(device=MIC_DEVICE, max_seconds=4)
        audio = recorder.record()
        duration, rms = audio_stats(audio)
        print(f"  Recorded Duration: {duration:.2f}s")
        print(f"  Loudness (RMS): {rms:.5f}")
        if duration >= 3.0:
            print("[STAGE 2: RECORDING] PASS")
        else:
            print(f"[STAGE 2: RECORDING] FAIL (Recorded {duration:.2f}s < 3.0s)")
    except Exception as exc:
        print(f"  Recording failed: {exc}")
        print("[STAGE 2: RECORDING] FAIL")

    # Stage 3: Transcription
    print("\n--- Stage 3: Whisper Speech-to-Text ---")
    try:
        if audio is None or len(audio) == 0:
            print("  Cannot transcribe: no audio recorded.")
            print("[STAGE 3: TRANSCRIPTION] FAIL")
        else:
            print(f"Transcribing with model '{STT_MODEL}' (lang='{STT_LANGUAGE}')...")
            transcriber = WhisperTranscriber(model_name=STT_MODEL, language=STT_LANGUAGE)
            transcript = transcriber.transcribe(audio)
            print(f"  Transcript: \"{transcript.text}\" (language={transcript.language})")
            print("[STAGE 3: TRANSCRIPTION] PASS")
    except Exception as exc:
        print(f"  Transcription failed: {exc}")
        print("[STAGE 3: TRANSCRIPTION] FAIL")

    # Stage 4: Text-to-Speech
    print("\n--- Stage 4: Text-to-Speech Output ---")
    try:
        test_message = "Hello, I am ORBIT. If you can hear this, text to speech works."
        print(f"Speaking (rate={TTS_RATE}): \"{test_message}\"")
        speaker = SubprocessSpeaker(rate=TTS_RATE)
        speaker.speak(test_message, blocking=True)
        print("[STAGE 4: TEXT TO SPEECH] PASS")
    except Exception as exc:
        print(f"  Text to speech failed: {exc}")
        print("[STAGE 4: TEXT TO SPEECH] FAIL")

    print("\n" + "=" * 60)
    print("Voice hardware check complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
