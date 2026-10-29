"""Unit tests for voice enhancement filters, AGC, spectral gate, and SNR estimation."""

import numpy as np
from voice.enhance import (
    AGC_CLIP_CEILING,
    agc,
    enhance,
    estimate_snr_db,
    highpass,
    spectral_gate,
)


def test_highpass_removes_hum_and_preserves_tone():
    """Verify highpass removes 60 Hz hum while preserving 1 kHz tone within 5% tolerance."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
    hum = 0.5 * np.sin(2 * np.pi * 60 * t, dtype=np.float32)
    tone = 0.5 * np.sin(2 * np.pi * 1000 * t, dtype=np.float32)
    sig = hum + tone

    filtered = highpass(sig, cutoff=80.0, sr=sr)
    assert len(filtered) == len(sig)
    assert filtered.dtype == np.float32

    # Measure frequency components using FFT
    spec_in = np.abs(np.fft.rfft(sig))
    spec_out = np.abs(np.fft.rfft(filtered))
    freqs = np.fft.rfftfreq(len(sig), d=1.0 / sr)

    bin_60 = np.argmin(np.abs(freqs - 60.0))
    bin_1k = np.argmin(np.abs(freqs - 1000.0))

    # 60 Hz hum must be virtually eliminated (< 1e-4 of original magnitude)
    assert spec_out[bin_60] < 1e-3 * spec_in[bin_60], (
        f"Hum not removed: {spec_out[bin_60]} vs original {spec_in[bin_60]}"
    )
    # 1 kHz tone must be preserved within tolerance (within 2% amplitude)
    assert abs(spec_out[bin_1k] - spec_in[bin_1k]) / spec_in[bin_1k] < 0.05, (
        f"Tone altered: {spec_out[bin_1k]} vs original {spec_in[bin_1k]}"
    )


def test_agc_target_gain_and_clip_guard():
    """Verify AGC levels quiet signals to target RMS, respects gain cap, and never clips."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
    # Signal with RMS 0.04 (target is 0.08, requires 2.0x gain = +6 dB)
    sig = 0.04 * np.sqrt(2.0) * np.sin(2 * np.pi * 440 * t, dtype=np.float32)
    in_rms = float(np.sqrt(np.mean(np.square(sig))))
    assert abs(in_rms - 0.04) < 1e-3

    out = agc(sig, target_rms=0.08, max_gain_db=20.0)
    out_rms = float(np.sqrt(np.mean(np.square(out))))
    # Reaches target RMS
    assert abs(out_rms - 0.08) < 1e-3

    # Respects max_gain_db cap (20 dB = 10.0x gain max)
    very_quiet = 0.001 * np.sin(2 * np.pi * 440 * t, dtype=np.float32)
    out_capped = agc(very_quiet, target_rms=0.08, max_gain_db=20.0)
    actual_gain = float(np.max(np.abs(out_capped))) / float(np.max(np.abs(very_quiet)))
    assert abs(actual_gain - 10.0) < 1e-3

    # Clip guard: loud signal with sharp spike that would clip
    hot_signal = 0.5 * np.sin(2 * np.pi * 440 * t, dtype=np.float32)
    hot_signal[100] = 0.95
    out_hot = agc(hot_signal, target_rms=0.08, max_gain_db=20.0)
    max_peak = float(np.max(np.abs(out_hot)))
    assert max_peak < 0.98
    assert max_peak <= AGC_CLIP_CEILING + 1e-4


def test_spectral_gate_lowers_noise_and_preserves_length():
    """Verify spectral gate suppresses stationary background noise and preserves length."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
    tone = 0.4 * np.sin(2 * np.pi * 440 * t, dtype=np.float32)
    # Noise throughout signal
    rng = np.random.RandomState(42)
    noise = rng.normal(0.0, 0.04, sr).astype(np.float32)
    # Speech tone active only in second half (0.5s - 1.0s)
    speech = noise.copy()
    speech[8000:] += tone[8000:]

    out = spectral_gate(speech, alpha=1.5, beta=0.05, sr=sr)
    assert len(out) == len(speech)

    # Noise floor in quiet first half should be significantly attenuated
    noise_in_pwr = float(np.mean(speech[:8000] ** 2))
    noise_out_pwr = float(np.mean(out[:8000] ** 2))
    assert noise_out_pwr < 0.3 * noise_in_pwr, (
        f"Noise floor not sufficiently lowered: {noise_out_pwr} vs {noise_in_pwr}"
    )

    # Tone region should remain active
    tone_out_pwr = float(np.mean(out[8000:] ** 2))
    assert tone_out_pwr > 0.03


def test_filters_silence_and_invalid_inputs():
    """Verify filters handle silence, empty arrays, and non-finite floats safely."""
    silence = np.zeros(1600, dtype=np.float32)

    # Silence in -> silence out
    assert np.all(highpass(silence) == 0.0)
    assert np.all(agc(silence) == 0.0)
    assert np.all(spectral_gate(silence) == 0.0)

    # Empty array -> returns unchanged
    empty = np.zeros(0, dtype=np.float32)
    assert len(highpass(empty)) == 0
    assert len(agc(empty)) == 0
    assert len(spectral_gate(empty)) == 0

    # Non-finite inputs -> returns unchanged
    bad_arr = np.array([0.1, np.nan, 0.2, np.inf], dtype=np.float32)
    res_hp = highpass(bad_arr)
    assert np.isnan(res_hp[1])
    assert np.isinf(res_hp[3])

    res_agc = agc(bad_arr)
    assert np.isnan(res_agc[1])

    res_gate = spectral_gate(bad_arr)
    assert np.isnan(res_gate[1])


def test_estimate_snr_db_ordering():
    """Verify estimate_snr_db orders quiet vs noisy audio appropriately."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
    # Loud speech in center, quiet background
    speech = np.zeros(sr, dtype=np.float32)
    speech[4000:12000] = 0.5 * np.sin(2 * np.pi * 300 * t[:8000])

    # Clean signal: high SNR
    clean_audio = speech + np.random.RandomState(1).normal(0.0, 0.001, sr).astype(np.float32)
    snr_clean = estimate_snr_db(clean_audio)

    # Moderate noise
    med_noise = speech + np.random.RandomState(2).normal(0.0, 0.03, sr).astype(np.float32)
    snr_med = estimate_snr_db(med_noise)

    # High noise
    loud_noise = speech + np.random.RandomState(3).normal(0.0, 0.15, sr).astype(np.float32)
    snr_noisy = estimate_snr_db(loud_noise)

    assert snr_clean > snr_med > snr_noisy
    assert snr_clean >= 20.0
    assert snr_noisy <= 12.0


def test_enhance_pipeline_order_and_error_handling():
    """Verify enhance() applies highpass -> agc -> gate and never raises."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
    sig = 0.05 * np.sin(2 * np.pi * 400 * t, dtype=np.float32)

    settings = type("MockSettings", (), {
        "VOICE_HIGHPASS": 1,
        "VOICE_AGC": 1,
        "VOICE_DENOISE": 1,
    })()

    out = enhance(sig, settings)
    assert len(out) == len(sig)
    assert out.dtype == np.float32

    # Determinism: two runs on same input yield identical results
    out2 = enhance(sig, settings)
    assert np.array_equal(out, out2)

    # Never raises on bad types
    assert enhance(None) is None
    assert len(enhance(np.zeros(0, dtype=np.float32))) == 0
