"""Voice provider protocols (STT/TTS). No new hard dependencies.

Only the `disabled` provider ships: any real provider (OpenAI Whisper,
ElevenLabs, local whisper/fastspeech) plugs in by implementing the two
protocols and registering in PROVIDERS. Audio stays bytes in/out so the
API layer never cares which backend is active.
"""
from __future__ import annotations

from typing import Protocol

from ..config import settings


class STTProvider(Protocol):
    name: str

    def transcribe(self, audio: bytes, language: str = "vi") -> dict:
        """-> {"ok": True, "text": ...} or {"ok": False, "error": ...}."""
        ...


class TTSProvider(Protocol):
    name: str

    def speak(self, text: str, language: str = "vi") -> dict:
        """-> {"ok": True, "audio": b"...", "mime": "audio/mpeg"} or error dict."""
        ...


class DisabledSTT:
    name = "disabled"

    def transcribe(self, audio: bytes, language: str = "vi") -> dict:
        return {"ok": False, "error": "stt_disabled",
                "hint": "Set VOICE_ENABLED=true and VOICE_STT_PROVIDER to a real backend."}


class DisabledTTS:
    name = "disabled"

    def speak(self, text: str, language: str = "vi") -> dict:
        return {"ok": False, "error": "tts_disabled",
                "hint": "Set VOICE_ENABLED=true and VOICE_TTS_PROVIDER to a real backend."}


PROVIDERS: dict[str, type] = {"disabled": DisabledSTT, "disabled-tts": DisabledTTS}


def get_stt() -> STTProvider:
    """STT backend honoring VOICE_ENABLED (anything but a real backend -> disabled)."""
    if not settings.VOICE_ENABLED:
        return DisabledSTT()
    cls = PROVIDERS.get(settings.VOICE_STT_PROVIDER, DisabledSTT)
    try:
        inst = cls()
        return inst if hasattr(inst, "transcribe") else DisabledSTT()
    except Exception:
        return DisabledSTT()


def get_tts() -> TTSProvider:
    if not settings.VOICE_ENABLED:
        return DisabledTTS()
    cls = PROVIDERS.get(settings.VOICE_TTS_PROVIDER, DisabledTTS)
    try:
        inst = cls()
        return inst if hasattr(inst, "speak") else DisabledTTS()
    except Exception:
        return DisabledTTS()
