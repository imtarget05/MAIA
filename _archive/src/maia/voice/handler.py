"""Voice chat handler: audio bytes -> transcript -> agent.chat -> text (+audio).

Works with any object exposing .chat(question, session_id, employee_id,
tenant_id) returning the typed agent response dict, so tests inject a stub.
Never raises: every failure becomes an error dict.
"""
from __future__ import annotations

import base64
from typing import Any

from .providers import get_stt, get_tts


class VoiceChatHandler:
    def __init__(self, agent: Any, stt=None, tts=None, speak: bool = False):
        self.agent = agent
        self.stt = stt or get_stt()
        self.tts = tts or get_tts()
        self.speak = speak

    def handle_audio(self, audio: bytes, session_id: str = "voice_default",
                     employee_id: str | None = None, tenant_id: str | None = None,
                     language: str = "vi") -> dict:
        if not audio:
            return {"ok": False, "error": "empty_audio"}
        try:
            tr = self.stt.transcribe(audio, language=language)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not tr.get("ok"):
            return {"ok": False, "error": tr.get("error", "stt_failed"),
                    "hint": tr.get("hint", "")}
        transcript = (tr.get("text") or "").strip()
        if not transcript:
            return {"ok": False, "error": "empty_transcript"}
        try:
            response = self.agent.chat(transcript, session_id=session_id,
                                       employee_id=employee_id, tenant_id=tenant_id)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "transcript": transcript}
        out = {"ok": True, "transcript": transcript, "response": response, "audio_b64": None}
        if self.speak:
            try:
                sp = self.tts.speak(response.get("answer", ""), language=language)
                if sp.get("ok") and sp.get("audio"):
                    out["audio_b64"] = base64.b64encode(sp["audio"]).decode()
                    out["audio_mime"] = sp.get("mime", "audio/mpeg")
                else:
                    out["tts_error"] = sp.get("error", "tts_failed")
            except Exception as e:
                out["tts_error"] = f"{type(e).__name__}: {e}"
        return out
