"""Unit tests for the voice review step, noise status, and UI review gate."""

import tkinter as tk
from unittest.mock import MagicMock
import pytest

from voice.review import compute_needs_review, snr_to_noise_status
from voice.stt import Transcript


@pytest.mark.parametrize(
    "setting,avg_logprob,no_speech,snr,text,expected_review,expected_reason_substr",
    [
        # VOICE_REVIEW=never always returns False
        ("never", -2.5, 0.9, 5.0, "hi", False, None),
        ("never", -0.2, 0.1, 25.0, "open notepad", False, None),
        # VOICE_REVIEW=always always returns True
        ("always", -0.1, 0.05, 28.0, "open notepad please", True, "Low confidence"),
        ("always", -0.1, 0.05, 8.0, "open notepad please", True, "Noisy room: a headset helps"),
        # VOICE_REVIEW=auto: clean transcript passes without review
        ("auto", -0.3, 0.1, 24.0, "open notepad", False, None),
        ("auto", -0.5, 0.2, 15.0, "launch calculator now", False, None),
        # VOICE_REVIEW=auto: low avg_logprob (< -1.0)
        ("auto", -1.2, 0.1, 22.0, "open notepad", True, "Low confidence"),
        # VOICE_REVIEW=auto: high no_speech_prob (> 0.6)
        ("auto", -0.4, 0.75, 22.0, "open notepad", True, "Low confidence"),
        # VOICE_REVIEW=auto: low estimated SNR (< 10 dB)
        ("auto", -0.2, 0.1, 8.5, "open notepad", True, "Noisy room: a headset helps"),
        # VOICE_REVIEW=auto: fewer than 2 words (< 2 words)
        ("auto", -0.1, 0.05, 25.0, "notepad", True, "Low confidence"),
        ("auto", -0.1, 0.05, 25.0, "", True, "Low confidence"),
    ],
)
def test_compute_needs_review_decision_table(
    setting, avg_logprob, no_speech, snr, text, expected_review, expected_reason_substr
):
    """Verify review decision over combinations of settings and acoustic metrics."""
    trans = Transcript(
        text=text,
        language="en",
        duration_s=1.5,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech,
    )
    needs_review, reason = compute_needs_review(trans, snr_db=snr, review_setting=setting)
    assert needs_review == expected_review
    if expected_reason_substr is not None:
        assert reason is not None
        assert expected_reason_substr.lower() in reason.lower()
    else:
        assert reason is None


def test_snr_to_noise_status_mapping():
    """Verify mapping of SNR to high, medium, and low noise labels."""
    assert snr_to_noise_status(5.0) == "Noise: high"
    assert snr_to_noise_status(9.9) == "Noise: high"
    assert snr_to_noise_status(10.0) == "Noise: medium"
    assert snr_to_noise_status(19.9) == "Noise: medium"
    assert snr_to_noise_status(20.0) == "Noise: low"
    assert snr_to_noise_status(35.0) == "Noise: low"


def test_voice_review_needed_does_not_submit():
    """Verify voice_review_needed event places text in input box and does not submit to controller."""
    from tests.conftest import check_tk_available, make_test_root
    check_tk_available()
    from ui.app import OrbitWindow
    from ui.controller import UiEvent

    root = make_test_root()
    controller = MagicMock()
    controller.submit.return_value = True

    app = OrbitWindow(root, controller, mode_label="MOCK")
    try:
        # Simulate review needed event
        review_data = {
            "text": "open notepad",
            "reason": "Low confidence",
            "noise_status": "Noise: high",
        }
        app._handle_event(UiEvent("voice_review_needed", review_data))

        # 1. Text is placed in the input box
        box_text = app.input_text.get("1.0", tk.END).strip()
        assert box_text == "open notepad"

        # 2. Controller.submit was NOT called automatically
        assert controller.submit.call_count == 0

        # 3. Pending voice review flag is armed
        assert app._pending_voice_reviewed is True

        # 4. User sends text (e.g. presses Enter)
        app._on_send()

        # 5. Controller receives text with source='voice-reviewed'
        controller.submit.assert_called_once_with("open notepad", source="voice-reviewed")
        assert app._pending_voice_reviewed is False
    finally:
        root.destroy()


def test_voice_cannot_resolve_confirm_or_plan_dialog():
    """Verify voice input NEVER resolves ConfirmDialog or PlanDialog."""
    from tests.conftest import check_tk_available, make_test_root
    check_tk_available()
    from ui.app import ConfirmDialog, OrbitWindow
    from ui.controller import ConfirmRequest, UiEvent

    root = make_test_root()
    controller = MagicMock()

    app = OrbitWindow(root, controller, mode_label="MOCK")
    try:
        req = ConfirmRequest(
            id=1,
            tool="open_application",
            args={"name": "notepad"},
            warning=None,
            timeout_s=15.0,
        )
        dlg = ConfirmDialog(root, controller, req)
        app._confirm_dialog = dlg

        # Simulate voice transcript arriving
        app._handle_event(UiEvent("voice_transcript", {"text": "yes", "source": "voice"}))

        # Dialog must remain open; voice input cannot approve or dismiss it
        assert dlg.winfo_exists()
        controller.confirm.assert_not_called()
        dlg.destroy()
    finally:
        root.destroy()
