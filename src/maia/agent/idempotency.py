"""Side-effect idempotency for agent tool execution.

WHY THIS EXISTS
---------------
An agent that calls a write-capable tool can be retried for reasons that have
nothing to do with the tool itself: the LLM times out after the tool already
succeeded, the client reconnects and replays the request, a supervisor retries
a failed run. Without a guard, each retry re-executes the side effect.

That is not hypothetical here. `create_leave_request` decrements the employee's
balance and appends a record on every call, so a single retried HITL approval
would silently spend leave twice — the user sees two requests and one balance
deduction they did not authorise.

THE CONTRACT
------------
    operation_id  = f(tenant, employee, tool, canonical params)

The caller may supply `operation_id` explicitly. When it does not, one is
derived deterministically from the identity of the work, so the natural retry
path — the same request arriving again — collapses onto the same key.

Three outcomes, and the third is the important one:

    new        -> execute, record the result
    replay     -> return the STORED result, do not execute
    in_flight  -> a concurrent attempt holds the key; refuse rather than
                  double-execute, and say so

`in_flight` exists because a plain "does the key exist?" check is a race: two
processes can both miss and both execute. Claiming the key BEFORE doing the
work converts that into a visible refusal instead of a silent duplicate.

DURABILITY
----------
Records live in Postgres when `MAIA_POSTGRES_DSN` is set, so an idempotency key
still deduplicates across a process restart — which is the case that matters,
because a crash is exactly when a client retries. Without a DSN there is a
process-local fallback that is explicitly NOT crash-safe; it exists so local
development and unit tests work, and `backend_in_use()` reports which one is
active rather than pretending the durable guarantee holds.

This is deliberately not a distributed transaction framework. It is one table,
one unique key, and a claim-then-execute ordering.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

ENV_DSN = "MAIA_POSTGRES_DSN"

#: Terminal outcomes. A failure is deliberately NOT cached: if a tool genuinely
#: failed, replaying that failure would make a corrected retry impossible, so
#: the claim is released and a corrected retry may proceed.
STATUS_NEW = "new"
STATUS_REPLAY = "replay"
STATUS_IN_FLIGHT = "in_flight"


def canonical_params(params: dict[str, Any] | None) -> str:
    """Stable JSON for a params dict.

    `sort_keys` matters: without it `{"a":1,"b":2}` and `{"b":2,"a":1}` would
    derive different keys for the same logical operation, and a retry that
    happened to reorder keys would execute twice.
    """
    return json.dumps(params or {}, sort_keys=True, default=str, separators=(",", ":"))


def derive_operation_id(
    tool: str,
    params: dict[str, Any] | None = None,
    *,
    employee_id: str | None = None,
    tenant_id: str | None = None,
) -> str:
    """Deterministic id for a unit of work.

    Scoped by tenant AND employee so two employees requesting the same leave on
    the same day never collide into one another's stored result.
    """
    payload = "|".join([tool, tenant_id or "-", employee_id or "-",
                       canonical_params(params)])
    return f"{tool}:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def backend_in_use() -> str:
    """`postgres` when crash-safe, `memory` when not. Report this honestly."""
    return "postgres" if _pg_available() else "memory"


# -- process-local fallback: NOT crash-safe -------------------------------
_MEMORY_LOCK = threading.Lock()
_MEMORY: dict[str, dict[str, Any]] = {}


def _memory_claim(operation_id: str) -> tuple[str, dict[str, Any] | None]:
    with _MEMORY_LOCK:
        rec = _MEMORY.get(operation_id)
        if rec is None:
            _MEMORY[operation_id] = {"status": "in_flight"}
            return STATUS_NEW, None
        if rec.get("status") == "done":
            return STATUS_REPLAY, rec.get("result")
        return STATUS_IN_FLIGHT, None


def _memory_complete(operation_id: str, result: dict[str, Any]) -> None:
    with _MEMORY_LOCK:
        _MEMORY[operation_id] = {"status": "done", "result": result}


def _memory_release(operation_id: str) -> None:
    """Drop a claim whose execution produced no result.

    Leaving it would wedge the key permanently: every later retry would be told
    "in flight" by a process that has already exited.
    """
    with _MEMORY_LOCK:
        rec = _MEMORY.get(operation_id)
        if rec is not None and rec.get("status") == "in_flight":
            del _MEMORY[operation_id]


def reset_memory_backend() -> None:
    """Clear the process-local fallback. Test helper only."""
    with _MEMORY_LOCK:
        _MEMORY.clear()


_PG_DDL = """
CREATE TABLE IF NOT EXISTS maia_idempotency (
    operation_id TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    result       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _pg_available() -> bool:
    return bool(os.environ.get(ENV_DSN))


def _pg_claim(operation_id: str) -> tuple[str, dict[str, Any] | None]:
    """Claim the key, or report why we cannot. Returns (status, stored)."""
    import psycopg

    with psycopg.connect(os.environ[ENV_DSN]) as conn:
        with conn.cursor() as cur:
            cur.execute(_PG_DDL)
            # Claim FIRST. The PRIMARY KEY makes this atomic, so two concurrent
            # callers cannot both win: the loser gets no row back and falls
            # through to inspecting the winner's row. Claiming after execution
            # would leave the double-execute window wide open.
            cur.execute(
                """INSERT INTO maia_idempotency (operation_id, status)
                   VALUES (%s, 'in_flight')
                   ON CONFLICT (operation_id) DO NOTHING
                   RETURNING operation_id""",
                (operation_id,),
            )
            if cur.fetchone() is not None:
                conn.commit()
                return STATUS_NEW, None

            cur.execute(
                "SELECT status, result FROM maia_idempotency WHERE operation_id = %s",
                (operation_id,),
            )
            row = cur.fetchone()
        conn.commit()

    if row is None:
        # Row disappeared between the insert and the select (another process
        # released it). Treat as new so a legitimate retry can proceed.
        return STATUS_NEW, None
    status, result = row
    if status == "done":
        return STATUS_REPLAY, result
    return STATUS_IN_FLIGHT, None


def _pg_complete(operation_id: str, result: dict[str, Any]) -> None:
    import psycopg

    with psycopg.connect(os.environ[ENV_DSN]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE maia_idempotency SET status = 'done', result = %s
                   WHERE operation_id = %s""",
                (json.dumps(result), operation_id),
            )
        conn.commit()


def _pg_release(operation_id: str) -> None:
    import psycopg

    with psycopg.connect(os.environ[ENV_DSN]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM maia_idempotency "
                "WHERE operation_id = %s AND status = 'in_flight'",
                (operation_id,),
            )
        conn.commit()


def run_once(
    fn: Callable[[], dict[str, Any]],
    *,
    tool: str,
    params: dict[str, Any] | None = None,
    employee_id: str | None = None,
    tenant_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Execute `fn` at most once for this operation id.

    Returns the tool result on first execution, the STORED result on replay, or
    an explicit refusal when a concurrent attempt already holds the key.
    """
    op_id = operation_id or derive_operation_id(
        tool, params, employee_id=employee_id, tenant_id=tenant_id
    )

    durable = _pg_available()
    if durable:
        status, stored = _pg_claim(op_id)
    else:
        status, stored = _memory_claim(op_id)

    if status == STATUS_REPLAY:
        replayed = dict(stored or {})
        # Explicit markers so a caller can tell a replay from a fresh execution
        # without comparing timestamps or guessing from a missing field. The
        # stored payload is returned verbatim — the caller sees the SAME
        # request_id it got the first time, which is the whole point: the retry
        # is told what happened rather than being sent to do it again.
        replayed["idempotent_replay"] = True
        replayed["idempotent_status"] = STATUS_REPLAY
        replayed["operation_id"] = op_id
        return replayed

    if status == STATUS_IN_FLIGHT:
        return {
            "ok": False,
            "error": ("A concurrent execution already holds this operation id; "
                      "refusing to run the side effect twice."),
            "operation_id": op_id,
            "idempotent_status": STATUS_IN_FLIGHT,
        }

    try:
        result = fn()
    except Exception:
        # Release rather than cache: the work did not complete, and a cached
        # failure would be indistinguishable from a business rejection.
        (_pg_release if durable else _memory_release)(op_id)
        raise

    (_pg_complete if durable else _memory_complete)(op_id, result)

    out = dict(result or {})
    out["operation_id"] = op_id
    out["idempotent_status"] = STATUS_NEW
    out["idempotent_backend"] = "postgres" if durable else "memory"
    return out

