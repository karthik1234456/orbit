"""Audio enhancement and signal processing module for ORBIT.

Pure numpy implementation: zero scipy, zero external dependencies.
Processes 16 kHz 1-D float32 audio arrays with fixed DSP constants.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
import numpy as np

logger = logging.getLogger(__name__)

# Fixed DSP constants (not settings, not tuned at runtime)
SAMPLE_RATE: int = 16000
HIGHPASS_CUTOFF_HZ: float = 80.0
HIGHPASS_TAPER_HZ: float = 20.0
AGC_TARGET_RMS: float = 0.08
AGC_MAX_GAIN_DB: float = 20.0
AGC_CLIP_CEILING: float = 0.979
SPECTRAL_FRAME_MS: float = 25.0
SPECTRAL_ALPHA: float = 1.5
SPECTRAL_BETA: float = 0.05
SPECTRAL_NOISE_PERCENTILE: float = 0.15
SPECTRAL_TIME_SMOOTH_GAMMA: float = 0.25

# Global record of last enhancement error for diagnostics
LAST_ENHANCE_ERROR: Optional[str] = None


def highpass(
    audio: np.ndarray,
    cutoff: float = HIGHPASS_CUTOFF_HZ,
    sr: int = SAMPLE_RATE,
) -> np.ndarray:
    """High-pass filter using FFT zeroing below cutoff with a smooth taper.

    Zeros frequency bins below cutoff and applies a smooth raised-cosine
    taper transition up to (cutoff + HIGHPASS_TAPER_HZ) to avoid time-domain ringing.
    Returns float32 1-D array matching input length.
    """
    if audio is None or len(audio) == 0:
        return audio
    if not isinstance(audio, np.ndarray):
        audio = np.asarray(audio, dtype=np.float32)
    if not np.all(np.isfinite(audio)):
        return audio
    if audio.ndim > 1:
        audio = audio.flatten()

    n = len(audio)
    if n < 4 or cutoff <= 0:
        return audio.astype(np.float32)

    # Compute single-sided FFT
    spec = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)

    taper_hz = min(HIGHPASS_TAPER_HZ, max(5.0, cutoff * 0.25))
    h = np.zeros_like(freqs, dtype=np.float32)

    # Passband: full transmission
    pass_mask = freqs >= (cutoff + taper_hz)
    h[pass_mask] = 1.0

    # Transition band: smooth raised cosine
    trans_mask = (freqs > cutoff) & (freqs < (cutoff + taper_hz))
    if np.any(trans_mask):
        frac = (freqs[trans_mask] - cutoff) / taper_hz
        h[trans_mask] = 0.5 * (1.0 - np.cos(np.pi * frac))

    # Stopband: freqs <= cutoff remain 0.0

    filtered = np.fft.irfft(spec * h, n=n)
    return filtered.astype(np.float32)


def agc(
    audio: np.ndarray,
    target_rms: float = AGC_TARGET_RMS,
    max_gain_db: float = AGC_MAX_GAIN_DB,
) -> np.ndarray:
    """Automatic Gain Control (AGC) leveling audio RMS with a peak clip guard.

    Scales signal toward target_rms up to max_gain_db (default +20 dB).
    Applies a clip guard scaling the peak below 0.98 if gain would induce clipping.
    Leaves silence unchanged.
    """
    if audio is None or len(audio) == 0:
        return audio
    if not isinstance(audio, np.ndarray):
        audio = np.asarray(audio, dtype=np.float32)
    if not np.all(np.isfinite(audio)):
        return audio
    if audio.ndim > 1:
        audio = audio.flatten()

    rms = float(np.sqrt(np.mean(np.square(audio))))
    # Silence in, silence out
    if rms <= 1e-8:
        return audio.astype(np.float32)

    desired_gain = target_rms / rms
    max_gain = float(10.0 ** (max_gain_db / 20.0))
    gain = min(desired_gain, max_gain)

    out = audio * gain
    peak = float(np.max(np.abs(out)))
    if peak >= 0.98:
        out = out * (AGC_CLIP_CEILING / peak)

    return out.astype(np.float32)


def spectral_gate(
    audio: np.ndarray,
    alpha: float = SPECTRAL_ALPHA,
    beta: float = SPECTRAL_BETA,
    sr: int = SAMPLE_RATE,
) -> np.ndarray:
    """Spectral subtraction gate for stationary background noise reduction.

    Performs STFT with 25 ms Hann frames and 50% overlap.
    Samples noise spectrum from the quietest 15% of frames.
    Subtracts alpha * noise_spectrum with floor beta * magnitude.
    Applies light temporal smoothing to prevent musical artifacts.
    Resynthesizes via overlap-add (OLA), guaranteeing identical output length.
    """
    if audio is None or len(audio) == 0:
        return audio
    if not isinstance(audio, np.ndarray):
        audio = np.asarray(audio, dtype=np.float32)
    if not np.all(np.isfinite(audio)):
        return audio
    if audio.ndim > 1:
        audio = audio.flatten()

    n = len(audio)
    frame_len = int(sr * (SPECTRAL_FRAME_MS / 1000.0))  # 400 samples
    hop_len = frame_len // 2  # 200 samples (50% overlap)

    if n < frame_len:
        return audio.astype(np.float32)

    # Check for near-total silence
    total_energy = float(np.mean(audio**2))
    if total_energy <= 1e-10:
        return audio.astype(np.float32)

    # Frame audio
    window = np.hanning(frame_len).astype(np.float32)
    pad_needed = (frame_len - (n % hop_len)) % hop_len
    if pad_needed > 0 or n < frame_len:
        padded = np.pad(audio, (0, pad_needed + frame_len), mode="constant")
    else:
        padded = np.pad(audio, (0, frame_len), mode="constant")

    n_frames = 1 + (len(padded) - frame_len) // hop_len
    frames = np.zeros((n_frames, frame_len), dtype=np.float32)
    for i in range(n_frames):
        start = i * hop_len
        frames[i] = padded[start : start + frame_len] * window

    # STFT
    spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec)
    phase = np.angle(spec)

    # Frame energies for quietest 15% selection
    frame_energies = np.sum(frames**2, axis=1)
    k_noise = max(1, int(np.ceil(SPECTRAL_NOISE_PERCENTILE * n_frames)))
    quiet_indices = np.argsort(frame_energies)[:k_noise]
    noise_profile = np.mean(mag[quiet_indices], axis=0)

    # Spectral subtraction with beta floor
    subtracted = np.maximum(mag - alpha * noise_profile, beta * mag)
    gains = subtracted / np.maximum(mag, 1e-8)

    # Light time smoothing across frames
    smoothed_gains = np.zeros_like(gains)
    smoothed_gains[0] = gains[0]
    gamma = SPECTRAL_TIME_SMOOTH_GAMMA
    for i in range(1, n_frames):
        smoothed_gains[i] = gamma * gains[i] + (1.0 - gamma) * smoothed_gains[i - 1]

    # Reconstruct frames
    clean_mag = mag * smoothed_gains
    clean_spec = clean_mag * np.exp(1j * phase)
    inv_frames = np.fft.irfft(clean_spec, n=frame_len, axis=1).astype(np.float32) * window

    # Overlap-add
    out_len = (n_frames - 1) * hop_len + frame_len
    out_buf = np.zeros(out_len, dtype=np.float32)
    norm_buf = np.zeros(out_len, dtype=np.float32)
    win2 = window**2

    for i in range(n_frames):
        start = i * hop_len
        out_buf[start : start + frame_len] += inv_frames[i]
        norm_buf[start : start + frame_len] += win2

    norm_mask = norm_buf > 1e-5
    out_buf[norm_mask] /= norm_buf[norm_mask]

    return out_buf[:n].astype(np.float32)


def estimate_snr_db(audio: np.ndarray, sr: int = SAMPLE_RATE) -> float:
    """Rough estimate of Signal-to-Noise Ratio (SNR) in dB from frame energies.

    Compares the energy of the quietest 15% of frames (ambient noise baseline)
    to the loudest 15% of frames (speech activity baseline).
    Returns a float rounded to 1 decimal place. Documented as a rough estimate.
    """
    if audio is None or len(audio) == 0:
        return 0.0
    if not isinstance(audio, np.ndarray):
        audio = np.asarray(audio, dtype=np.float32)
    if not np.all(np.isfinite(audio)):
        return 0.0

    frame_len = int(sr * (SPECTRAL_FRAME_MS / 1000.0))
    hop_len = frame_len // 2
    n = len(audio)
    if n < frame_len:
        return 0.0

    n_frames = 1 + (n - frame_len) // hop_len
    if n_frames < 2:
        return 0.0

    energies = np.array(
        [
            float(np.mean(audio[i * hop_len : i * hop_len + frame_len] ** 2))
            for i in range(n_frames)
        ],
        dtype=np.float32,
    )
    energies = np.sort(energies)
    k = max(1, int(np.ceil(SPECTRAL_NOISE_PERCENTILE * n_frames)))

    noise_energy = float(np.mean(energies[:k]))
    signal_energy = float(np.mean(energies[-k:]))

    if signal_energy <= 1e-9:
        return 0.0
    if noise_energy <= 1e-9:
        noise_energy = 1e-9

    snr = 10.0 * np.log10(signal_energy / noise_energy)
    return float(round(max(0.0, snr), 1))


def enhance(audio: np.ndarray, settings: Optional[Any] = None) -> np.ndarray:
    """Apply enabled audio enhancement filters in sequence: highpass -> agc -> gate.

    Never raises an exception: on any error returns the input audio unchanged
    and records the failure in LAST_ENHANCE_ERROR.
    """
    global LAST_ENHANCE_ERROR
    LAST_ENHANCE_ERROR = None

    if audio is None or len(audio) == 0:
        return audio
    if not isinstance(audio, np.ndarray):
        audio = np.asarray(audio, dtype=np.float32)
    if not np.all(np.isfinite(audio)):
        return audio

    if settings is None:
        try:
            from config import settings as cfg_settings

            settings = cfg_settings
        except Exception:
            settings = None

    hp_on = bool(getattr(settings, "VOICE_HIGHPASS", 0))
    agc_on = bool(getattr(settings, "VOICE_AGC", 0))
    gate_on = bool(getattr(settings, "VOICE_DENOISE", 0))

    out = audio
    try:
        if hp_on:
            out = highpass(out)
        if agc_on:
            out = agc(out)
        if gate_on:
            out = spectral_gate(out)
        return out
    except Exception as exc:
        LAST_ENHANCE_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("Audio enhancement failed: %s; returning original audio", exc)
        return audio

