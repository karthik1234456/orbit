# ORBIT Live Verification & Test Checklist

This document provides a repeatable, human-executable verification checklist for testing live ORBIT features across terminal and desktop GUI environments.

Each test case specifies the feature area, exact command or user action, expected behavior, verification outcome, and space for tester notes.

> [!NOTE]
> **Execution Mode Agnostic**: Every live verification item in this checklist can be executed against either normal mode (`python main.py` / `python -m ui`) or isolated demo mode (`python scripts/demo.py` / `python scripts/demo.py --terminal`). In demo mode, all stores, logs, and browser profiles are redirected under `demo_home/`, leaving the real repository environment clean and untouched.

---

## 1. Summary of Test Sections

| Area | Version | Focus | Primary Interface |
| :--- | :--- | :--- | :--- |
| **Voice in the Window** | v2.5 | Click-to-talk, speech synthesis, mic gating, dialog security | Desktop GUI |
| **Reminders & Timers** | v2.6 | Timeparse, scheduling, toasts, focus isolation, snooze, persistence | Desktop GUI & CLI |
| **Tool Routing** | v2.7 | Narrowed tool offering, probe execution, miss recovery, trace logging | Scripts & Evals |
| **Controlled Browser** | v2.3 | 6-step interactive workflow (search, click, read, scroll, media, history) | Desktop GUI / CLI |
| **Real Offline Mode** | v2.1 | Wi-Fi off, in-process network guard, local Ollama execution | Terminal CLI & GUI |
| **Environment Doctor** | v2.8 | Read-only configuration, hardware, dependency, and security checks | Terminal CLI & GUI |
| **Settings Panel & CLI** | v2.9 | Whitelisted settings, .env.bak backup, environment overrides, CRLF | Desktop GUI & CLI |
| **Named Routines** | v2.10 | Save approval, preview gate on run, limits, store, UI tab, isolation | Desktop GUI & CLI |
| **Demo Mode & Isolation** | v2.11 | Isolated storage, synthetic seeding, marker reset, settings protection | Scripts, Desktop GUI & CLI |
| **Sci-Fi HUD & Dock** | v2.13 | 3D wireframe sphere, particle rings, dock icons, themes, quality governor | Desktop GUI |
| **Voice in Noisy Rooms** | v2.14 | Noise filtering, AGC, spectral gate, vocab prompt, aliases, review gate | Desktop GUI & Evals |

---

## 2. Live Test Checklist Matrix

| # | Component / Feature | Command / User Action | Expected Result | Result | Notes |
| :-: | :--- | :--- | :--- | :-: | :--- |
| **V1** | **v2.5 Voice: Click-to-Talk** | Click `Mic` button or press `Ctrl+M` in the GUI. Speak: *"What time is it?"* Click `Mic` again to stop. | Status bar shows `Listening...` with a dynamic green RMS level bar, switches to `Transcribing...`, and inputs the transcript into the prompt. | `PASS` | Microphone hardware and `faster-whisper` required. |
| **V2** | **v2.5 Voice: Heard Line** | Complete a voice transcription. | The chat display renders `Heard: "What time is it?"` in italics before the agent response begins. | `PASS` | Confirms speech was ingested through standard user input pipeline. |
| **V3** | **v2.5 Voice: Speak Replies & Stop** | Check `Speak replies`. Ask: *"Tell me a short fun fact."* While audio is speaking, click the red `Stop` button. | Subprocess TTS speaks the reply aloud (titles/code stripped). Clicking `Stop` immediately silences speech output and resets status. | `PASS` | Speech terminates without lingering worker processes. |
| **V4** | **v2.5 Voice: Dialog Immunity** | Trigger a dangerous action (e.g. *"open notepad"*). When the `ConfirmDialog` appears, say *"yes"* or *"allow"* into the microphone. | Nothing happens. The dialog does not close or approve. The Allow button remains gated by mouse click / keyboard only. | `PASS` | Security requirement: voice input can never approve actions. |
| **V5** | **v2.5 Voice: Mic Refusal When Busy** | Run a multi-step task or open a confirmation dialog, then try clicking the `Mic` button or pressing `Ctrl+M`. | Microphone is refused. Status or tooltip indicates mic is disabled while agent is busy or a modal dialog is active. | `PASS` | Prevents race conditions and unintended voice input. |
| **V6** | **v2.5 Voice: Muted / Missing Mic** | Disconnect microphone or launch with invalid audio device index. | GUI displays clear warning message/tooltip that microphone is unavailable; app does not crash or raise uncaught exceptions. | `PASS` | Graceful degradation to keyboard-only input. |
| **V7** | **v2.5 Voice: Chained Voice Command** | Click `Mic`. Say: *"Open notepad and then wait 5 seconds"*. Click `Mic` to stop. | System parses input into a plan preview (`PlanDialog`), displaying numbered steps and asking for approval. | `PASS` | Voice input cleanly delegates to `core/chains.py`. |
| **V8** | **v2.5 Voice: Privacy Telemetry** | Type `/diag` or click **System Info**. Inspect output in terminal or Activity log. | `=== VOICE ===` reports model name, device status, and telemetry. Strictly zero transcript text or audio arrays exist. | `PASS` | User audio and speech text are strictly ephemeral. |
| **R1** | **v2.6 Reminders: Set with Dialog** | In chat, enter: `"remind me in 1 minute to check tea"`. | `ConfirmDialog` appears with exact arguments: text `"check tea"`, due time. After clicking **Allow**, reminder is saved. | `PASS` | Creation requires explicit confirmation. |
| **R2** | **v2.6 Reminders: Focus Isolation** | While typing rapidly in the input field, wait for the 1-minute reminder to fire. | `ReminderToast` appears in the corner. Keyboard focus is NOT stolen; user typing in the input box continues uninterrupted. | `PASS` | `takefocus=0` prevents modal grab and typing disruption. |
| **R3** | **v2.6 Reminders: Timer & Snooze** | Enter: `"set a timer for 10 seconds"`. Approve dialog. When toast appears, click `Snooze 5 min`. | Timer fires after 10s. Clicking **Snooze 5 min** updates reminder due time to `+300s` and updates Reminders tab. | `PASS` | Up to 3 snoozes allowed per reminder. |
| **R4** | **v2.6 Reminders: List & Cancel** | Enter: `"what reminders do i have"`. Then enter: `"cancel reminder 1"`. Approve confirmation dialog. | Active reminders are listed. Cancellation dialog confirms deletion and removes it from store. | `PASS` | Also verifiable via Cancel button in Reminders tab. |
| **R5** | **v2.6 Reminders: Chain Isolation** | Enter: `"remind me in 2 minutes to stretch then pause"`. | Stays as a single reminder with text `"stretch then pause"`. Does NOT split into a chain or execute `pause`. | `PASS` | Reminders parser cleanly isolates from command chainer. |
| **R6** | **v2.6 Reminders: Missed on Restart** | Set reminder for 10 seconds ahead. Close ORBIT immediately. Wait 30 seconds. Re-open ORBIT. | On startup, a missed-while-closed notice is displayed: `[reminder] Missed while closed: <text> (due at ...)`. | `PASS` | Unfired items past due are reported on next boot. |
| **R7** | **v2.6 Reminders: Dialog Collision** | Open a confirmation dialog (e.g. *"open calc"*). Let a reminder become due while dialog is open. | Reminder does not pop a conflicting toast over the modal dialog; instead, it logs cleanly as a chat line. | `PASS` | Toast collision avoidance fallback. |
| **R8** | **v2.6 Reminders: Denied Reminder** | Enter: `"remind me in 5 minutes to submit report"`. When `ConfirmDialog` appears, click `Deny`. | No reminder is created. Store count remains unchanged; no reminder appears in Reminders tab. | `PASS` | Fail-closed security rule. |
| **T1** | **v2.7 Routing: Probe Baseline** | Run: `python scripts/intent_probe.py --phrases evals/routing/dev.json` | Runs 36 test phrases with all tools offered. Generates report at `evals/results/intent-probe-<timestamp>.md`. | `PASS` | All tools (24+) offered to provider. |
| **T2** | **v2.7 Routing: Probe with Routing** | Run: `python scripts/intent_probe.py --routing --phrases evals/routing/dev.json` | Runs 36 phrases with narrowed tool groups. Tools offered drops significantly (median ~5-8). Generates report. | `PASS` | Prompt token count reduced; action pass rate preserved. |
| **T3** | **v2.7 Routing: Test Split Gate** | Run: `python scripts/intent_probe.py --phrases evals/routing/test.json` | Immediately refuses execution with error: `Refusing to run unseen test split without --final-test flag`. | `PASS` | Prevents accidental contamination of test split. |
| **T4** | **v2.7 Routing: Final Test Lock** | Run: `python scripts/intent_probe.py --final-test --phrases evals/routing/test.json` twice in a row. | First run succeeds and writes `.ran`. Second run refuses unless `--force` is passed, marking report `RERUN: NOT UNSEEN`. | `PASS` | Enforces scientific integrity for unseen holdout. |
| **T5** | **v2.7 Routing: Miss Recovery** | Enable `TOOL_ROUTING=1`. Trigger a narrowed tool prompt where model replies *"I am unable to access files"*. | Agent detects inability phrasing, retries turn ONCE with full tool set, and records `routing_retry: 1` in trace. | `PASS` | Safety net prevents model capability clipping. |
| **B1** | **Browser 6-Step: Step 1 (Search)** | Enter: `"search wikipedia for Python"` with `BROWSER_ENABLED=1`. | Controlled Playwright browser opens, navigates to Wikipedia search, extracts and displays numbered search results. | `PASS` | Requires Playwright and Chromium backend. |
| **B2** | **Browser 6-Step: Step 2 (Click)** | Enter: `"click the first result"`. | Browser navigates to the target page, verifies URL change and DOM ready state, and updates session context. | `PASS` | Session state tracks active URL and site. |
| **B3** | **Browser 6-Step: Step 3 (Read)** | Enter: `"read the page summary"`. | Extracts visible page text, truncates to safe length, and marks agent turn as tainted. | `PASS` | Taint tracking activates on external web content. |
| **B4** | **Browser 6-Step: Step 4 (Scroll)** | Enter: `"scroll down 500 pixels"`. | Dispatches scroll delta, measures new scroll displacement, and confirms vertical page movement. | `PASS` | Controller verifies displacement delta > 0. |
| **B5** | **Browser 6-Step: Step 5 (Media)** | Navigate to an approved video page. Enter: `"pause the video"`. | Controller reads `<video>` DOM element state and pauses playback without throwing errors. | `PASS` | Restricted strictly to approved media domains. |
| **B6** | **Browser 6-Step: Step 6 (Close)** | Enter: `"close the browser"`. Enter: `"what was the last site I visited?"`. | Browser closes cleanly. Session context recalls `last_site` and `last_url` from memory. | `PASS` | Resources released; session state persists. |
| **O1** | **Offline: Wi-Fi Disconnected** | Disconnect Wi-Fi / Ethernet. Launch: `python main.py --provider ollama` or `ORBIT_OFFLINE=1 python main.py`. | Application starts immediately without hanging or attempting outbound HTTP connections. | `PASS` | In-process guard blocks external socket creation. |
| **O2** | **Offline: Local Ollama Q&A** | While offline, enter: `"What is 42 * 17?"` and `"Create a note named ideas.txt with test"`. | Local Ollama model runs locally. Calculations and sandboxed file creations succeed with zero network egress. | `PASS` | Completely private on-device execution. |
| **O3** | **Offline: Network Egress Block** | While offline, attempt an external command (e.g. Gemini provider or external API fetch). | Fails immediately with `OfflineModeError` without hanging on DNS timeouts. | `PASS` | Fail-closed network isolation verified. |
| **D1** | **v2.8 Doctor: Merged `.env` Line** | Add a merged line `KEY=OTHER_KEY=val` to `.env`. Run: `python scripts/doctor.py` | Detects bad line, flags line number and key name ONLY (`KEY`), reports `FAIL` with fix suggestion. | `PASS` | No values or other keys exposed. |
| **D2** | **v2.8 Doctor: Stale ORBIT_MOCK** | Set `ORBIT_MOCK=1` in `.env` or process environment. Run `python scripts/doctor.py` | Flags `ORBIT_MOCK` present as `WARN` explaining mock mode is forced, recommending unsetting it for real models. | `PASS` | Warns developers against unintended mock mode. |
| **D3** | **v2.8 Doctor: Missing Package** | In an environment lacking an optional package (e.g. `playwright`), run `python scripts/doctor.py` | Detects absent package via `find_spec`, reports `WARN` with feature name and exact install command (`pip install ...`). | `PASS` | Pure inspect without import. |
| **D4** | **v2.8 Doctor: Ollama Stopped** | Stop Ollama server. Run `python scripts/doctor.py` | Reports Ollama unreachable as `FAIL` with status `FAIL` and suggested command `ollama serve`. | `PASS` | Fast 2s timeout, never hangs. |
| **D5** | **v2.8 Doctor: Secret Redaction** | Set a fake secret `MY_API_KEY=supersecret123` in `.env`. Run `python scripts/doctor.py` and inspect output. | Report contains strictly `"set"` or `"not set"` for keys containing `KEY`, `TOKEN`, `SECRET`, or `PASSWORD`. The secret value `supersecret123` never appears anywhere in text or JSON. | `PASS` | Zero secret leakage in reports or diag. |
| **S1** | **v2.9 Settings: Single-Line In-Place Diff** | In terminal, run `/set SPEAK_REPLIES 1` (or change setting in GUI). Confirm `y`. Inspect `git diff .env`. | The `.env` diff shows strictly that single line modified; all comments, blank lines, and other settings retain exact positions. `.env.bak` exists and matches previous `.env`. | `PASS` | In-place update preserving comments and unknown keys. |
| **S2** | **v2.9 Settings: Stale ORBIT_MOCK Source** | Set `ORBIT_MOCK=1` in the terminal environment. Run `/settings` or open Settings window. | `ORBIT_MOCK` is displayed with effective value `1` and source `environment`. | `PASS` | Origin source tracking flags process environment overrides. |
| **S3** | **v2.9 Settings: Environment Override Flagged** | Define `OLLAMA_NUM_CTX=4096` in `.env`, but set `$env:OLLAMA_NUM_CTX="16384"` in terminal before launching. | In `/settings`, source shows `environment (overridden)`. In Settings GUI, row displays orange note `⚠ overridden by environment`. | `PASS` | Prevents confusion when terminal overrides `.env`. |
| **S4** | **v2.9 Settings: Merged-Line Refusal** | Introduce a merged line (e.g. `LLM_PROVIDER=OLLAMA_MODEL=qwen3:8b`) in `.env`. Attempt `/set LLM_PROVIDER ollama`. | Refuses to write with clear error naming line number and key name only (`Refusing to write: merged setting value detected for key 'LLM_PROVIDER' (line ...)`). Original `.env` is unchanged. | `PASS` | Fail-closed syntax protection against file corruption. |
| **S5** | **v2.9 Settings: CRLF Preservation** | Save a setting in a `.env` file formatted with Windows CRLF (`\r\n`) line endings. | After saving, the resulting `.env` file retains CRLF line endings throughout with no conversion to LF. | `PASS` | Byte-level line ending preservation. |
| **RT1** | **v2.10 Routines: Create with Approval** | Enter: `"create a routine morning: open notepad then open calculator"`. | `PlanDialog` (or CLI prompt) appears with title `"Save Routine"`, heading `"Save routine 'morning' with these steps?"`, numbered steps, and `"Save"` button (800ms cooldown). Clicking Save persists routine to `data/routines.db`. | `PASS` | Save requires upfront user approval. |
| **RT2** | **v2.10 Routines: Save Last Plan** | Execute a multi-step chain (e.g. `"open notepad then wait 2 seconds"`). Then enter: `"save that as a routine dev"`. | Confirmation dialog appears for saving the last plan. Approving saves the steps under name `"dev"`. | `PASS` | Reuses existing plan steps cleanly. |
| **RT3** | **v2.10 Routines: Run with Preview** | Enter: `"run routine morning"`. | Upfront preview dialog displays header `"Routine 'morning' (2 steps):"`, lists steps with `(asks for approval)` annotations, and `"Run"` button. Runs through existing plan runner; per-step confirmations apply. | `PASS` | Reuses existing plan runner with full gate. |
| **RT4** | **v2.10 Routines: Stop & Cancellation** | Run a multi-step routine. While running or during a wait step, click the red `Stop` button. | Plan runner immediately halts after current step, cancels pending steps, and reports cancellation without corrupting routine state. | `PASS` | Sub-100ms cancellation slice. |
| **RT5** | **v2.10 Routines: Desktop Routines Tab** | Open the **Routines** tab in the desktop GUI. | Displays table with `Name`, `Steps`, `Last Run`, `Runs`. Clicking **Run** submits `run routine <name>`. Clicking **Show steps** displays formatted steps. Clicking **Delete** prompts confirmation messagebox before deleting. | `PASS` | Full GUI management interface. |
| **RT6** | **v2.10 Routines: Stale Allowlist Safety** | Remove an app from `ALLOWED_APPS` after saving a routine that uses it. Enter: `"run routine <name>"`. | Pre-execution re-validation detects stale app and refuses before step 1 (`no longer in the allowed list`). Zero steps execute. | `PASS` | Fails closed before any step runs. |
| **RT7** | **v2.10 Routines: Model & Safety Isolation** | Save and run routines. Inspect LLM call logs, prompts, memory, and `/diag`. | Model is never invoked during routine operations; routine names/steps never appear in prompt context or memory; `/diag` `=== ROUTINES ===` outputs count only. | `PASS` | Strict isolation from model and memory. |
| **DM1** | **v2.11 Demo: Window Title & Status Chip** | Launch: `python scripts/demo.py` | GUI window title displays `ORBIT v2.11.0 Desktop Assistant - DEMO MODE (sample data)`; status bar displays orange `DEMO` badge. | `PASS` | Visual demo mode indicator verified. |
| **DM2** | **v2.11 Demo: Storage Isolation** | In demo mode, create a file `"notes/demo_test.txt"`. Inspect `workspace/` and `demo_home/workspace/`. | File is created strictly in `demo_home/workspace/notes/demo_test.txt`; real repository `workspace/` remains clean and empty. | `PASS` | Filesystem storage isolation verified. |
| **DM3** | **v2.11 Demo: Settings Protection** | In demo mode, enter `/set SPEAK_REPLIES 1` or open Settings GUI and click Save. | Rejection message appears: `"Settings can't be changed in demo mode"`. Neither `.env` nor `.env.bak` is written. | `PASS` | Config protection in demo mode verified. |
| **DM4** | **v2.11 Demo: Marker Reset Safety** | Create a non-empty directory without `.orbit_demo` marker. Pass `--home <dir>` to `scripts/demo.py`. | Execution stops with clear error and deletes nothing (`does not contain the '.orbit_demo' marker. Refusing to delete`). | `PASS` | Safe reset protection verified. |
| **DM5** | **v2.11 Demo: Path Invariance Unset** | Launch ORBIT without ORBIT_HOME. Query paths via config.paths. | All storage and log paths resolve to standard repository defaults (workspace/, data/, logs/); no redirection occurs. | PASS | Baseline path invariance verified. |
| **DM6** | **v2.11 Demo: Browser Profile Isolation** | Launch python scripts/demo.py. Open browser via tool (search wikipedia for Python). | Playwright uses clean demo_home/data/browser_profile/; personal cookies and active sessions from default profile are absent. | PASS | Profile sandbox isolation verified. |
| **HUD1** | **v2.13 HUD: 3D Wireframe Sphere & Math** | Launch `python -m ui` (or preview with `scripts/hud_preview.py`). | 3D rotating wireframe sphere (12 lat, 24 lon circles), 3 tilted orbit rings, and concentric boundary ticks render smoothly; 3D rotation strictly preserves Euclidean vector lengths. | `PASS` | Pure math verified in `test_hud.py`. |
| **HUD2** | **v2.13 HUD: State Priority Hierarchy** | Trigger confirming dialog, error, speaking, listening, and busy states. | HUD resolves states strictly: Confirming (amber hold) > Error (red warning with 1.5s decay) > Speaking (3 Hz pulse) > Listening (mic-responsive pulse) > Thinking (busy spin) > Idle (gentle breathing). | `PASS` | Priority precedence enforced across all permutations. |
| **HUD3** | **v2.13 HUD: Quality Governor & Dynamic Tiers** | Benchmark or record frame times exceeding 25ms threshold over 20 frames. | Quality governor automatically downgrades fidelity tier (`high` [400 particles] -> `medium` [200] -> `low` [100, no glow] -> `static`); respects 30-second hysteresis cooldown. | `PASS` | Adaptive load shedding verified. |
| **HUD4** | **v2.13 HUD: Theme Contrast Verification** | Switch themes (`cyan`, `gold`, `mono`) via settings or preview tool. | All text (`text`) and secondary text (`text_dim`) colors achieve WCAG 2.1 relative contrast ratios >= 4.5:1 against both `bg` and `panel` surfaces across all three palettes. | `PASS` | Verified with WCAG luminance calculations. |
| **HUD5** | **v2.13 HUD: Canvas Item Pooling & Resource Conservation** | Run HUD animation and inspect Canvas item IDs. | All canvas visual elements (lines, arcs, ovals, particles) are pre-allocated once during initialization. No canvas items are created or deleted per frame; only coords and colors are updated when changed. | `PASS` | Zero memory leak or item thrashing. |
| **HUD6** | **v2.13 HUD: Bottom Dock Navigation & Shortcuts** | Use Left/Right arrow keys, Space/Enter, and `Ctrl+1`..`5` on bottom dock. | Focus moves visibly with distinct focus ring. Space/Enter or Ctrl+1..5 triggers Activity, Routines, Mic, Settings, and Doctor; center mic icon dynamically pulses with live audio level ring. | `PASS` | Keyboard accessibility verified. |
| **HUD7** | **v2.13 HUD: Dialog Topmost & Voice Immunity** | Trigger sensitive action requiring approval while HUD is animating. | `ConfirmDialog` and `PlanDialog` maintain `-topmost True`, modal grabbing, and auto-deny timer above the animated canvas; voice input is strictly refused and cannot confirm dialogs. | `PASS` | Human-in-the-loop security boundary intact. |
| **HUD8** | **v2.13 HUD: Graceful Fallback on Canvas Exception** | Simulate a canvas or HUD initialization exception under `UI_STYLE="hud"`. | Application catches the error, logs it to diagnostics (`LAST_HUD_ERROR`), prints a one-line system notice in chat, and safely falls back to the classic panels layout without crashing. | `PASS` | Fail-safe UI resilience verified. |
| **VE1** | **v2.14 Voice: DSP Enhancement Pipeline** | Feed synthetic audio with steady 50 Hz hum and low volume through `enhance()`. | Highpass removes <80Hz hum, AGC normalizes RMS to 0.08 target without clipping (peak < 0.98), and spectral noise gate suppresses quiet background floors without throwing exceptions. | `PASS` | Pure numpy DSP verified in `test_voice_enhance.py`. |
| **VE2** | **v2.14 Voice: Vocabulary Prompt & Beam Size** | Call `WhisperTranscriber.transcribe()` with `STT_PROMPT=1` and `STT_BEAM=5`. | Passes vocabulary prompt (bounded <= 200 chars) to model `hotwords` or `initial_prompt` and sets beam size to 5; default settings reproduce prior version transcribe kwargs exactly. | `PASS` | Verified in `test_voice_eval.py`. |
| **VE3** | **v2.14 Voice: Deterministic Alias Resolution** | Pass multi-token split phrases (e.g. *"open note pad"*, *"open you tube"*) to `voice.aliases.resolve()`. | Resolves to single parsed candidate (`"open notepad"`, `"open youtube"`) when unique; zero matches or ambiguous multi-matches leave text completely untouched. | `PASS` | Verified in `test_voice_aliases.py`. |
| **VE4** | **v2.14 Voice: Review Step & Source Tracking** | Transcribe noisy speech with SNR < 10 dB, avg logprob < -1.0, or single-word command under `VOICE_REVIEW="auto"`. | Transcription halts auto-submission, displays review warning banner (`"Noisy room: a headset helps"` or `"Low confidence"`), and populates input box; pressing Enter submits with `source="voice-reviewed"`. | `PASS` | Verified in `test_voice_review.py`. |
| **VE5** | **v2.14 Voice: Dev/Test Split Isolation & Final Test Lock** | Attempt configuration sweep on test split, and run `--final-test` on candidate config. | Test sweep is strictly refused; final test requires `--final-test`, records `.ran-<config>` marker on first run, and blocks second run unless `--force` is passed (marking report `RERUN: NOT UNSEEN`). | `PASS` | Verified in `test_voice_eval.py`. |
| **VE6** | **v2.14 Voice: Scientific Acceptance Verdict** | Run `python -m evals --voice-verdict` across baseline and candidate test reports. | Requires all 5 pre-registered acceptance criteria simultaneously (noisy gain >= +6.0 pts, wrong rate increase <= +1.0 pt, clean drop <= 1/24 phrases, latency <= 5.0s, real noise preserved); reports explicit metrics and score diffs. | `PASS` | Verified in `test_voice_eval.py`. |

---

## 3. Execution Instructions

### A. Running the Intent & Routing Probe

To evaluate routing performance and compare baseline vs routed tool offerings:

```powershell
# 1. Baseline run (all tools offered)
python scripts/intent_probe.py --phrases evals/routing/dev.json

# 2. Routed run (narrowed tool groups offered)
python scripts/intent_probe.py --routing --phrases evals/routing/dev.json

# 3. Check reports generated in evals/results/
ls evals/results/intent-probe-*.md
```

### B. Running Controlled Browser Verification

Ensure browser dependencies are installed and test with the 6-step workflow:

```powershell
$env:BROWSER_ENABLED="1"
python main.py
```

### C. Running Offline Verification

1. Disconnect network adapter (disable Wi-Fi or unplug Ethernet cable).
2. Start Ollama locally: `ollama run qwen3:8b`
3. Launch ORBIT in offline mode:
```powershell
$env:ORBIT_OFFLINE="1"
$env:LLM_PROVIDER="ollama"
python main.py
```

### D. Running Environment Doctor Verification

1. Run the command-line doctor:
```powershell
python scripts/doctor.py
# Or JSON output:
python scripts/doctor.py --json
```
2. Test interactive chat doctor:
Launch `python main.py` and run `/doctor`.
3. Test desktop GUI doctor:
Launch `python -m ui`, click the **Doctor** button or the status bar chip.

### E. Running Demo Mode Verification

1. Launch demo GUI:
```powershell
python scripts/demo.py
```
2. Launch demo terminal:
```powershell
python scripts/demo.py --terminal
```
3. Launch offline demo:
```powershell
python scripts/demo.py --offline
```

### F. Running HUD Preview & Benchmark

1. Launch standalone interactive preview:
```powershell
python scripts/hud_preview.py
```
2. Run automated performance benchmark across tiers:
```powershell
python scripts/hud_bench.py
```
### G. Running Voice Evaluation & Measurement Kit

1. Record benchmark phrases (resumable, requires microphone hardware):
```powershell
# Record 24 clean development phrases:
python scripts/voice_record.py --split dev --kind clean

# Record 12 real background noise development phrases:
python scripts/voice_record.py --split dev --kind real_noise
```

2. Run development configuration sweep across noise conditions:
```powershell
python -m evals --voice dev --sweep
```

3. Run candidate configuration on unseen test split (scientific final test):
```powershell
python -m evals --voice test --config combined --final-test
```

4. Evaluate pre-registered 5-condition acceptance verdict:
```powershell
python -m evals --voice-verdict
```
