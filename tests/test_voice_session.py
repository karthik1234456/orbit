"""Tests for voice/session.py: VoiceSession orchestration, audio handling, security parity, and TTS integration."""

import numpy as np
import pytest

from core.agent import Agent
from core.llm import LLMProvider, LLMResponse, ToolCall
from security.permissions import PermissionLevel, PermissionPolicy
from tools.registry import Tool, ToolRegistry
from voice.audio import AudioError
from voice.session import VoiceSession
from voice.stt import FakeTranscriber, Transcriber, TranscriptionError
from voice.tts import Speaker, SpeechError


def make_speech_audio() -> np.ndarray:
    """Generate 1.0 second of synthetic non-silent audio."""
    t = np.linspace(0, 1, 16000, endpoint=False, dtype=np.float32)
    return (0.4 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)


class FakeRecorder:
    def __init__(self, audio: np.ndarray):
        self.audio = audio
        self.calls = 0

    def record(self, stop_event=None) -> np.ndarray:
        self.calls += 1
        return self.audio


class FakeSpeaker(Speaker):
    def __init__(self):
        self.spoken: list[str] = []
        self.stop_calls = 0

    def speak(self, text: str, blocking: bool = True) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stop_calls += 1

    def is_speaking(self) -> bool:
        return False


class FakeAgent:
    def __init__(self, reply: str = "I can help with that."):
        self.reply = reply
        self.chats: list[str] = []

    def chat(self, text: str) -> str:
        self.chats.append(text)
        return self.reply


def test_normal_turn_transcribes_chats_and_speaks():
    """A standard turn records audio, transcribes, calls agent.chat with transcript, and speaks response."""
    recorder = FakeRecorder(make_speech_audio())
    transcriber = FakeTranscriber(scripted=["what is the weather"])
    speaker = FakeSpeaker()
    agent = FakeAgent(reply="It is sunny.")

    session = VoiceSession(
        agent=agent,
        recorder=recorder,
        transcriber=transcriber,
        speaker=speaker,
        speak_enabled=True,
    )

    reply = session.turn()
    assert reply == "It is sunny."
    assert agent.chats == ["what is the weather"]
    assert speaker.spoken == ["It is sunny."]


def test_silent_audio_returns_none_and_never_calls_agent():
    """When recorded audio is pure silence, session returns None and never calls agent."""
    zeros = np.zeros(16000, dtype=np.float32)
    recorder = FakeRecorder(zeros)
    transcriber = FakeTranscriber(scripted=["unreachable"])
    speaker = FakeSpeaker()
    agent = FakeAgent()
    status_msgs: list[str] = []

    session = VoiceSession(
        agent=agent,
        recorder=recorder,
        transcriber=transcriber,
        speaker=speaker,
        on_status=status_msgs.append,
    )

    reply = session.turn()
    assert reply is None
    assert len(agent.chats) == 0
    assert any("didn't hear anything" in msg.lower() for msg in status_msgs)


def test_empty_transcript_never_calls_agent():
    """When transcriber returns empty string (or filtered noise), agent is never called."""
    recorder = FakeRecorder(make_speech_audio())
    transcriber = FakeTranscriber(scripted=[""])
    speaker = FakeSpeaker()
    agent = FakeAgent()

    session = VoiceSession(
        agent=agent,
        recorder=recorder,
        transcriber=transcriber,
        speaker=speaker,
    )

    reply = session.turn()
    assert reply is None
    assert len(agent.chats) == 0


def test_speaker_stop_called_before_recording():
    """speaker.stop() must be invoked before recorder.record() to prevent acoustic feedback/self-listening."""
    call_order: list[str] = []

    class OrderedSpeaker(Speaker):
        def stop(self) -> None:
            call_order.append("speaker.stop")

        def speak(self, text: str, blocking: bool = True) -> None:
            pass

        def is_speaking(self) -> bool:
            return False

    class OrderedRecorder:
        def record(self, stop_event=None) -> np.ndarray:
            call_order.append("recorder.record")
            return make_speech_audio()

    session = VoiceSession(
        agent=FakeAgent(),
        recorder=OrderedRecorder(),
        transcriber=FakeTranscriber(scripted=["test order"]),
        speaker=OrderedSpeaker(),
    )

    session.turn()
    assert len(call_order) >= 2
    assert call_order[0] == "speaker.stop"
    assert call_order[1] == "recorder.record"


def test_transcription_error_reported_and_does_not_crash():
    """TranscriptionError is caught and reported to on_status without crashing the session."""
    class FailingTranscriber(Transcriber):
        def transcribe(self, audio: np.ndarray, sample_rate: int = 16000):
            raise TranscriptionError("GPU memory full")

    status_msgs: list[str] = []
    session = VoiceSession(
        agent=FakeAgent(),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FailingTranscriber(),
        speaker=FakeSpeaker(),
        on_status=status_msgs.append,
    )

    reply = session.turn()
    assert reply is None
    assert any("GPU memory full" in msg for msg in status_msgs)


def test_audio_error_reported_and_does_not_crash():
    """AudioError is caught and reported to on_status without crashing the session."""
    class FailingRecorder:
        def record(self, stop_event=None):
            raise AudioError("Microphone unplugged")

    status_msgs: list[str] = []
    session = VoiceSession(
        agent=FakeAgent(),
        recorder=FailingRecorder(),
        transcriber=FakeTranscriber(scripted=["test"]),
        speaker=FakeSpeaker(),
        on_status=status_msgs.append,
    )

    reply = session.turn()
    assert reply is None
    assert any("Microphone unplugged" in msg for msg in status_msgs)


def test_speech_error_still_returns_text_reply():
    """A failure in text-to-speech still preserves and returns the agent's text response."""
    class FailingSpeaker(Speaker):
        def speak(self, text: str, blocking: bool = True) -> None:
            raise SpeechError("Audio driver crash")

        def stop(self) -> None:
            pass

        def is_speaking(self) -> bool:
            return False

    status_msgs: list[str] = []
    session = VoiceSession(
        agent=FakeAgent(reply="Important response text"),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["tell me something"]),
        speaker=FailingSpeaker(),
        on_status=status_msgs.append,
    )

    reply = session.turn()
    assert reply == "Important response text"
    assert any("Speech error" in msg for msg in status_msgs)


def test_speak_enabled_property_toggles_speech():
    """speak_enabled=False prevents speak() calls, and toggling it enables speech."""
    speaker = FakeSpeaker()
    session = VoiceSession(
        agent=FakeAgent(reply="Response"),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["turn 1", "turn 2"]),
        speaker=speaker,
        speak_enabled=False,
    )

    # First turn: speak is disabled
    reply1 = session.turn()
    assert reply1 == "Response"
    assert len(speaker.spoken) == 0

    # Toggle speak_enabled on
    session.speak_enabled = True
    reply2 = session.turn()
    assert reply2 == "Response"
    assert len(speaker.spoken) == 1
    assert speaker.spoken[0] == "Response"


def test_agent_receives_exact_transcript_text():
    """The agent receives the exact transcribed text string without unexpected alteration."""
    raw_prompt = "delete temporary file C:\\workspace\\temp.log and verify"
    agent = FakeAgent()

    session = VoiceSession(
        agent=agent,
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=[raw_prompt]),
        speaker=FakeSpeaker(),
    )

    session.turn()
    assert len(agent.chats) == 1
    assert agent.chats[0] == raw_prompt


def test_real_agent_confirm_callback_still_used_with_voice():
    """With a real Agent and a CONFIRM tool, confirm_callback is still invoked (voice never auto-approves)."""
    class ScriptedProvider(LLMProvider):
        def __init__(self):
            self.turn = 0

        def generate(self, messages, tools=None):
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    tool_calls=[ToolCall(name="write_critical_file", args={"path": "config.json"})]
                )
            return LLMResponse(text="Handled.")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="write_critical_file",
            description="Write a critical system file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            func=lambda path: f"Wrote {path}",
            permission=PermissionLevel.CONFIRM,
        )
    )

    confirm_prompts: list[tuple[str, dict]] = []

    def confirm_cb(tool_name: str, args: dict, warning: str = None) -> bool:
        confirm_prompts.append((tool_name, args))
        # User explicitly denies at keyboard
        return False

    real_agent = Agent(
        provider=ScriptedProvider(),
        registry=registry,
        policy=PermissionPolicy(),
        confirm_callback=confirm_cb,
    )

    session = VoiceSession(
        agent=real_agent,
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["please write config.json"]),
        speaker=FakeSpeaker(),
    )

    session.turn()

    # Verify confirmation callback was executed
    assert len(confirm_prompts) == 1
    assert confirm_prompts[0][0] == "write_critical_file"

    # Verify tool was blocked because keyboard confirmed False
    tool_msgs = [m for m in real_agent.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "user denied permission" in tool_msgs[0]["content"]


def test_first_sentence_fed_to_speaker_before_agent_returns():
    """Streaming feeds the first completed sentence chunk to the speaker before agent.chat finishes."""
    events = []

    class StreamingSpeaker(Speaker):
        def __init__(self):
            self.chunks = []

        def speak(self, text: str, blocking: bool = True):
            pass

        def stop(self):
            pass

        def is_speaking(self):
            return False

        def begin_stream(self):
            events.append(("speaker", "begin_stream"))

        def feed(self, text: str):
            events.append(("speaker", f"feed:{text}"))
            self.chunks.append(text)

        def end_stream(self, wait: bool = True):
            events.append(("speaker", "end_stream"))

    class StreamingAgent:
        def chat(self, text: str, on_delta=None, cancel_event=None):
            events.append(("agent", "start"))
            if on_delta:
                on_delta("This is the first sentence. And this is the second sentence.")
            events.append(("agent", "end"))
            return "This is the first sentence. And this is the second sentence."

    speaker = StreamingSpeaker()
    agent = StreamingAgent()
    session = VoiceSession(
        agent=agent,
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["test"]),
        speaker=speaker,
        speak_enabled=True,
    )
    reply = session.respond("test")
    assert reply == "This is the first sentence. And this is the second sentence."
    # Assert feed happened before agent end
    agent_end_idx = events.index(("agent", "end"))
    first_feed_idx = events.index(("speaker", "feed:This is the first sentence."))
    assert first_feed_idx < agent_end_idx


def test_interrupt_mid_stream_calls_speaker_stop_and_no_further_feed():
    """An interrupt event triggered mid-stream calls speaker.stop() and suppresses subsequent feeds."""
    feed_calls = []
    stop_calls = []

    class InterruptSpeaker(Speaker):
        def speak(self, text: str, blocking: bool = True):
            pass

        def stop(self):
            stop_calls.append(True)

        def is_speaking(self):
            return False

        def feed(self, text: str):
            feed_calls.append(text)

        def end_stream(self, wait: bool = True):
            pass

    from voice.interrupt import InterruptMonitor
    monitor = InterruptMonitor(key_source=lambda: None)

    class InterruptingAgent:
        def chat(self, text: str, on_delta=None, cancel_event=None):
            if on_delta:
                on_delta("Sentence number one. ")
                # Trigger interrupt
                monitor.event.set()
                on_delta("Sentence number two. ")
            return "Sentence number one. Sentence number two."

    status_msgs = []
    session = VoiceSession(
        agent=InterruptingAgent(),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["test"]),
        speaker=InterruptSpeaker(),
        on_status=status_msgs.append,
        monitor=monitor,
        speak_enabled=True,
    )
    session.respond("test")
    assert len(stop_calls) > 0
    assert any("(interrupted)" in msg for msg in status_msgs)
    assert "Sentence number two." not in feed_calls
    monitor.close()


def test_provider_sends_no_deltas_falls_back_to_speak():
    """When the provider/agent does not emit deltas, VoiceSession falls back to speaker.speak(reply)."""
    class NonStreamingAgent:
        def chat(self, text: str, on_delta=None, cancel_event=None):
            return "Non-streamed full reply."

    speaker = FakeSpeaker()
    session = VoiceSession(
        agent=NonStreamingAgent(),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["test"]),
        speaker=speaker,
        speak_enabled=True,
    )
    reply = session.respond("test")
    assert reply == "Non-streamed full reply."
    assert speaker.spoken == ["Non-streamed full reply."]


def test_monitor_armed_during_respond_and_disarmed_afterwards_even_when_agent_raises():
    """The InterruptMonitor is armed during respond() and disarmed afterwards even if agent.chat raises."""
    from voice.interrupt import InterruptMonitor
    monitor = InterruptMonitor(key_source=lambda: None)

    class CrashingAgent:
        def chat(self, text: str, on_delta=None, cancel_event=None):
            assert monitor._armed is True
            raise RuntimeError("Agent crashed unexpected")

    speaker = FakeSpeaker()
    session = VoiceSession(
        agent=CrashingAgent(),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["test"]),
        speaker=speaker,
        monitor=monitor,
        speak_enabled=True,
    )

    assert monitor._armed is False
    with pytest.raises(RuntimeError, match="Agent crashed unexpected"):
        session.respond("test")

    assert monitor._armed is False
    monitor.close()


def test_latency_keys_are_populated():
    """Latency metrics dictionary is populated after speech turns and streaming response."""
    class StreamingAgent:
        def chat(self, text: str, on_delta=None, cancel_event=None):
            if on_delta:
                on_delta("Hello world. More content.")
            return "Hello world. More content."

    session = VoiceSession(
        agent=StreamingAgent(),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["test voice prompt"]),
        speaker=FakeSpeaker(),
        speak_enabled=True,
    )

    session.turn()
    lat = session.last_latency
    assert "speech_end_to_transcript" in lat
    assert "transcript_to_first_delta" in lat
    assert "first_delta_to_first_audio" in lat
    assert "total" in lat
    assert all(isinstance(v, (int, float)) and v >= 0 for v in lat.values())


def test_real_agent_confirm_callback_never_auto_approves_and_monitor_is_paused():
    """Voice input never bypasses confirmations; confirm_callback is called and monitor is paused during prompt."""
    from voice.interrupt import InterruptMonitor
    monitor = InterruptMonitor(key_source=lambda: None)

    class ScriptedProvider(LLMProvider):
        def __init__(self):
            self.turn = 0

        def generate(self, messages, tools=None, on_delta=None, should_stop=None):
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    tool_calls=[ToolCall(name="delete_account", args={"id": "user123"})]
                )
            return LLMResponse(text="Operation halted.")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="delete_account",
            description="Delete account tool",
            parameters={"type": "object", "properties": {"id": {"type": "string"}}},
            func=lambda id: f"Deleted {id}",
            permission=PermissionLevel.CONFIRM,
        )
    )

    paused_states = []

    def confirm_cb(tool_name: str, args: dict, warning: str = None) -> bool:
        with monitor.paused():
            paused_states.append(monitor._paused)
            return False

    agent = Agent(
        provider=ScriptedProvider(),
        registry=registry,
        policy=PermissionPolicy(),
        confirm_callback=confirm_cb,
    )

    session = VoiceSession(
        agent=agent,
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(scripted=["delete account user123"]),
        speaker=FakeSpeaker(),
        monitor=monitor,
        speak_enabled=True,
    )

    _reply = session.turn()
    assert len(paused_states) == 1
    assert paused_states[0] is True
    assert monitor._paused is False

    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "user denied permission" in tool_msgs[0]["content"]
    monitor.close()


def test_is_speaking_delegates_to_speaker():
    """session.is_speaking() delegates to speaker.is_speaking()."""
    speaker = FakeSpeaker()
    session = VoiceSession(
        agent=FakeAgent(),
        recorder=FakeRecorder(make_speech_audio()),
        transcriber=FakeTranscriber(),
        speaker=speaker,
    )
    assert session.is_speaking() is False

