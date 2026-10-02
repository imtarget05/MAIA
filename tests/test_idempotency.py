"""M3 — side-effect idempotency for write-capable agent tools.

The question under test: "what if the agent/tool call is retried after a
timeout?" Without a guard, `create_leave_request` decrements the leave balance
and appends a record on every call, so one retried HITL approval would spend
leave twice.

The Postgres assertions skip without a DSN because the memory backend genuinely
cannot provide the crash-safety guarantee they exist to check, and a test that
silently degrades to a weaker claim is worse than one that skips loudly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.idempotency import (  # noqa: E402
    ENV_DSN,
    STATUS_IN_FLIGHT,
    STATUS_NEW,
    STATUS_REPLAY,
    backend_in_use,
    derive_operation_id,
    reset_memory_backend,
    run_once,
)

DSN = os.environ.get(ENV_DSN)
needs_db = pytest.mark.skipif(
    not DSN, reason=f"can {ENV_DSN} troi toi Postgres de test durable backend")

PARAMS = {"days": 2, "start_date": "2026-10-15"}


@pytest.fixture(autouse=True)
def _clean_memory():
    reset_memory_backend()
    yield
    reset_memory_backend()


def _forget(operation_id: str) -> None:
    """Remove one key from the durable table so a test starts from nothing.

    Without this the suite is not repeatable: a previous run's stored result
    makes the FIRST call a replay, and the test then fails while looking like
    an idempotency bug. `calls == 0` is the symptom — the tool never ran
    because a stale record claimed the key was already done.
    """
    if not DSN:
        return
    import psycopg

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM maia_idempotency WHERE operation_id = %s",
                    (operation_id,))
        conn.commit()


def _derive(**kw) -> str:
    return derive_operation_id("create_leave_request", PARAMS, **kw)


def _claim_in_flight(operation_id: str) -> None:
    """Write an in-flight row directly, simulating another live process."""
    if not DSN:
        from maia.agent.idempotency import _memory_claim
        _memory_claim(operation_id)
        return
    import psycopg

    from maia.agent.idempotency import _PG_DDL
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(_PG_DDL)
        cur.execute(
            """INSERT INTO maia_idempotency (operation_id, status)
               VALUES (%s, 'in_flight')
               ON CONFLICT (operation_id) DO NOTHING""",
            (operation_id,),
        )
        conn.commit()


class Counter:
    """A tool that counts how many times the side effect actually ran."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        return {"ok": True, "request_id": "LV-001", "remaining_balance": 10}


def test_duplicate_delivery_executes_side_effect_once():
    """The headline requirement: same action twice -> one business effect."""
    kw = {"employee_id": "emp_001", "tenant_id": "t1"}
    _forget(_derive(**kw))
    tool = Counter()
    for _ in range(2):
        run_once(tool, tool="create_leave_request", params=PARAMS, **kw)
    assert tool.calls == 1, "a retry must not spend leave twice"
    _forget(_derive(**kw))


def test_replay_returns_the_stored_result():
    kw = {"employee_id": "emp_002", "tenant_id": "t1"}
    _forget(_derive(**kw))
    tool = Counter()
    first = run_once(tool, tool="create_leave_request", params=PARAMS, **kw)
    second = run_once(tool, tool="create_leave_request", params=PARAMS, **kw)
    assert first["idempotent_status"] == STATUS_NEW
    assert second["idempotent_status"] == STATUS_REPLAY
    assert second["idempotent_replay"] is True
    # Same request_id both times: the caller is told what already happened.
    assert second["request_id"] == first["request_id"]
    assert tool.calls == 1
    _forget(_derive(**kw))


def test_different_params_are_a_different_operation():
    """A genuinely different request must still be allowed to run."""
    kw = {"employee_id": "emp_003", "tenant_id": "t1"}
    other = {"days": 5, "start_date": "2026-10-15"}
    _forget(_derive(**kw))
    _forget(derive_operation_id("create_leave_request", other, **kw))
    tool = Counter()
    run_once(tool, tool="create_leave_request", params=PARAMS, **kw)
    run_once(tool, tool="create_leave_request", params=other, **kw)
    assert tool.calls == 2
    _forget(_derive(**kw))
    _forget(derive_operation_id("create_leave_request", other, **kw))


def test_explicit_operation_ids_isolate_otherwise_identical_work():
    """Two distinct authorised operations with identical params both run."""
    tool = Counter()
    _forget("op-A")
    _forget("op-B")
    run_once(tool, tool="create_leave_request", params=PARAMS,
             operation_id="op-A")
    run_once(tool, tool="create_leave_request", params=PARAMS,
             operation_id="op-B")
    assert tool.calls == 2
    _forget("op-A")
    _forget("op-B")


def test_key_is_scoped_per_employee_and_tenant():
    """Two employees must never share one stored result — that is a data leak."""
    a = derive_operation_id("create_leave_request", PARAMS,
                            employee_id="emp_001", tenant_id="t1")
    b = derive_operation_id("create_leave_request", PARAMS,
                            employee_id="emp_002", tenant_id="t1")
    c = derive_operation_id("create_leave_request", PARAMS,
                            employee_id="emp_001", tenant_id="t2")
    assert len({a, b, c}) == 3


def test_param_order_does_not_change_the_key():
    """A retry that reorders keys is still the same operation."""
    assert derive_operation_id("t", {"a": 1, "b": 2}) == \
        derive_operation_id("t", {"b": 2, "a": 1})


def test_concurrent_holder_is_refused_not_executed():
    """A key held in-flight must refuse, never double-execute.

    The in-flight row is written directly into the durable table, because the
    memory backend cannot represent a claim made by a *different* process.
    """
    op = f"op-concurrent-{os.getpid()}"
    _forget(op)
    _claim_in_flight(op)
    tool = Counter()
    out = run_once(tool, tool="create_leave_request", params=PARAMS,
                   operation_id=op)
    assert out["ok"] is False
    assert out["idempotent_status"] == STATUS_IN_FLIGHT
    assert tool.calls == 0, "must refuse rather than execute a second time"
    _forget(op)


def test_exception_releases_the_claim_so_a_corrected_retry_can_run():
    """A crash must not permanently wedge the key."""
    op = f"op-boom-{os.getpid()}"
    _forget(op)

    def explodes():
        raise RuntimeError("provider timeout")

    with pytest.raises(RuntimeError):
        run_once(explodes, tool="create_leave_request", params=PARAMS,
                 operation_id=op)

    good = Counter()
    out = run_once(good, tool="create_leave_request", params=PARAMS,
                   operation_id=op)
    assert out["idempotent_status"] == STATUS_NEW
    assert good.calls == 1
    _forget(op)


def test_backend_in_use_reports_memory_when_no_dsn(monkeypatch):
    monkeypatch.delenv(ENV_DSN, raising=False)
    assert backend_in_use() == "memory"


# ---------------------------------------------------------------------------
# Durable backend — the part that survives a restart
# ---------------------------------------------------------------------------
_PROBE = """
import sys, json
sys.path.insert(0, 'src')
from maia.agent.idempotency import run_once
r = run_once(lambda: {'ok': True, 'request_id': 'LV-777'},
             tool='create_leave_request', operation_id=OP_ID)
print(json.dumps({'status': r.get('idempotent_status'),
                  'replay': r.get('idempotent_replay', False),
                  'rid': r.get('request_id')}))
"""


def _probe_in_new_process(op_id: str) -> dict:
    """One claim+execute (or replay) in a genuinely separate interpreter.

    The operation id is injected by substitution rather than str.format: the
    probe body contains dict literals, and `.format()` would try to interpret
    those braces as replacement fields.
    """
    import json

    out = subprocess.run(
        [sys.executable, "-c", _PROBE.replace("OP_ID", repr(op_id))],
        capture_output=True, text=True, env={**os.environ},
        cwd=str(Path(__file__).resolve().parents[1]), timeout=60,
    )
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_db
def test_restart_between_execution_and_retry_does_not_duplicate():
    """The requirement only a DURABLE backend can satisfy.

    Process A executes the side effect and exits. Process B — a new
    interpreter, sharing no memory with A — retries the same operation id. It
    must replay the stored result rather than re-execute. A memory backend
    could satisfy this within one interpreter, which is exactly why the
    assertion is made across two processes.
    """
    op = f"op-restart-{os.getpid()}"
    _forget(op)
    first = _probe_in_new_process(op)
    assert first["status"] == STATUS_NEW, first

    second = _probe_in_new_process(op)
    assert second["status"] == STATUS_REPLAY, second
    assert second["replay"] is True
    assert second["rid"] == "LV-777", "the original result must be replayed"
    _forget(op)


@needs_db
def test_durable_backend_really_is_postgres():
    """Guard against the durable tests silently running on the memory backend."""
    assert backend_in_use() == "postgres"


@needs_db
def test_distinct_operation_ids_both_execute_across_processes():
    """A different authorised operation is not blocked by the first one's record."""
    a = f"op-a-{os.getpid()}"
    b = f"op-b-{os.getpid()}"
    assert _probe_in_new_process(a)["status"] == STATUS_NEW
    assert _probe_in_new_process(b)["status"] == STATUS_NEW

# ---------------------------------------------------------------------------
# End-to-end through the REAL agent entry point, not the module in isolation
# ---------------------------------------------------------------------------
def test_execute_tool_deduplicates_a_retried_approval():
    """The wiring itself must be idempotent, not just the helper.

    `_execute_tool` is where an approved HITL interrupt actually reaches a tool.
    Testing only `run_once` would leave a regression where the guard works but
    nothing calls it.
    """
    from maia.agent import idempotency as idem
    from maia.agent import langgraph_agent as lga
    from maia.agent import tools as tools_mod

    calls = {"n": 0}

    def fake_tool(**kw):
        calls["n"] += 1
        return {"ok": True, "request_id": f"LV-{calls['n']:03d}"}

    saved = tools_mod.TOOL_REGISTRY["create_leave_request"]
    tools_mod.TOOL_REGISTRY["create_leave_request"] = fake_tool
    op = None
    try:
        params = {"days": 2, "start_date": "2026-11-02"}
        op = idem.derive_operation_id("create_leave_request", params,
                                     employee_id="emp_wire", tenant_id="t1")
        _forget(op)
        first = lga._execute_tool("create_leave_request", params, "emp_wire", "t1")
        second = lga._execute_tool("create_leave_request", params, "emp_wire", "t1")
    finally:
        tools_mod.TOOL_REGISTRY["create_leave_request"] = saved
        if op:
            _forget(op)

    assert calls["n"] == 1, "a retried approval must not spend leave twice"
    assert first["idempotent_status"] == STATUS_NEW
    assert second["idempotent_replay"] is True
    assert second["request_id"] == first["request_id"]


def test_read_only_tool_is_not_gated():
    """A re-read must stay a re-read, not become a cached 'replay'."""
    from maia.agent import langgraph_agent as lga
    from maia.agent import tools as tools_mod

    calls = {"n": 0}

    def fake_balance(**kw):
        calls["n"] += 1
        return {"ok": True, "balance": 10 - calls["n"]}

    saved = tools_mod.TOOL_REGISTRY["check_leave_balance"]
    tools_mod.TOOL_REGISTRY["check_leave_balance"] = fake_balance
    try:
        a = lga._execute_tool("check_leave_balance", {}, "emp_ro", "t1")
        b = lga._execute_tool("check_leave_balance", {}, "emp_ro", "t1")
    finally:
        tools_mod.TOOL_REGISTRY["check_leave_balance"] = saved

    assert calls["n"] == 2, "a read-only tool must execute every time"
    assert a["balance"] != b["balance"]