"""Approval workflow store: pending -> approved/rejected, shared across processes.

Both the FastAPI backend and the Streamlit UI record proposals here (same
SQLite FILE, one connection per call — no cross-process state issues) so the
admin dashboard sees every request awaiting decision, whoever proposed it.

Tables:
  requests — one row per proposed side-effect (leave / IT ticket)
  events   — append-only activity feed (proposed / approved / rejected / ...)
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import settings

_STATUSES = ("pending", "approved", "rejected", "cancelled")

# P1-8: explicit state-transition allowlist (fail-closed).
_VALID_TRANSITIONS = {
    "pending": frozenset({"approved", "rejected", "cancelled"}),
    # terminal states never transition again
    "approved": frozenset(),
    "rejected": frozenset(),
    "cancelled": frozenset(),
}


def _safe_add_column(con: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """Idempotent ADD COLUMN: no-op if the column already exists (existing DBs)."""
    try:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except Exception:
        pass


def _db_path() -> Path:
    p = Path(settings.WORKFLOW_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS requests(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             type TEXT NOT NULL, session_id TEXT DEFAULT '',
             tool TEXT DEFAULT '', requester_email TEXT DEFAULT '',
             employee_id TEXT DEFAULT '', tenant_id TEXT DEFAULT 'default',
             summary TEXT DEFAULT '', params_json TEXT DEFAULT '{}',
             status TEXT DEFAULT 'pending', result_ref TEXT DEFAULT '',
             result_json TEXT DEFAULT '{}', decided_by TEXT DEFAULT '',
             created_at REAL NOT NULL, decided_at REAL,
             idempotency_key TEXT UNIQUE)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS events(
             id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
             kind TEXT NOT NULL, actor TEXT DEFAULT '', note TEXT DEFAULT '',
             actor_tenant_id TEXT DEFAULT '', actor_role TEXT DEFAULT '',
             ts REAL NOT NULL)"""
    )
    # P1-8: audit-trail backfill for DBs created by older releases.
    _safe_add_column(con, "events", "actor_tenant_id", "TEXT DEFAULT ''")
    _safe_add_column(con, "events", "actor_role", "TEXT DEFAULT ''")
    return con


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["params"] = json.loads(d.pop("params_json", "{}") or "{}")
    except Exception:
        d["params"] = {}
    try:
        d["result"] = json.loads(d.pop("result_json", "{}") or "{}")
    except Exception:
        d["result"] = {}
    return d


def record_proposal(*, type: str, session_id: str = "", tool: str = "",
                    requester_email: str = "", employee_id: str = "",
                    tenant_id: str = "", summary: str = "",
                    params: dict | None = None, idempotency_key: str | None = None) -> dict:
    """Upsert a pending proposal (same session+tool re-proposal updates).

    If idempotency_key is provided and already exists, returns existing request.
    """
    now = time.time()
    con = _connect()
    try:
        # Check for existing idempotency_key first
        if idempotency_key:
            cur = con.execute(
                "SELECT * FROM requests WHERE idempotency_key=?",
                (idempotency_key,),
            )
            hit = cur.fetchone()
            if hit:
                return _row_to_dict(hit)

        cur = con.execute(
            "SELECT id FROM requests WHERE session_id=? AND tool=? AND status='pending'",
            (session_id, tool),
        )
        hit = cur.fetchone()
        if hit:
            con.execute(
                "UPDATE requests SET type=?, requester_email=?, employee_id=?, tenant_id=?,"
                " summary=?, params_json=?, created_at=?, idempotency_key=? WHERE id=?",
                (type, requester_email, employee_id, tenant_id or "default",
                 summary, json.dumps(params or {}, ensure_ascii=False), now,
                 idempotency_key, hit["id"]),
            )
            rid = hit["id"]
        else:
            cur = con.execute(
                "INSERT INTO requests(type,session_id,tool,requester_email,employee_id,"
                " tenant_id,summary,params_json,status,created_at,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
                (type, session_id, tool, requester_email, employee_id,
                 tenant_id or "default", summary,
                 json.dumps(params or {}, ensure_ascii=False), now,
                 idempotency_key),
            )
            rid = cur.lastrowid
        con.execute(
            "INSERT INTO events(request_id,kind,actor,note,actor_tenant_id,actor_role,ts) VALUES(?, 'proposed', ?, ?, ?, ?, ?)",
            (rid, requester_email, summary, tenant_id or "", "", now),
        )
        con.commit()
        return get_request(rid) or {"id": rid, "status": "pending"}
    finally:
        con.close()


def _decide_row(con: sqlite3.Connection, rid: int, approved: bool,
                decided_by: str, result_ref: str = "",
                result: dict | None = None) -> dict | None:
    now = time.time()
    status = "approved" if approved else "rejected"
    # P1-8 audit: capture the request's tenant for the actor_tenant_id trail.
    row_tid = ""
    cur = con.execute("SELECT tenant_id FROM requests WHERE id=?", (rid,))
    r = cur.fetchone()
    if r is not None:
        row_tid = r["tenant_id"] or ""
    con.execute(
        "UPDATE requests SET status=?, decided_by=?, result_ref=?, result_json=?,"
        " decided_at=? WHERE id=? AND status='pending'",
        (status, decided_by, result_ref,
         json.dumps(result or {}, ensure_ascii=False), now, rid),
    )
    if con.total_changes == 0:
        return None
    con.execute(
        "INSERT INTO events(request_id,kind,actor,note,actor_tenant_id,actor_role,ts) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (rid, status, decided_by, result_ref, row_tid, "", now),
    )
    con.commit()
    return get_request(rid, _con=con)


def decide(request_id: int, approved: bool, decided_by: str = "",
           result_ref: str = "", result: dict | None = None) -> dict | None:
    con = _connect()
    try:
        return _decide_row(con, request_id, approved, decided_by, result_ref, result)
    finally:
        con.close()


def decide_by_session(session_id: str, tool: str, approved: bool,
                      decided_by: str = "", result_ref: str = "",
                      result: dict | None = None) -> dict | None:
    con = _connect()
    try:
        cur = con.execute(
            "SELECT id FROM requests WHERE session_id=? AND tool=? AND status='pending'"
            " ORDER BY id DESC LIMIT 1",
            (session_id, tool),
        )
        hit = cur.fetchone()
        if not hit:
            return None
        return _decide_row(con, hit["id"], approved, decided_by, result_ref, result)
    finally:
        con.close()


def transition_request(request_id: int, new_status: str,
                       decided_by: str = "", result_ref: str = "",
                       result: dict | None = None,
                       tenant_id: str | None = None) -> dict | None:
    """P1-8: explicitly-validated state transition (fail-closed).

    Rejects any transition not in ``_VALID_TRANSITIONS``. When ``tenant_id`` is
    provided, only allows the transition if the request belongs to that tenant.
    """
    if new_status not in _STATUSES:
        return None
    con = _connect()
    try:
        cur = con.execute("SELECT * FROM requests WHERE id=?", (request_id,))
        row = cur.fetchone()
        if not row:
            return None
        current = row["status"]
        allowed = _VALID_TRANSITIONS.get(current, frozenset())
        if new_status not in allowed:
            return None
        if tenant_id is not None and row["tenant_id"] and row["tenant_id"] != tenant_id:
            return None
        now = time.time()
        con.execute(
            "UPDATE requests SET status=?, decided_by=?, result_ref=?, result_json=?,"
            " decided_at=? WHERE id=? AND status=?",
            (new_status, decided_by, result_ref,
             json.dumps(result or {}, ensure_ascii=False), now, request_id, current),
        )
        if con.total_changes == 0:
            return None
        con.execute(
            "INSERT INTO events(request_id,kind,actor,note,actor_tenant_id,actor_role,ts) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (request_id, new_status, decided_by, result_ref,
             row["tenant_id"] or tenant_id or "", "", now),
        )
        con.commit()
        return get_request(request_id, _con=con)
    finally:
        con.close()


def is_valid_transition(current_status: str, new_status: str) -> bool:
    """P1-8: public predicate for whether a state transition is allowed."""
    return new_status in _VALID_TRANSITIONS.get(current_status, frozenset())


def get_request(request_id: int, _con=None) -> dict | None:
    con = _con or _connect()
    try:
        cur = con.execute("SELECT * FROM requests WHERE id=?", (request_id,))
        hit = cur.fetchone()
        return _row_to_dict(hit) if hit else None
    finally:
        if _con is None:
            con.close()


def list_requests(status: str = "", type: str = "", tenant_id: str = "",
                  limit: int = 100) -> list[dict]:
    con = _connect()
    try:
        q = "SELECT * FROM requests WHERE 1=1"
        args: list = []
        if status:
            q += " AND status=?"; args.append(status)
        if type:
            q += " AND type=?"; args.append(type)
        if tenant_id:
            q += " AND tenant_id=?"; args.append(tenant_id)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return [_row_to_dict(r) for r in con.execute(q, args)]
    finally:
        con.close()


def recent_events(limit: int = 50) -> list[dict]:
    con = _connect()
    try:
        rows = con.execute(
            """SELECT e.id, e.request_id, e.kind, e.actor, e.note, e.ts,
                      r.type, r.summary, r.status
               FROM events e LEFT JOIN requests r ON r.id = e.request_id
               ORDER BY e.id DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]
    finally:
        con.close()


def counts() -> dict:
    con = _connect()
    try:
        rows = con.execute("SELECT status, COUNT(*) n FROM requests GROUP BY status")
        out = {s: 0 for s in _STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        out["total"] = sum(out.values())
        return out
    finally:
        con.close()
