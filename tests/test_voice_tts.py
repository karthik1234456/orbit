"""Tests for voice/tts.py: NullSpeaker, SubprocessSpeaker process isolation, timeouts, and process management."""

from pathlib import Path
import subprocess
import sys
import pytest

from voice.tts import NullSpeaker, SpeechError, SubprocessSpeaker


def test_null_speaker():
    """NullSpeaker performs no-op operations safely."""
    speaker = NullSpeaker()
    speaker.speak("test text", blocking=True)
    speaker.speak("test text", blocking=False)
    speaker.stop()
    assert speaker.is_speaking() is False


def test_subprocess_speaker_receives_cleaned_text(tmp_path: Path):
    """SubprocessSpeaker sanitizes text using clean_for_speech and passes it to worker stdin."""
    out_file = tmp_path / "tts_received.txt"
    # Python script writing stdin directly into out_file
    fake_worker = [
        sys.executable,
        "-c",
        f"import sys; open(r'{out_file}', 'w', encoding='utf-8').write(sys.stdin.read())",
    ]

    speaker = SubprocessSpeaker(command=fake_worker)
    # Pass text with markdown and code blocks that require cleaning
    dirty_text = "Here is ```python\nprint(1)\n``` and https://example.com/page"
    speaker.speak(dirty_text, blocking=True)

    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "print(1)" not in content
    assert "https://example.com" not in content
    assert content == "Here is code omitted and link"


def test_popen_args_is_list_without_shell_true(monkeypatch: pytest.MonkeyPatch):
    """SubprocessSpeaker invokes Popen with an explicit list and without shell=True."""
    captured_kwargs: dict = {}
    captured_args: list = []

    real_popen = subprocess.Popen

    def mock_popen(args, **kwargs):
        captured_args.append(args)
        captured_kwargs.update(kwargs)
        return real_popen([sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(0)"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    speaker = SubprocessSpeaker(command=[sys.executable, "-c", "pass"])
    speaker.speak("Hello world", blocking=True)

    assert len(captured_args) == 1
    assert isinstance(captured_args[0], list)
    assert captured_kwargs.get("shell") is not True


def test_speak_on_empty_text_starts_no_process():
    """speak() skips launching a process if text contains nothing speakable."""
    speaker = SubprocessSpeaker()
    speaker.speak("   \t\n  ", blocking=True)
    assert speaker._process is None
    assert speaker.is_speaking() is False

    speaker.speak("* # | -", blocking=True)
    assert speaker._process is None


def test_stop_terminates_long_running_process():
    """stop() cleanly terminates a running subprocess."""
    long_script = [sys.executable, "-c", "import time; time.sleep(15)"]
    speaker = SubprocessSpeaker(command=long_script)

    speaker.speak("Keep talking", blocking=False)
    assert speaker.is_speaking() is True

    speaker.stop()
    assert speaker.is_speaking() is False
    assert speaker._process is None


def test_second_speak_stops_first_process():
    """A subsequent speak() call stops and terminates any currently executing process."""
    long_script = [sys.executable, "-c", "import time; time.sleep(15)"]
    speaker = SubprocessSpeaker(command=long_script)

    speaker.speak("First speech", blocking=False)
    proc1 = speaker._process
    assert speaker.is_speaking() is True

    speaker.speak("Second speech", blocking=False)
    proc2 = speaker._process

    assert proc1 is not None and proc2 is not None
    assert proc1 != proc2
    # Verify first process was terminated
    assert proc1.poll() is not None
    assert speaker.is_speaking() is True

    speaker.stop()
    assert speaker.is_speaking() is False


def test_blocking_timeout_kills_process():
    """A blocking call exceeding the timeout kills the process and raises SpeechError."""
    long_script = [sys.executable, "-c", "import time; time.sleep(10)"]
    speaker = SubprocessSpeaker(command=long_script, timeout=0.15)

    with pytest.raises(SpeechError) as exc_info:
        speaker.speak("This takes too long", blocking=True)

    assert "timed out" in str(exc_info.value).lower()
    assert speaker.is_speaking() is False


def test_stream_worker_receives_one_cleaned_line_per_sentence(tmp_path: Path):
    """The streaming worker in --lines mode receives one cleaned line per sentence."""
    out_file = tmp_path / "stream_received.txt"
    fake_worker = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            f"with open(r'{out_file}', 'w', encoding='utf-8') as f:\n"
            "    for line in sys.stdin:\n"
            "        f.write(line)\n"
            "        f.flush()\n"
        ),
    ]

    speaker = SubprocessSpeaker(command=fake_worker)
    speaker.begin_stream()
    speaker.feed("First sentence with ```code``` and https://example.com/link.")
    speaker.feed("Second sentence with *markdown* emphasis.")
    speaker.end_stream(wait=True)

    assert out_file.exists()
    lines = [line.strip() for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0] == "First sentence with code omitted and link."
    assert lines[1] == "Second sentence with markdown emphasis."


def test_stream_character_cap_triggers_single_rest_in_text_line(tmp_path: Path):
    """Passing 1200 characters triggers a single 'The rest is in the text.' line and ignores further feeds."""
    out_file = tmp_path / "stream_cap.txt"
    fake_worker = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            f"with open(r'{out_file}', 'w', encoding='utf-8') as f:\n"
            "    for line in sys.stdin:\n"
            "        f.write(line)\n"
            "        f.flush()\n"
        ),
    ]

    speaker = SubprocessSpeaker(command=fake_worker)
    speaker.begin_stream()

    # Feed chunks totaling over 1200 characters:
    # 500 chars -> total 500
    # 500 chars -> total 1000
    # 300 chars -> total 1300 > 1200 (triggers cap)
    c1 = "A" * 500
    c2 = "B" * 500
    c3 = "C" * 300
    c4 = "D" * 200

    speaker.feed(c1)
    speaker.feed(c2)
    speaker.feed(c3)
    speaker.feed(c4)  # Must be ignored
    speaker.end_stream(wait=True)

    assert out_file.exists()
    lines = [line.strip() for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3
    assert lines[0] == c1
    assert lines[1] == c2
    assert lines[2] == "The rest is in the text."


def test_stream_stop_kills_running_stream_process():
    """stop() immediately kills a running streaming worker process."""
    long_script = [sys.executable, "-c", "import sys, time; sys.stdin.readline(); time.sleep(15)"]
    speaker = SubprocessSpeaker(command=long_script)

    speaker.begin_stream()
    speaker.feed("First streaming line")
    assert speaker.is_speaking() is True

    speaker.stop()
    assert speaker.is_speaking() is False
    assert speaker._process is None


def test_feed_after_process_death_raises_speech_error():
    """Calling feed() after the worker process has terminated raises SpeechError."""
    short_script = [sys.executable, "-c", "import sys; sys.exit(0)"]
    speaker = SubprocessSpeaker(command=short_script)

    speaker.begin_stream()
    # Wait for short process to exit
    speaker._process.wait(timeout=2.0)
    assert speaker.is_speaking() is False

    with pytest.raises(SpeechError):
        speaker.feed("Another sentence after process died")


def test_second_begin_stream_stops_first():
    """A subsequent begin_stream() call terminates any previously running stream process."""
    long_script = [sys.executable, "-c", "import time; time.sleep(15)"]
    speaker = SubprocessSpeaker(command=long_script)

    speaker.begin_stream()
    proc1 = speaker._process
    assert speaker.is_speaking() is True

    speaker.begin_stream()
    proc2 = speaker._process

    assert proc1 is not None and proc2 is not None
    assert proc1 != proc2
    assert proc1.poll() is not None  # First process was terminated
    assert speaker.is_speaking() is True

    speaker.stop()
    assert speaker.is_speaking() is False

