import os
import sys
import threading
import time
from typing import Any, Optional

from config.settings import (
    MAX_RECORD_SECONDS,
    MIC_DEVICE,
    RAG_INCLUDE_FLAGGED,
    RAG_MAX_CHARS,
    RAG_TOP_K,
    SILENCE_SECONDS,
    STT_LANGUAGE,
    STT_MODEL,
    TTS_RATE,
    VAD_THRESHOLD,
    WAKE_COOLDOWN_SECONDS,
    WAKE_LISTEN_TIMEOUT,
    WAKE_PHRASE,
    WAKE_STT_MODEL,
)
from config import settings
from core.bootstrap import build_app, default_system_prompt
from core.llm import LLMError, MockProvider
from core.offline import enable_network_guard
from core.providers import make_provider
from core.version import VERSION

SYSTEM_PROMPT = default_system_prompt()
STARTUP_MESSAGE = f"ORBIT v{VERSION} started with tool support and security controls. Type 'quit' to exit."

from core.planner import PlanError, format_plan, make_plan
from rag.safety import build_docs_block
from voice.audio import Recorder, list_input_devices
from voice.interrupt import InterruptMonitor
from voice.session import VoiceSession
from voice.stt import FakeTranscriber, WhisperTranscriber
from voice.tts import NullSpeaker, SubprocessSpeaker
from voice.vad import UtteranceSegmenter, capture_utterance
from voice.wake import WakeDetector



def format_startup_banner(
    label: str, tool_count: int, memory_count: int, notice: Optional[str] = None
) -> str:
    """Format the startup banner line and notice according to ORBIT specification."""
    lines = [f"ORBIT | provider: {label} | tools: {tool_count} | memory: {memory_count} items"]
    if label == "MOCK":
        reason = notice if notice else "Running in mock mode."
        if not reason.startswith("*** MOCK MODE:"):
            lines.append(f"*** MOCK MODE: replies are NOT from a real model. {reason} ***")
        else:
            lines.append(reason)
    elif notice:
        lines.append(notice)
    return "\n".join(lines)


def get_status_info(provider_label: str) -> dict[str, Any]:
    """Gather diagnostic status info about provider, models, and Ollama reachability."""
    from config import settings
    from core.ollama_provider import list_models, server_version
    from core.providers import _is_model_installed, is_orbit_offline

    ollama_host = getattr(settings, "OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model = getattr(settings, "OLLAMA_MODEL", "qwen3:8b")
    gemini_model = getattr(settings, "MODEL_NAME", "gemini-3.8-flash")
    offline = is_orbit_offline()

    if "LIVE" in provider_label:
        active_model = gemini_model
    elif "LOCAL" in provider_label:
        active_model = ollama_model
    elif provider_label == "MOCK":
        active_model = "mock"
    else:
        active_model = ollama_model

    version = server_version(ollama_host)
    server_reachable = version is not None

    if server_reachable:
        installed_list = list_models(ollama_host)
        model_installed = _is_model_installed(installed_list, ollama_model)
    else:
        model_installed = False

    return {
        "provider_label": provider_label,
        "model": active_model,
        "ollama_host": ollama_host,
        "ollama_model": ollama_model,
        "offline": offline,
        "server_reachable": server_reachable,
        "server_version": version,
        "model_installed": model_installed,
    }


def format_status(info: dict[str, Any]) -> str:
    """Format status dictionary into human-readable text."""
    srv_status = f"Reachable (v{info['server_version']})" if info.get("server_version") else ("Reachable" if info["server_reachable"] else "Not reachable")
    model_status = "Installed / reachable" if info["model_installed"] else ("Not installed" if info["server_reachable"] else "Not reachable (server offline)")
    lines = [
        f"Provider label: {info['provider_label']}",
        f"Active model: {info['model']}",
        f"Ollama host: {info['ollama_host']}",
        f"ORBIT_OFFLINE: {'ON' if info['offline'] else 'OFF'}",
        f"Ollama server: {srv_status}",
        f"Ollama model '{info['ollama_model']}': {model_status}",
    ]
    return "\n".join(lines)


def main() -> None:
    """Terminal chat loop managing user input, permissions, tool execution, and responses."""
    speaker = None
    monitor = None
    try:
        # Enable network guard if offline mode is active
        if os.getenv("ORBIT_OFFLINE", "").strip().lower() in ("true", "1", "yes", "on") or getattr(settings, "ORBIT_OFFLINE", False):
            enable_network_guard()

        # Initialize LLM provider (supports ORBIT_MOCK=1 or missing API key for zero-cost local offline development)
        choice = make_provider()
        provider = choice.provider
        is_mock = isinstance(provider, MockProvider)

        # Build interrupt monitor
        monitor = InterruptMonitor()

        def confirm_tool(tool_name: str, args: dict, warning: Optional[str] = None) -> bool:
            """Prompt the user in the terminal to authorize actions requiring confirmation."""
            try:
                if warning:
                    print(f"WARNING: {warning}")
                prompt = f"ORBIT wants to run {tool_name} with {args}. Allow? (y/n): "
                with monitor.paused():
                    answer = input(prompt).strip().lower()
                return answer == "y"
            except (KeyboardInterrupt, EOFError):
                return False

        def print_event(event: dict) -> None:
            """Print live progress indicator when a tool execution step completes."""
            if event.get("type") == "step_end":
                idx = event.get("index", "?")
                tool = event.get("tool", "unknown")
                status = event.get("status")
                symbol = "✓" if status == "success" else "✗"
                print(f"[{idx}] {tool} {symbol}")

        # Wire the application components using the bootstrap builder
        app = build_app(
            provider=provider,
            confirm_callback=confirm_tool,
            on_event=print_event,
        )

        agent = app.agent
        registry = app.registry
        memory_store = app.memory_store
        doc_index = app.doc_index
        docs_dir = app.paths.docs

        tool_count = len(registry.list_tools())
        memory_count = len(memory_store.list_all(limit=10000)) if memory_store else 0

        # Print single clear banner line + notice/mock loud warning
        banner = format_startup_banner(
            label=choice.label,
            tool_count=tool_count,
            memory_count=memory_count,
            notice=choice.notice,
        )
        print(banner)

        if doc_index is None:
            print("Warning: Document index unavailable. Continuing without document tools.")
        elif getattr(doc_index, "migrated", False):
            print("Document index was rebuilt; run /index again.")

        # Initialize Voice components
        recorder = Recorder(device=MIC_DEVICE, max_seconds=MAX_RECORD_SECONDS)
        if is_mock:
            transcriber = FakeTranscriber(default_text="Voice mock response")
            speaker = NullSpeaker()
        else:
            transcriber = WhisperTranscriber(model_name=STT_MODEL, language=STT_LANGUAGE)
            speaker = SubprocessSpeaker(rate=TTS_RATE)

        voice_session = VoiceSession(
            agent=agent,
            recorder=recorder,
            transcriber=transcriber,
            speaker=speaker,
            on_status=print,
            speak_enabled=True,
            monitor=monitor,
        )
        speak_typed = False
    except Exception as exc:
        print(f"Initialization error: {exc}")
        sys.exit(1)

    print(STARTUP_MESSAGE)

    try:
        while True:
            try:
                user_input = input("You: ").strip()
                if not user_input:
                    continue

                if user_input.lower() == "quit":
                    print("Goodbye!")
                    break

                if user_input.lower() == "/status":
                    status_info = get_status_info(choice.label)
                    print(format_status(status_info))
                    continue

                if user_input.lower() == "/reset":
                    agent.reset()
                    print("Conversation history reset.")
                    continue

                if user_input.lower() == "/context":
                    state = getattr(agent, "session_state", None)
                    if state is None or state.is_empty():
                        print("No session context.")
                    else:
                        d = state.to_dict()
                        print(f"Site: {d.get('last_site') or 'none'}")
                        print(f"Query: {d.get('last_query') or 'none'}")
                        print(f"App: {d.get('last_app') or 'none'}")
                        print(f"Folder: {d.get('last_folder') or 'none'}")
                        print(f"URL: {d.get('last_url') or 'none'}")
                        print(f"Last tool: {d.get('last_tool') or 'none'}")
                        age_s = int(d.get("age_seconds", 0))
                        print(f"Age: {age_s}s")
                    continue

                if user_input.lower() == "/last":
                    if agent.last_run:
                        print(agent.last_run.summary())
                    else:
                        print("No recent task runs.")
                    continue

                if user_input.lower() == "/memories":
                    all_mems = memory_store.list_all(limit=50)
                    if not all_mems:
                        print("No memories stored.")
                    else:
                        for m in all_mems:
                            print(f"[{m.id}] ({m.category}) {m.content}")
                    continue

                if user_input.startswith("/forget"):
                    parts = user_input.split()
                    if len(parts) != 2 or not parts[1].isdigit():
                        print("Usage: /forget <id>")
                        continue
                    mem_id = int(parts[1])
                    try:
                        confirm = input(f"Delete memory {mem_id}? (y/n): ").strip().lower()
                        if confirm == "y":
                            deleted = memory_store.delete(mem_id)
                            if deleted:
                                print(f"Memory {mem_id} deleted.")
                            else:
                                print(f"Memory with ID {mem_id} not found.")
                        else:
                            print("Deletion cancelled.")
                    except (KeyboardInterrupt, EOFError):
                        print("\nDeletion cancelled.")
                    continue

                if user_input.startswith("/plan"):
                    task = user_input[len("/plan"):].strip()
                    if not task:
                        print("Usage: /plan <task>")
                        continue

                    try:
                        plan = make_plan(provider, task, registry.list_tools())
                        print(format_plan(plan))
                        confirm = input("Run this plan? (y/n): ").strip().lower()
                        if confirm == "y":
                            reply = agent.run_plan(plan)
                            for name, args, result in agent.last_tool_events:
                                print(f"[tool] {name}({args}) -> {result}")
                            print(f"ORBIT: {reply}")
                            if speak_typed and speaker:
                                speaker.speak(reply, blocking=False)
                        else:
                            print("Plan discarded.")
                    except PlanError as err:
                        print(f"Planning error: {err}")
                    continue

                if user_input.lower() == "/index":
                    if not doc_index:
                        print("Document indexing is unavailable (FTS5 not supported).")
                        continue
                    try:
                        confirm = input(f"Index documents in {docs_dir}? (y/n): ").strip().lower()
                        if confirm == "y":
                            idx_tool = registry.get("index_documents")
                            if idx_tool:
                                summary = idx_tool.func()
                                print(summary)
                            else:
                                print("index_documents tool not registered.")
                        else:
                            print("Indexing cancelled.")
                    except (KeyboardInterrupt, EOFError):
                        print("\nIndexing cancelled.")
                    continue

                if user_input.lower() == "/docs":
                    if not doc_index:
                        print("Document index is unavailable.")
                        continue
                    list_tool = registry.get("list_documents")
                    if list_tool:
                        print(list_tool.func())
                    else:
                        print("list_documents tool not registered.")
                    continue

                if user_input.startswith("/ask"):
                    question = user_input[len("/ask"):].strip()
                    if not question:
                        print("Usage: /ask <question>")
                        continue
                    if not doc_index:
                        print("Document index is unavailable.")
                        continue

                    hits, withheld = doc_index.search(
                        query=question,
                        top_k=RAG_TOP_K,
                        include_flagged=RAG_INCLUDE_FLAGGED,
                    )
                    docs_block = build_docs_block(hits, withheld_count=withheld, max_chars=RAG_MAX_CHARS)
                    if not docs_block:
                        print("No matching passages in your documents.")
                        continue

                    reply = agent.chat(question, extra_system_text=docs_block)
                    for name, args, result in agent.last_tool_events:
                        print(f"[tool] {name}({args}) -> {result}")
                    print(f"ORBIT: {reply}")
                    if speak_typed and speaker:
                        speaker.speak(reply, blocking=False)
                    continue

                if user_input.lower() == "/speak on":
                    voice_session.speak_enabled = True
                    speak_typed = True
                    print("Speech enabled (voice mode and typed replies will be spoken).")
                    continue

                if user_input.lower() == "/speak off":
                    voice_session.speak_enabled = False
                    speak_typed = False
                    if speaker:
                        speaker.stop()
                    print("Speech disabled.")
                    continue

                if user_input.lower() == "/shortcuts on":
                    agent.command_shortcuts = True
                    print("Command shortcuts enabled.")
                    continue

                if user_input.lower() == "/shortcuts off":
                    agent.command_shortcuts = False
                    print("Command shortcuts disabled.")
                    continue

                if user_input.lower().startswith("/shortcuts"):
                    print("Usage: /shortcuts on|off")
                    continue

                if user_input.lower() == "/mic":
                    devs = list_input_devices()
                    if not devs:
                        print("No input audio devices detected.")
                    else:
                        print("Available audio input devices:")
                        for idx, name in devs:
                            sel = " (current)" if (MIC_DEVICE is not None and (idx == MIC_DEVICE or name == MIC_DEVICE)) else ""
                            print(f"  [{idx}] {name}{sel}")
                    print(f"Current MIC_DEVICE setting: {MIC_DEVICE}")
                    continue

                if user_input.lower() == "/latency":
                    if not voice_session.last_latency:
                        print("No latency metrics recorded yet.")
                    else:
                        print("Last turn latency:")
                        for k, v in voice_session.last_latency.items():
                            print(f"  {k}: {v:.3f}s")
                    continue

                if user_input.lower() == "/listen":
                    print(
                        f"ORBIT listens locally for '{WAKE_PHRASE}'. "
                        "Audio is not saved and nothing is sent to Gemini until the phrase is heard. Ctrl+C to stop."
                    )
                    if is_mock:
                        wake_transcriber = FakeTranscriber(default_text=f"{WAKE_PHRASE} what time is it")
                    else:
                        wake_transcriber = WhisperTranscriber(model_name=WAKE_STT_MODEL, language=STT_LANGUAGE)

                    def make_segmenter():
                        return UtteranceSegmenter(threshold=VAD_THRESHOLD, silence_seconds=SILENCE_SECONDS)

                    def capture_for_wake(segmenter=None, cancel_event=None):
                        seg = segmenter if segmenter is not None else make_segmenter()
                        return capture_utterance(
                            stream_factory=None,
                            segmenter=seg,
                            cancel_event=cancel_event,
                        )

                    last_speech_time = [0.0]

                    def is_muted() -> bool:
                        if voice_session.is_speaking():
                            last_speech_time[0] = time.monotonic()
                            return True
                        if (time.monotonic() - last_speech_time[0]) < 1.0:
                            return True
                        return False

                    detector = WakeDetector(
                        transcriber=wake_transcriber,
                        make_segmenter=make_segmenter,
                        capture_fn=capture_for_wake,
                        phrase=WAKE_PHRASE,
                        cooldown_s=WAKE_COOLDOWN_SECONDS,
                        is_muted=is_muted,
                    )

                    try:
                        while True:
                            try:
                                wake_res = detector.wait_for_wake()
                                if wake_res is None:
                                    break

                                try:
                                    import winsound
                                    winsound.Beep(1000, 150)
                                except Exception:
                                    pass

                                remainder = wake_res.remainder.strip()
                                if remainder:
                                    command_text = remainder
                                else:
                                    cmd_audio = capture_utterance(
                                        stream_factory=None,
                                        segmenter=make_segmenter(),
                                        start_timeout=WAKE_LISTEN_TIMEOUT,
                                    )
                                    if cmd_audio is None or len(cmd_audio) == 0:
                                        print("Didn't catch a command")
                                        continue
                                    transcript_obj = transcriber.transcribe(cmd_audio)
                                    command_text = (
                                        transcript_obj.text.strip()
                                        if transcript_obj and transcript_obj.text
                                        else ""
                                    )

                                if not command_text:
                                    print("Didn't catch a command")
                                    continue

                                print(f'Heard: "{command_text}"')
                                reply = voice_session.respond(command_text)
                                for name, args, result in agent.last_tool_events:
                                    print(f"[tool] {name}({args}) -> {result}")
                                print(f"ORBIT: {reply}")
                            except KeyboardInterrupt:
                                raise
                            except Exception as exc:
                                print(f"Voice turn error: {exc}")
                                continue
                    except KeyboardInterrupt:
                        print("\nLeaving listen mode.")
                        if speaker:
                            speaker.stop()
                        if monitor:
                            monitor.close()
                    continue

                if user_input.lower() == "/voice":
                    print("Entering voice mode. Press Ctrl+C or type 'q' to exit.")
                    while True:
                        try:
                            print("\nPress Enter to start speaking (or type q then Enter to leave voice mode)")
                            mode_cmd = input().strip()
                            if mode_cmd.lower() in ("q", "quit"):
                                print("Leaving voice mode.")
                                if speaker:
                                    speaker.stop()
                                break

                            if speaker:
                                speaker.stop()
                            stop_event = threading.Event()
                            recorded_chunks = []

                            def record_thread():
                                try:
                                    audio = recorder.record(stop_event=stop_event)
                                    recorded_chunks.append(audio)
                                except Exception as exc:
                                    print(f"Recording error: {exc}")

                            t = threading.Thread(target=record_thread)
                            t.start()

                            print("Listening... press Enter to stop")
                            try:
                                input()
                            except (KeyboardInterrupt, EOFError):
                                pass
                            finally:
                                stop_event.set()
                                t.join()

                            if recorded_chunks and len(recorded_chunks[0]) > 0:
                                try:
                                    reply = voice_session.turn(audio=recorded_chunks[0])
                                    if reply is not None:
                                        for name, args, result in agent.last_tool_events:
                                            print(f"[tool] {name}({args}) -> {result}")
                                        print(f"ORBIT: {reply}")
                                except Exception as exc:
                                    print(f"Voice turn error: {exc}")
                            else:
                                print("No audio captured.")

                        except KeyboardInterrupt:
                            print("\nLeaving voice mode.")
                            if speaker:
                                speaker.stop()
                            break
                        except Exception as exc:
                            print(f"Voice error: {exc}")
                            continue
                    continue

                if speak_typed:
                    reply = voice_session.respond(user_input)
                else:
                    reply = agent.chat(user_input)

                # Display any tools invoked during this turn
                for name, args, result in agent.last_tool_events:
                    print(f"[tool] {name}({args}) -> {result}")

                print(f"ORBIT: {reply}")

            except LLMError as err:
                # Report provider or network errors without crashing the interactive session
                print(f"ORBIT error: {err}")
            except (KeyboardInterrupt, EOFError):
                # Handle interruption signal cleanly
                print("\nGoodbye!")
                break
    finally:
        if speaker:
            speaker.stop()
        if monitor:
            monitor.close()


if __name__ == "__main__":
    main()
