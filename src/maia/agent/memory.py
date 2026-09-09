"""Long-term memory (LTM) across sessions — user preferences & facts.

SQLite-backed (stdlib only), scoped by (user_id, tenant_id) so tenants stay
isolated. Recall is token-overlap ranked (maia.textnorm): fully offline, no
embeddings needed.

Opt-in via LTM_ENABLED (default false): when disabled every helper is a
no-op and chat behavior is byte-identical to short-term-only memory.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time

from ..config import settings
from ..textnorm import norm_tokens

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,  -- 'preference' | 'fact'
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, tenant_id, content)
);
CREATE INDEX IF NOT EXISTS idx_mem_user_tenant ON memories(user_id, tenant_id);
"""

# Rule-based preference/fact extraction (VI + EN). Conservative: only
# explicit self-statements become memories, never arbitrary chat text.
# Order matters: specific name patterns win over the generic "hãy".
_EXTRACTORS: list[tuple[str, re.Pattern]] = [
    ("fact", re.compile(r"(?:tên tôi là|hãy gọi tôi là|my name is|call me)\s+(.+)", re.IGNORECASE)),
    ("preference", re.compile(r"(?:tôi thích|tôi muốn|hãy|prefer|i like|i prefer)\s+(.+)", re.IGNORECASE)),
    ("fact", re.compile(r"(?:tôi (?:làm (?:ở|việc (?:ở|tại))|thuộc))\s+(.+)", re.IGNORECASE)),
]


def extract_memories(text: str) -> list[tuple[str, str]]:
    """Extract (memory_type, content) pairs from an explicit self-statement."""
    out: list[tuple[str, str]] = []
    for mtype, pat in _EXTRACTORS:
        m = pat.search(text or "")
        if m:
            content = m.group(0).strip()[:280]
            if len(content.split()) >= 2:
                out.append((mtype, content))
    return out


class LongTermMemory:
    """Persistent cross-session memory. Thread-safe, lazy-connecting."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.LTM_DB_PATH
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            import os

            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
        return self._conn

    def store(self, user_id: str, tenant_id: str, memory_type: str, content: str) -> int | None:
        """Store a memory; returns row id (dedupe: same content -> existing id)."""
        content = (content or "").strip()[:500]
        if not content:
            return None
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO memories "
                    "(user_id, tenant_id, memory_type, content, created_at, last_accessed) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, tenant_id, memory_type, content, now, now))
                conn.commit()
                if cur.lastrowid:
                    return cur.lastrowid
                row = conn.execute(
                    "SELECT id FROM memories WHERE user_id=? AND tenant_id=? AND content=?",
                    (user_id, tenant_id, content)).fetchone()
                return row[0] if row else None
            except Exception:
                return None

    def recall(self, user_id: str, tenant_id: str, query: str, k: int = 5) -> list[dict]:
        """Top-k memories ranked by token overlap with the query (offline)."""
        try:
            with self._lock:
                conn = self._connect()
                rows = conn.execute(
                    "SELECT id, memory_type, content, access_count FROM memories "
                    "WHERE user_id=? AND tenant_id=?", (user_id, tenant_id)).fetchall()
        except Exception:
            return []
        qtokens = set(norm_tokens(query))
        scored = []
        for mid, mtype, content, count in rows:
            overlap = len(qtokens & set(norm_tokens(content)))
            if overlap > 0:
                scored.append((overlap, count, {"id": mid, "type": mtype, "content": content}))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        hits = [s[2] for s in scored[: max(0, k)]]
        if hits:
            with self._lock:
                try:
                    conn = self._connect()
                    conn.executemany(
                        "UPDATE memories SET access_count=access_count+1, last_accessed=? "
                        "WHERE id=?", [(time.time(), h["id"]) for h in hits])
                    conn.commit()
                except Exception:
                    pass
        return hits

    def forget(self, user_id: str, tenant_id: str, memory_id: int) -> bool:
        with self._lock:
            try:
                conn = self._connect()
                cur = conn.execute(
                    "DELETE FROM memories WHERE id=? AND user_id=? AND tenant_id=?",
                    (memory_id, user_id, tenant_id))
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                return False

    def clear_user(self, user_id: str, tenant_id: str) -> int:
        with self._lock:
            try:
                conn = self._connect()
                cur = conn.execute(
                    "DELETE FROM memories WHERE user_id=? AND tenant_id=?",
                    (user_id, tenant_id))
                conn.commit()
                return cur.rowcount
            except Exception:
                return 0

    def count(self, user_id: str, tenant_id: str) -> int:
        try:
            with self._lock:
                conn = self._connect()
                row = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE user_id=? AND tenant_id=?",
                    (user_id, tenant_id)).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0


_ltm: LongTermMemory | None = None
_ltm_lock = threading.Lock()


def get_ltm() -> LongTermMemory:
    """Process-wide singleton (lazy: no file created until first use)."""
    global _ltm
    if _ltm is None:
        with _ltm_lock:
            if _ltm is None:
                _ltm = LongTermMemory()
    return _ltm


def ltm_context(user_id: str, tenant_id: str, question: str, k: int = 3) -> str:
    """Recalled-preferences block for the prompt, or '' when LTM is off."""
    if not settings.LTM_ENABLED:
        return ""
    try:
        hits = get_ltm().recall(user_id, tenant_id, question, k=k)
    except Exception:
        return ""
    if not hits:
        return ""
    lines = "\n".join(f"- ({h['type']}) {h['content']}" for h in hits)
    return f"Known about this employee (long-term memory):\n{lines}"


def ltm_learn(user_id: str, tenant_id: str, text: str) -> list[tuple[str, str]]:
    """Extract + persist explicit self-statements, or [] when LTM is off."""
    if not settings.LTM_ENABLED:
        return []
    stored = []
    try:
        for mtype, content in extract_memories(text):
            if get_ltm().store(user_id, tenant_id, mtype, content) is not None:
                stored.append((mtype, content))
    except Exception:
        pass
    return stored
