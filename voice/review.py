"""Voice review step logic and noise status evaluation.

Computes whether a voice transcript requires user review before execution based on:
- VOICE_REVIEW policy setting ('auto', 'always', 'never')
- Whisper decoding log probabilities and no-speech probability
- Estimated signal-to-noise ratio (SNR)
- Transcript word count

SECURITY GUARANTEE:
Transcripts marked for review are held locally in the input box / terminal prompt.
They are never sent to the model or executed until the user explicitly confirms (Enter or 'y').
ConfirmDialog and PlanDialog remain strictly mouse-and-keyboard only.
"""

from __future__ import annotations

from typing import Optional

from voice.stt import Transcript


def snr_to_noise_status(snr_db: float) -> str:
    """Map numeric SNR in dB to standard status line display string."""
    if snr_db < 10.0:
        return "Noise: high"
    if snr_db < 20.0:
        return "Noise: medium"
    return "Noise: low"


def compute_needs_review(
    transcript: Transcript,
    snr_db: float,
    review_setting: str = "auto",
) -> tuple[bool, Optional[str]]:
    """Determine whether voice transcript requires user review.

    Returns (needs_review, reason):
    - review_setting == "always": True, "Low confidence" (or review required)
    - review_setting == "never": False, None
    - review_setting == "auto":
        True if avg_logprob < -1.0, no_speech_prob > 0.6, snr_db < 10.0 dB,
        or word_count < 2.
        Reason is "Noisy room: a headset helps" if snr_db < 10.0, else "Low confidence".
        False if all checks pass.
    """
    setting = (review_setting or "auto").strip().lower()

    if setting == "never":
        return False, None

    if setting == "always":
        if snr_db < 10.0:
            return True, "Noisy room: a headset helps"
        return True, "Low confidence"

    # auto mode
    is_noisy = snr_db < 10.0
    low_logprob = transcript.avg_logprob is not None and transcript.avg_logprob < -1.0
    high_no_speech = transcript.no_speech_prob is not None and transcript.no_speech_prob > 0.6
    words = transcript.text.strip().split()
    too_few_words = len(words) < 2

    if is_noisy:
        return True, "Noisy room: a headset helps"

    if low_logprob or high_no_speech or too_few_words:
        return True, "Low confidence"

    return False, None

