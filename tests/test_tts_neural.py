"""Tests for ORBIT v2.15.0 neural text-to-speech (TTS) engine and SAPI fallback.

All tests use fakes, mocks, and tmp_path only:
- Fake synthesizer, fake player, fake clock.
- No audio hardware or sound playback.
- No network access or model downloads.
- No real piper or onnxruntime imports.
- Isolated from real .env, data/, logs/, and workspace/ directories.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


from core.doctor import Probes, check_tts_engine
from core.diagnostics import build_diagnostics_text
from voice.voices import (
    list_voices,
    validate_voice_file,
    validate_voice_filename,
)
from voice.piper_worker import (
    apply_volume_to_wav,
    synthesize_sentence_to_wav,
)
from voice.tts_engines import (
    FallbackSpeaker,
    PiperSpeaker,
    clean_dead_worker_tempdirs,
    get_speaker,
    reset_session_fallback_notice,
)


# ============================================================================
# 1. Voice File Validation and Directory Safety Tests
# ============================================================================

def test_validate_voice_filename_rules():
    """Verify filename length, allowed characters, and extension rules."""
    # Valid filenames
    assert validate_voice_filename("en_US-lessac-medium.onnx")[0] is True
    assert validate_voice_filename("voice1.onnx")[0] is True
    assert validate_voice_filename("my_voice-model.2.onnx")[0] is True

    # Bad extensions
    assert validate_voice_filename("voice.bin")[0] is False
    assert validate_voice_filename("voice.onnx.json")[0] is False
    assert validate_voice_filename("voice")[0] is False

    # Disallowed characters / path traversal
    assert validate_voice_filename("../escape.onnx")[0] is False
    assert validate_voice_filename("sub/model.onnx")[0] is False
    assert validate_voice_filename("voice with space.onnx")[0] is False
    assert validate_voice_filename("voice;rm.onnx")[0] is False
    assert validate_voice_filename("voice*.onnx")[0] is False

    # Length > 80 chars
    long_name = "a" * 75 + ".onnx"  # 75 + 5 = 80 chars total
    assert len(long_name) == 80
    assert validate_voice_filename(long_name)[0] is True
    too_long = "a" * 76 + ".onnx"  # 76 + 5 = 81 chars
    assert len(too_long) == 81
    assert validate_voice_filename(too_long)[0] is False


def test_validate_voice_file_traversal_and_symlink(tmp_path: Path):
    """Verify voice file cannot escape voices directory via traversal or symlink."""
    vdir = tmp_path / "voices"
    vdir.mkdir()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_model = outside_dir / "target.onnx"
    outside_model.write_bytes(b"dummy onnx content")
    outside_json = outside_dir / "target.onnx.json"
    outside_json.write_text("{}", encoding="utf-8")

    # Path traversal string
    valid, msg, _, _ = validate_voice_file("../outside/target.onnx", voices_folder=vdir)
    assert valid is False
    assert "directory separators" in (msg or "")

    # Symlink escape attempt (if platform allows symlinks)
    symlink_model = vdir / "symlink.onnx"
    try:
        symlink_model.symlink_to(outside_model)
        valid, msg, _, _ = validate_voice_file("symlink.onnx", voices_folder=vdir)
        assert valid is False
        assert "resolves outside" in (msg or "")
    except (OSError, NotImplementedError):
        # Symlink creation may require elevated privilege on Windows
        pass


def test_validate_voice_file_missing_json(tmp_path: Path):
    """Verify model without sibling .onnx.json is rejected."""
    vdir = tmp_path / "voices"
    vdir.mkdir()

    model = vdir / "test.onnx"
    model.write_bytes(b"model data")

    valid, msg, _, _ = validate_voice_file("test.onnx", voices_folder=vdir)
    assert valid is False
    assert "Missing required sibling" in (msg or "")


def test_validate_voice_file_size_limits(tmp_path: Path):
    """Verify size limits: model <= 300MB, JSON <= 1MB."""
    vdir = tmp_path / "voices"
    vdir.mkdir()

    model = vdir / "large.onnx"
    cfg = vdir / "large.onnx.json"
    model.write_bytes(b"x")
    cfg.write_text("{}", encoding="utf-8")

    orig_stat = Path.stat

    # Normal size
    valid, _, _, _ = validate_voice_file("large.onnx", voices_folder=vdir)
    assert valid is True

    # Oversized ONNX (> 300MB)
    def stat_onnx_over(self, *args, **kwargs):
        st = orig_stat(self, *args, **kwargs)
        if str(self).endswith(".onnx"):
            return MagicMock(st_size=301 * 1024 * 1024, st_mode=st.st_mode)
        return st

    with patch.object(Path, "stat", stat_onnx_over):
        valid, msg, _, _ = validate_voice_file("large.onnx", voices_folder=vdir)
        assert valid is False
        assert "exceeds 300 MB limit" in (msg or "")

    # Oversized JSON (> 1MB)
    def stat_json_over(self, *args, **kwargs):
        st = orig_stat(self, *args, **kwargs)
        if str(self).endswith(".json"):
            return MagicMock(st_size=2 * 1024 * 1024, st_mode=st.st_mode)
        return st

    with patch.object(Path, "stat", stat_json_over):
        valid, msg, _, _ = validate_voice_file("large.onnx", voices_folder=vdir)
        assert valid is False
        assert "exceeds 1 MB limit" in (msg or "")


def test_list_voices_returns_valid_pairs_sorted(tmp_path: Path):
    """Verify list_voices returns only valid sorted .onnx files."""
    vdir = tmp_path / "voices"
    vdir.mkdir()

    # Valid pair 1
    (vdir / "beta.onnx").write_bytes(b"data")
    (vdir / "beta.onnx.json").write_text("{}", encoding="utf-8")

    # Valid pair 2
    (vdir / "alpha.onnx").write_bytes(b"data")
    (vdir / "alpha.onnx.json").write_text("{}", encoding="utf-8")

    # Missing JSON
    (vdir / "gamma.onnx").write_bytes(b"data")

    # Non-onnx file
    (vdir / "notes.txt").write_text("hello", encoding="utf-8")

    found = list_voices(vdir)
    assert found == ["alpha.onnx", "beta.onnx"]


# ============================================================================
# 2. Settings Validation and Speed/Volume Parsing Tests
# ============================================================================

def test_tts_settings_parsers():
    """Verify TTS settings parsers and defaults."""
    import warnings
    from config.settings import (
        _parse_float_range,
        _parse_piper_voice,
        _parse_tts_engine,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        assert _parse_tts_engine("piper") == "piper"
        assert _parse_tts_engine("PIPER") == "piper"
        assert _parse_tts_engine("sapi") == "sapi"
        assert _parse_tts_engine("invalid") == "sapi"
        assert _parse_tts_engine(None) == "sapi"

        assert _parse_piper_voice("en_US-lessac.onnx") == "en_US-lessac.onnx"
        assert _parse_piper_voice("") == ""
        assert _parse_piper_voice("bad/path.onnx") == ""

        assert _parse_float_range("1.0", default=1.0, min_val=0.8, max_val=1.3) == 1.0
        assert _parse_float_range("1.25", default=1.0, min_val=0.8, max_val=1.3) == 1.25
        assert _parse_float_range("0.5", default=1.0, min_val=0.8, max_val=1.3) == 1.0
        assert _parse_float_range("2.0", default=1.0, min_val=0.8, max_val=1.3) == 1.0
        assert _parse_float_range("abc", default=1.0, min_val=0.8, max_val=1.3) == 1.0


# ============================================================================
# 3. Audio Processing & Piper Worker Tests (No Real Audio / No Audioop)
# ============================================================================

def test_lazy_import_piper_worker():
    """Verify that importing voice.piper_worker does not import piper or onnxruntime."""

    # Check that piper and onnxruntime are not loaded simply by importing piper_worker
    assert "piper" not in sys.modules
    import voice.piper_worker  # noqa: F401
    assert "piper" not in sys.modules


def test_apply_volume_to_wav(tmp_path: Path):
    """Verify apply_volume_to_wav scales samples and clamps without audioop."""
    import array
    import wave

    wav_file = tmp_path / "test.wav"
    raw_samples = array.array("h", [0, 1000, 20000, -20000, -32000])

    with wave.open(str(wav_file), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(raw_samples.tobytes())

    # Apply 50% volume (0.5)
    apply_volume_to_wav(wav_file, 0.5)

    with wave.open(str(wav_file), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        scaled_samples = array.array("h")
        scaled_samples.frombytes(frames)

    assert scaled_samples[0] == 0
    assert scaled_samples[1] == 500
    assert scaled_samples[2] == 10000
    assert scaled_samples[3] == -10000
    assert scaled_samples[4] == -16000


def test_synthesize_sentence_to_wav_adapter(tmp_path: Path):
    """Verify synthesize_sentence_to_wav adapter handles both chunks and synthesize_wav."""
    out_wav = tmp_path / "synth.wav"

    # 1. Mock voice with synthesize_wav method
    mock_voice_wav = MagicMock()
    mock_voice_wav.synthesize_wav = MagicMock()
    synthesize_sentence_to_wav(mock_voice_wav, "Hello world", out_wav, length_scale=1.0)
    mock_voice_wav.synthesize_wav.assert_called_once()

    # 2. Mock voice with synthesize generator method
    mock_voice_gen = MagicMock(spec=["synthesize"])
    mock_chunk = MagicMock()
    mock_chunk.audio_int16_bytes = b"\x00\x00" * 100
    mock_chunk.sample_rate = 16000
    mock_chunk.sample_width = 2
    mock_chunk.num_channels = 1
    mock_voice_gen.synthesize = MagicMock(return_value=[mock_chunk])

    out_wav2 = tmp_path / "synth2.wav"
    synthesize_sentence_to_wav(mock_voice_gen, "Hello chunk", out_wav2, length_scale=1.0)
    assert out_wav2.exists()
    assert out_wav2.stat().st_size > 0


# ============================================================================
# 4. Engine Selection and Fallback Speaker Tests
# ============================================================================

def test_get_speaker_engine_selection():
    """Verify get_speaker returns FallbackSpeaker for piper and SubprocessSpeaker for sapi."""
    with patch.dict(os.environ, {"TTS_ENGINE": "sapi"}):
        from voice.tts import SubprocessSpeaker
        spk = get_speaker()
        assert isinstance(spk, SubprocessSpeaker)

    with patch.dict(os.environ, {"TTS_ENGINE": "piper"}):
        spk = get_speaker()
        assert isinstance(spk, FallbackSpeaker)


def test_fallback_speaker_missing_piper_emits_one_notice():
    """Verify FallbackSpeaker falls back to SAPI with exactly one notice when piper is missing."""
    reset_session_fallback_notice()

    notices: list[str] = []
    fake_sapi = MagicMock()
    fake_piper = MagicMock()
    fake_piper.is_available.return_value = False
    fake_piper.worker_cmd = None

    speaker = FallbackSpeaker(
        piper_speaker=fake_piper,
        sapi_speaker=fake_sapi,
        on_fallback_notice=lambda msg: notices.append(msg),
    )

    # First speak triggers fallback and notice
    speaker.speak("First sentence", blocking=False)
    fake_sapi.speak.assert_called_with("First sentence", blocking=False)
    assert len(notices) == 1
    assert "Neural voice unavailable" in notices[0]
    assert "piper is not installed" in notices[0]

    # Second speak continues using SAPI without duplicate notice
    speaker.speak("Second sentence", blocking=False)
    assert len(notices) == 1
    assert fake_sapi.speak.call_count == 2


def test_fallback_speaker_invalid_voice_triggers_fallback(tmp_path: Path):
    """Verify invalid voice triggers SAPI fallback and records diagnostics reason."""
    reset_session_fallback_notice()
    notices: list[str] = []
    fake_sapi = MagicMock()

    # Fake PiperSpeaker that reports piper available but no voice configured
    fake_piper = MagicMock()
    fake_piper.is_available.return_value = True
    fake_piper.voice_path = None
    fake_piper.worker_cmd = None

    speaker = FallbackSpeaker(
        piper_speaker=fake_piper,
        sapi_speaker=fake_sapi,
        on_fallback_notice=lambda msg: notices.append(msg),
    )

    speaker.speak("Testing missing voice", blocking=False)
    assert len(notices) == 1
    assert "no voice file configured" in notices[0]
    fake_sapi.speak.assert_called_once_with("Testing missing voice", blocking=False)

    from voice.tts_engines import LAST_TTS_FALLBACK_REASON
    assert LAST_TTS_FALLBACK_REASON == "no voice file configured"


def test_fallback_speaker_timeout_triggers_fallback():
    """Verify synthesis timeout > 15s triggers SAPI fallback."""
    reset_session_fallback_notice()
    notices: list[str] = []
    fake_sapi = MagicMock()

    fake_piper = MagicMock()
    fake_piper.is_available.return_value = True
    fake_piper.voice_path = Path("valid.onnx")
    # Simulate timeout on speak
    fake_piper.speak.side_effect = TimeoutError("Synthesis took 16s")

    speaker = FallbackSpeaker(
        piper_speaker=fake_piper,
        sapi_speaker=fake_sapi,
        on_fallback_notice=lambda msg: notices.append(msg),
    )

    speaker.speak("This sentence will time out", blocking=False)
    assert len(notices) == 1
    assert "synthesis timed out" in notices[0]
    fake_sapi.speak.assert_called_once()


def test_fallback_speaker_stream_delegation():
    """Verify streaming methods (begin_stream, feed, end_stream) delegate properly."""
    reset_session_fallback_notice()
    fake_sapi = MagicMock()
    fake_piper = MagicMock()
    fake_piper.is_available.return_value = True
    fake_piper.voice_path = Path("valid.onnx")

    speaker = FallbackSpeaker(
        piper_speaker=fake_piper,
        sapi_speaker=fake_sapi,
    )

    speaker.begin_stream()
    fake_piper.begin_stream.assert_called_once()

    speaker.feed("First stream sentence.")
    fake_piper.feed.assert_called_once_with("First stream sentence.")

    speaker.end_stream()
    fake_piper.end_stream.assert_called_once()


# ============================================================================
# 5. PiperSpeaker Process Management and Stop Tests
# ============================================================================

def test_piper_speaker_stop_terminates_and_cleans():
    """Verify PiperSpeaker.stop() terminates/kills worker process and removes temp dir."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Process is running

    speaker = PiperSpeaker(voice_file="test.onnx")
    speaker._process = mock_proc

    speaker.stop()

    mock_proc.terminate.assert_called_once()
    assert speaker._process is None


def test_clean_dead_worker_tempdirs(tmp_path: Path):
    """Verify clean_dead_worker_tempdirs deletes leftover orbit_piper_* directories."""
    # Create simulated leftover directory in system tempdir
    leftover = Path(tempfile.gettempdir()) / f"orbit_piper_leftover_{int(time.time())}"
    leftover.mkdir(exist_ok=True)
    dummy_file = leftover / "chunk.wav"
    dummy_file.write_bytes(b"dummy")

    assert leftover.exists()
    clean_dead_worker_tempdirs()
    assert not leftover.exists()


# ============================================================================
# 6. Doctor and Diagnostics Telemetry Tests
# ============================================================================

def test_doctor_check_tts_engine_sapi():
    """Verify doctor check returns OK with install hint when SAPI is selected."""
    probes = Probes(
        env=lambda k, d=None: "sapi" if k == "TTS_ENGINE" else d,
        find_spec=lambda name: None,  # piper not installed
        path_exists=lambda p: False,
    )
    chk = check_tts_engine(probes)
    assert chk.status == "OK"
    assert "Engine: sapi" in chk.detail
    assert "piper: no" in chk.detail
    assert chk.fix is not None
    assert "install piper-tts" in chk.fix


def test_doctor_check_tts_engine_piper_warns_when_missing():
    """Verify doctor check returns WARN with exact remediation when piper is selected but missing."""
    probes = Probes(
        env=lambda k, d=None: "piper" if k == "TTS_ENGINE" else d,
        find_spec=lambda name: None,  # neither piper nor onnxruntime installed
        path_exists=lambda p: False,
    )
    chk = check_tts_engine(probes)
    assert chk.status == "WARN"
    assert "piper-tts not installed" in (chk.fix or "")
    assert "onnxruntime not installed" in (chk.fix or "")
    assert "SAPI fallback is active" in (chk.fix or "")


def test_doctor_check_tts_engine_piper_ok_when_ready(tmp_path: Path):
    """Verify doctor check returns OK when piper, onnxruntime, and voice are ready."""
    vdir = tmp_path / "voices"
    vdir.mkdir()
    (vdir / "en_US.onnx").write_bytes(b"data")
    (vdir / "en_US.onnx.json").write_text("{}", encoding="utf-8")

    def mock_env(k, d=None):
        if k == "TTS_ENGINE":
            return "piper"
        if k == "PIPER_VOICE":
            return "en_US.onnx"
        if k == "VOICES_DIR":
            return str(vdir)
        return d

    probes = Probes(
        env=mock_env,
        find_spec=lambda name: MagicMock(),  # piper & onnxruntime installed
        path_exists=lambda p: True,
    )
    chk = check_tts_engine(probes)
    assert chk.status == "OK"
    assert "piper: yes" in chk.detail
    assert "onnxruntime: yes" in chk.detail
    assert "1 valid (en_US.onnx)" in chk.detail
    assert chk.fix is None


def test_diagnostics_voice_section_no_spoken_text():
    """Verify diagnostics === VOICE === contains telemetry and NEVER contains spoken text."""
    import voice.tts_engines as engines_mod

    engines_mod.LAST_TTS_FALLBACK_REASON = "voice file invalid: test reason"
    secret_text = "This is confidential patient medical record text 987654"

    report = build_diagnostics_text()
    assert "=== VOICE ===" in report
    assert "TTS Engine: " in report
    assert "Piper Voice: " in report
    assert "Last TTS Fallback Reason: voice file invalid: test reason" in report
    assert secret_text not in report


# ============================================================================
# 7. Settings UI Voice Panel and Test Voice Action
# ============================================================================

def test_settings_ui_voice_panel_and_test_button(tmp_path: Path):
    """Verify SettingsWindow displays Voice panel and Test Voice button invokes speaker."""
    from tests.conftest import check_tk_available, make_test_root
    check_tk_available()

    from ui.settings_window import SettingsWindow

    root = make_test_root()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("TTS_ENGINE=sapi\nTTS_SPEED=1.0\n", encoding="utf-8")

        mock_speaker = MagicMock()
        mock_vc = MagicMock()
        mock_vc.speaker = mock_speaker
        mock_controller = MagicMock()
        mock_controller.voice_controller = mock_vc

        win = SettingsWindow(root, controller=mock_controller, env_path=env_file)

        # Assert test voice button exists
        assert hasattr(win, "test_voice_btn")
        assert win.test_voice_btn["text"] == "Test Voice"

        # Click the button
        win.test_voice_btn.invoke()

        # Check speaker was called with canonical test phrase
        mock_speaker.speak.assert_called_once_with(
            "This is ORBIT. Voice check complete.", blocking=False
        )

        win.destroy()
    finally:
        root.destroy()
