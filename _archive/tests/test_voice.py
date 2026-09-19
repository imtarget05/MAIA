"""Tests for the voice interface (fake providers + stub agent, offline)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.config import settings
from maia.voice import VoiceChatHandler, get_stt, get_tts


class FakeSTT:
    name = "fake"

    def __init__(self, text="Chính sách nghỉ phép thế nào?"):
        self.text = text

    def transcribe(self, audio: bytes, language: str = "vi") -> dict:
        assert audio
        return {"ok": True, "text": self.text}


class FakeTTS:
    name = "fake"

    def speak(self, text: str, language: str = "vi") -> dict:
        assert text
        return {"ok": True, "audio": b"FAKEAUDIO", "mime": "audio/mpeg"}


class StubAgent:
    def __init__(self):
        self.calls = []

    def chat(self, question, session_id="default", employee_id=None, tenant_id=None):
        self.calls.append(question)
        return {"answer": f"Echo: {question}", "status": "answered", "citations": []}


def test_providers_disabled_by_default():
    assert settings.VOICE_ENABLED is False
    assert get_stt().transcribe(b"x")["error"] == "stt_disabled"
    assert get_tts().speak("hi")["error"] == "tts_disabled"


def test_handler_empty_audio():
    h = VoiceChatHandler(StubAgent(), stt=FakeSTT(), tts=FakeTTS())
    assert h.handle_audio(b"")["error"] == "empty_audio"


def test_handler_roundtrip_without_speak():
    agent = StubAgent()
    h = VoiceChatHandler(agent, stt=FakeSTT(), tts=FakeTTS(), speak=False)
    out = h.handle_audio(b"\x00\x01", session_id="v1")
    assert out["ok"] is True
    assert out["transcript"] == "Chính sách nghỉ phép thế nào?"
    assert agent.calls == ["Chính sách nghỉ phép thế nào?"]
    assert out["response"]["status"] == "answered"
    assert out["audio_b64"] is None


def test_handler_roundtrip_with_speak():
    import base64
    h = VoiceChatHandler(StubAgent(), stt=FakeSTT("hi"), tts=FakeTTS(), speak=True)
    out = h.handle_audio(b"\x00\x01")
    assert out["ok"] is True
    assert base64.b64decode(out["audio_b64"]) == b"FAKEAUDIO"
    assert out["audio_mime"] == "audio/mpeg"


def test_handler_stt_failure_propagates():
    class BadSTT:
        name = "bad"

        def transcribe(self, audio: bytes, language: str = "vi") -> dict:
            return {"ok": False, "error": "no_speech"}
    agent = StubAgent()
    h = VoiceChatHandler(agent, stt=BadSTT(), tts=FakeTTS())
    out = h.handle_audio(b"\x00\x01")
    assert out["ok"] is False and out["error"] == "no_speech"
    assert agent.calls == []
