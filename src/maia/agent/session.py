"""Session / Memory for multi-turn chat with SQLite persistence."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from ..config import settings


def _make_key(tenant_id: str | None, session_id: str) -> tuple:
    """Create composite key for tenant isolation."""
    tid = tenant_id or settings.TENANT_ID
    return (tid, session_id)



def _db_path() -> Path:
    p = Path(settings.SESSION_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


class SessionStore:
    def __init__(self, max_turns: int | None = None, db_path: str | None = None):
        self.max_turns = max_turns or settings.MAX_HISTORY_TURNS
        # (tenant_id, session_id) -> deque of messages {role, content, ts, intent}
        self._store: dict[tuple, deque] = {}
        # (tenant_id, session_id) -> pending side-effect proposal awaiting approval (C1)
        self._pending: dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self._db_path = db_path or str(_db_path())
        self._conn = _connect(self._db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        """Ensure tables exist and clear stale data for fresh instance."""
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 tenant_id TEXT NOT NULL,
                 session_id TEXT NOT NULL,
                 role TEXT NOT NULL,
                 content TEXT NOT NULL,
                 intent TEXT,
                 ts REAL NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_actions (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 tenant_id TEXT NOT NULL,
                 session_id TEXT NOT NULL,
                 tool TEXT NOT NULL,
                 params_json TEXT NOT NULL,
                 summary TEXT NOT NULL,
                 question TEXT,
                 intent TEXT,
                 slots_json TEXT,
                 employee_id TEXT,
                 requester_email TEXT,
                 citations_json TEXT,
                 used_keys_json TEXT,
                 evidence_json,
                 created_at REAL NOT NULL,
                 UNIQUE(tenant_id, session_id, tool))"""
        )
        # Clear stale data from previous runs for this instance's tables
        self._conn.execute("DELETE FROM sessions")
        self._conn.execute("DELETE FROM pending_actions")
        self._conn.commit()

    def _init_store(self, key: tuple) -> None:
        """Ensure all historical messages for a key are loaded from SQLite."""
        if key in self._store:
            return
        tenant_id, session_id = key
        rows = self._conn.execute(
            "SELECT role, content, intent, ts FROM sessions WHERE tenant_id=? AND session_id=? ORDER BY id",
            (tenant_id, session_id),
        ).fetchall()
        dq = deque(maxlen=self.max_turns * 2)
        for r in rows:
            dq.append({"role": r["role"], "content": r["content"], "intent": r["intent"], "ts": r["ts"]})
        self._store[key] = dq

    def _init_pending(self, key: tuple) -> None:
        """Ensure pending action for a key is loaded from SQLite."""
        if key in self._pending:
            return
        tenant_id, session_id = key
        row = self._conn.execute(
            "SELECT tool, params_json, summary, question, intent, slots_json, employee_id, requester_email, citations_json, used_keys_json, evidence_json, created_at FROM pending_actions WHERE tenant_id=? AND session_id=? ORDER BY id DESC LIMIT 1",
            (tenant_id, session_id),
        ).fetchone()
        if row:
            self._pending[key] = {
                "tool": row["tool"],
                "params": json.loads(row["params_json"] or "{}"),
                "summary": row["summary"],
                "question": row["question"],
                "intent": row["intent"],
                "slots": json.loads(row["slots_json"] or "{}") if row["slots_json"] else {},
                "employee_id": row["employee_id"],
                "requester_email": row["requester_email"],
                "citations": json.loads(row["citations_json"]) if row["citations_json"] else [],
                "used_keys": json.loads(row["used_keys_json"]) if row["used_keys_json"] else [],
                "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else [],
                "created_at": row["created_at"],
            }

    def append(self, session_id: str, role: str, content: str, intent: str | None = None, tenant_id: str | None = None) -> None:
        key = _make_key(tenant_id, session_id)
        msg = {"role": role, "content": content, "intent": intent, "ts": time.time()}
        with self._lock:
            self._init_store(key)
            self._store[key].append(msg)
            self._conn.execute(
                "INSERT INTO sessions(tenant_id, session_id, role, content, intent, ts) VALUES(?, ?, ?, ?, ?, ?)",
                (key[0], key[1], role, content, intent, msg["ts"]),
            )
            self._conn.commit()

    def history(self, session_id: str, tenant_id: str | None = None) -> list[dict]:
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._init_store(key)
            return list(self._store.get(key, deque()))

    def history_text(self, session_id: str, last_n: int = 4, tenant_id: str | None = None) -> str:
        hist = self.history(session_id, tenant_id)[-last_n:]
        if not hist:
            return ""
        return "\n".join(f"{m['role']}: {m['content'][:300]}" for m in hist)

    def rewrite_query(self, session_id: str, question: str, tenant_id: str | None = None) -> str:
        """Query rewrite using short-term context (Agentic RAG §18).
        Handles anaphora like 'còn nếu tôi nghỉ 3 ngày thì sao?' by
        prepending last user intent context.
        """
        hist = self.history(session_id, tenant_id)
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

    def get_slots(self, session_id: str, tenant_id: str | None = None) -> dict:
        """Merge slots from recent history (simple: last leave_request slots)."""
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._init_store(key)
            slots: dict = {}
            for m in self._store.get(key, deque()):
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

    def last_intent(self, session_id: str, tenant_id: str | None = None) -> str | None:
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._init_store(key)
            for m in reversed(self._store.get(key, deque())):
                if m.get("intent"):
                    return m["intent"]
            return None

    def clear(self, session_id: str, tenant_id: str | None = None) -> None:
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._store.pop(key, None)
            self._pending.pop(key, None)
            self._conn.execute("DELETE FROM sessions WHERE tenant_id=? AND session_id=?", (key[0], key[1]))
            self._conn.execute("DELETE FROM pending_actions WHERE tenant_id=? AND session_id=?", (key[0], key[1]))
            self._conn.commit()

    def all_sessions(self) -> list[tuple]:
        with self._lock:
            return list(self._store.keys())

    # ---- Pending side-effect actions (C1: confirm-before-action) ----
    def set_pending(self, session_id: str, pending: dict, tenant_id: str | None = None) -> None:
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._init_pending(key)
            params = pending.get("params", pending.get("params_json", {}))
            if isinstance(params, dict):
                params_json = json.dumps(params, ensure_ascii=False)
            else:
                params_json = str(params)
            self._conn.execute(
                "INSERT INTO pending_actions(tenant_id, session_id, tool, params_json, summary, question, intent, slots_json, employee_id, requester_email, citations_json, used_keys_json, evidence_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, session_id, tool) DO UPDATE SET params_json=excluded.params_json, summary=excluded.summary, question=excluded.question, intent=excluded.intent, slots_json=excluded.slots_json, employee_id=excluded.employee_id, requester_email=excluded.requester_email, citations_json=excluded.citations_json, used_keys_json=excluded.used_keys_json, evidence_json=excluded.evidence_json, created_at=excluded.created_at",
                (key[0], key[1], pending.get("tool", ""), params_json,
                 pending.get("summary", ""), pending.get("question"), pending.get("intent"),
                 json.dumps(pending.get("slots", {}), ensure_ascii=False) if pending.get("slots") else "{}",
                 pending.get("employee_id"), pending.get("requester_email"),
                 json.dumps(pending.get("citations", []), ensure_ascii=False) if pending.get("citations") else "[]",
                 json.dumps(pending.get("used_keys", []), ensure_ascii=False) if pending.get("used_keys") else "[]",
                 json.dumps(pending.get("evidence", []), ensure_ascii=False) if pending.get("evidence") else "[]",
                 pending.get("created_at", time.time())),
            )
            self._conn.commit()
            self._pending[key] = pending

    def get_pending(self, session_id: str, tenant_id: str | None = None) -> dict | None:
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._init_pending(key)
            return self._pending.get(key)

    def pop_pending(self, session_id: str, tenant_id: str | None = None) -> dict | None:
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._init_pending(key)
            result = self._pending.pop(key, None)
            if result:
                self._conn.execute("DELETE FROM pending_actions WHERE tenant_id=? AND session_id=? AND tool=?",
                                   (key[0], key[1], result.get("tool", "")))
                self._conn.commit()
            return result

    def save_pending(self, session_id: str, tenant_id: str | None = None) -> None:
        """Persist pending actions to a JSON file for restart recovery.

        The in-memory store remains the primary store; this is a backup.
        """
        key = _make_key(tenant_id, session_id)
        with self._lock:
            self._init_pending(key)
            pending = self._pending.get(key)
            if not pending:
                return
            p = Path(settings.SESSION_DB_PATH).with_suffix(".json")
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "tool": pending.get("tool", ""),
                "params": pending.get("params", {}),
                "summary": pending.get("summary", ""),
                "question": pending.get("question", ""),
                "intent": pending.get("intent", ""),
                "slots": pending.get("slots", {}),
                "employee_id": pending.get("employee_id", ""),
                "requester_email": pending.get("requester_email", ""),
                "citations": pending.get("citations", []),
                "used_keys": pending.get("used_keys", []),
                "evidence": pending.get("evidence", {}),
                "created_at": pending.get("created_at", 0),
            }
            with open(p, "w") as f:
                json.dump(data, f, ensure_ascii=False)

    def load_pending(self, session_id: str, tenant_id: str | None = None) -> dict | None:
        """Load pending actions from the JSON file for restart recovery.

        Returns the pending dict if found, None otherwise.
        The in-memory store is populated from this data.
        """
        key = _make_key(tenant_id, session_id)
        p = Path(settings.SESSION_DB_PATH).with_suffix(".json")
        if not p.exists():
            return None
        try:
            with open(p, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if not data:
            return None
        with self._lock:
            self._init_pending(key)
            self._pending[key] = data
            return data


session_store = SessionStore(db_path=str(_db_path()))
