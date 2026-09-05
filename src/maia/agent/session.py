"""Session / Memory for multi-turn chat."""
from __future__ import annotations

import time
from collections import deque, defaultdict

from ..config import settings


class SessionStore:
    def __init__(self, max_turns: int | None = None):
        self.max_turns = max_turns or settings.MAX_HISTORY_TURNS
        # session_id -> deque of messages {role, content, ts, intent}
        self._store: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.max_turns * 2))

    def append(self, session_id: str, role: str, content: str, intent: str | None = None) -> None:
        self._store[session_id].append({"role": role, "content": content, "intent": intent, "ts": time.time()})

    def history(self, session_id: str) -> list[dict]:
        return list(self._store[session_id])

    def history_text(self, session_id: str, last_n: int = 4) -> str:
        hist = self.history(session_id)[-last_n:]
        if not hist:
            return ""
        return "\n".join(f"{m['role']}: {m['content'][:300]}" for m in hist)

    def rewrite_query(self, session_id: str, question: str) -> str:
        """Query rewrite using short-term context (Agentic RAG §18).
        Handles anaphora like 'còn nếu tôi nghỉ 3 ngày thì sao?' by
        prepending last user intent context.
        """
        hist = self.history(session_id)
        if not hist:
            return question
        ql = question.strip().lower()
        # short / follow-up signals
        follow_signals = ["còn nếu", "thì sao", "nó ", "cái đó", "vậy", "thế còn", "còn lại", "bao nhiêu", "thì"]
        is_followup = any(s in ql for s in follow_signals) or len(ql.split()) < 4
        if not is_followup:
            return question
        # last user question with substantive content
        last_user = next((m["content"] for m in reversed(hist) if m["role"] == "user" and len(m["content"].split()) > 4), None)
        if not last_user:
            return question
        # simple fusion: keep original + context hint
        # e.g. "Còn nếu tôi nghỉ 3 ngày thì sao?" -> "Còn nếu tôi nghỉ 3 ngày thì sao? (context: Chính sách nghỉ phép...)"
        if last_user.lower() not in ql:
            return f"{question} [context: {last_user[:120]}]"
        return question

    def get_slots(self, session_id: str) -> dict:
        """Merge slots from recent history (simple: last leave_request slots)."""
        slots: dict = {}
        for m in self._store[session_id]:
            if m.get("intent") == "leave_request":
                # naive: re-parse content for days/date
                import re
                c = m["content"]
                dm = re.search(r"(\d+)\s*ngày", c)
                if dm:
                    slots["days"] = int(dm.group(1))
                d2 = re.search(r"(\d{1,2}[/-]\d{1,2})", c)
                if d2:
                    slots["start_date"] = d2.group(1)
        return slots

    def last_intent(self, session_id: str) -> str | None:
        for m in reversed(self._store[session_id]):
            if m.get("intent"):
                return m["intent"]
        return None

    def clear(self, session_id: str) -> None:
        self._store.pop(session_id, None)

    def all_sessions(self) -> list[str]:
        return list(self._store.keys())


session_store = SessionStore()
