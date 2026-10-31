# ORBIT Hands-Free Wake Word & Ambient Listening Guide

ORBIT v2.25 introduces optional, local hands-free listening inside the desktop window. When enabled, ORBIT monitors microphone audio continuously on an isolated background daemon thread using a compact local model (`tiny.en`), detects the configured wake phrase (`hey orbit`), and submits recognized voice commands through the standard agent execution flow with source `"voice-handsfree"`.

---

## 1. How Hands-Free Works

1. **Continuous VAD Segmentation**: Incoming microphone frames pass through `UtteranceSegmenter`. The background worker cuts voice activity into distinct speech segments.
2. **Pre-Filtering Window**: Wake word detection runs **strictly** on segments between **0.3 s and 4.0 s** in duration. Segments outside this range are discarded immediately without invoking the neural model.
3. **Cropping & Wake STT**: Audio segments are cropped to the first **3.0 s** before being passed to `WAKE_STT_MODEL` (default `tiny.en`).
4. **Strict Ephemeral Privacy**: Non-wake transcripts are **discarded at once**. They are never logged to disk, never stored in memory attributes, and never transmitted.
5. **Command Ingestion**:
   - **Inline remainder**: If the user said `"hey orbit what time is it"`, the remainder (`"what time is it"`) is extracted directly.
   - **Separate capture**: If the user said only `"hey orbit"`, ORBIT plays the rising wake cue and enters the `"capturing"` state, endpointing the next command utterance (timeout 6 s, silence 0.8 s, max 30 s).
6. **Command Processing & Quality Drop Gate**: The command audio is enhanced using the active DSP settings, transcribed with `STT_MODEL` (default `base.en`), and tested against strict quality thresholds:
   - `avg_logprob < -1.0`
   - `no_speech_prob > 0.6`
   - Fewer than 2 words
   - Empty transcript
   If any threshold fails, ORBIT plays two descending error tones, prints `"Hands-free: I didn't catch that"`, and drops the command. Nothing is placed in the input box and nothing is submitted.
7. **Execution**: Valid commands are submitted via `controller.submit(text, source="voice-handsfree")` identically to typed input.
8. **Follow-Up Window**: After ORBIT delivers a spoken or text reply, an optional follow-up window (`HANDS_FREE_FOLLOWUP_SECONDS`, default 0, range 0–10) accepts one immediate reply utterance without requiring the wake phrase.

---

## 2. Security Boundaries & Physical Approvals

> [!IMPORTANT]
> **Voice Only Produces Text; Approvals Remain Physical**
> No voice utterance can approve, confirm, or resolve any security dialog (`ConfirmDialog`, `PlanDialog`). Saying "yes", "confirm", or "proceed" aloud has zero effect on open confirmation dialogs. Confirmations strictly require a physical mouse click or Enter keypress.

### Stream Muting & Gating Rules
The hands-free audio stream is automatically muted (frames dropped, segmenter reset) under the following conditions:
1. **Echo Suppression**: While ORBIT is actively speaking via TTS, and for **1.0 s after** speech ends.
2. **Active Dialogs**: While any `ConfirmDialog` or `PlanDialog` is open awaiting approval (plays two quick attention alert tones and cancels any active follow-up window).
3. **Active Reminder Toasts**: While a reminder notification toast is visible on screen.
4. **Task Execution**: While a background plan or task is running, unless inside an active follow-up window.
5. **Click-to-Talk Priority**: When the user clicks the dock microphone or presses `Ctrl+M`, hands-free listening pauses, releases the microphone completely, and only resumes after manual recording finishes.
6. **Rate Limiting**: At most 6 wake events per minute. Attempting 7 triggers an automatic 30-second cooldown period (`handsfree_cooldown`).
7. **Idle Auto-Off**: Hands-free turns itself off after `HANDS_FREE_IDLE_MINUTES` (default 10 minutes) without a wake trigger (`handsfree_auto_off`).
8. **Audio Errors**: Three consecutive microphone or stream errors automatically disable hands-free with an error notice.

---

## 3. Audio Cues

Audio cues provide non-intrusive feedback for state transitions using short vector tones (`winsound.Beep`):
- **Wake** (`800 Hz -> 1200 Hz`): One rising tone when the wake phrase is recognized.
- **Done** (`600 Hz`): One soft tone when a command is accepted for submission.
- **Error** (`500 Hz -> 350 Hz`): Two descending tones when speech is dropped for low confidence.
- **Attention** (`880 Hz, 880 Hz`): Two quick alert tones when a security dialog opens.
- **Off** (`300 Hz`): One low tone when hands-free turns off.

Cues can be toggled via `HANDS_FREE_CUES=0|1` (default `1`). Cues never carry any text.

---

## 4. Measuring Your Environment: The Wake Probe

Before enabling hands-free listening in a new room, measure your acoustic environment using the included live probe:

```powershell
python scripts/wake_probe.py
```

The probe runs three structured stages without storing any audio or text:
- **Stage (a) Detection**: Tests 10 prompted wake phrase utterances with a visual countdown and computes detection rate and median detection latency.
- **Stage (b) Ambient**: Listens for 120 seconds while you play your typical ambient room sound (television, podcast, music) to evaluate false wake triggers, segment count, process CPU usage, and maximum Whisper decode latency.
- **Stage (c) Summary**: Displays measured figures alongside reference guidance targets.

### Reference Guidance Targets

| Metric | Target Guidance | Description |
| :--- | :--- | :--- |
| **Detection Rate** | $\ge 9$ of 10 trials | High sensitivity to intentional wake phrases |
| **Detection Latency** | $\le 600\text{ ms}$ | Median time from end of utterance to wake trigger |
| **Ambient False Wakes** | $\le 2$ in 10 minutes ($\le 1$ in 2 min) | Minimal spurious triggers from background speech |
| **CPU Utilization** | $\le 5\%$ process CPU | Low background overhead on host system |

*Note: These targets represent operational guidance, not guaranteed benchmark results. Users should evaluate their room acoustics and decide whether to enable hands-free.*

---

## 5. Configuration Settings

| Setting | Type | Range / Options | Default | Description |
| :--- | :---: | :---: | :---: | :--- |
| `HANDS_FREE` | `bool` | `0 \| 1` | `0` | Enable hands-free listening (requires privacy confirmation) |
| `WAKE_PHRASE` | `text` | 2–3 lowercase words, 6–24 chars | `"hey orbit"` | Custom wake phrase for fuzzy activation |
| `WAKE_SENSITIVITY`| `float` | `0.75` – `0.90` | `0.80` | Fuzzy matching SequenceMatcher threshold |
| `HANDS_FREE_IDLE_MINUTES` | `int` | `1` – `60` | `10` | Minutes of silence before turning off |
| `HANDS_FREE_FOLLOWUP_SECONDS` | `int`| `0` – `10` | `0` | Duration of follow-up window after replies |
| `HANDS_FREE_CUES` | `bool` | `0 \| 1` | `1` | Play tone cues for state transitions |
| `WAKE_STT_MODEL` | `text` | Whisper model tag | `"tiny.en"` | Lightweight local model for wake detection |

