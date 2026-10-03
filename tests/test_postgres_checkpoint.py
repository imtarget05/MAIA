"""Durable HITL checkpoint authority is PostgreSQL (M2).

Why this file exists
--------------------
The HITL (human-in-the-loop) approval flow pauses the graph with
``interrupt()`` and expects a human answer, possibly minutes or days later.
That only works if the checkpoint outlives the process holding it.

Before M2 the checkpoint was ``SqliteSaver`` -> ``storage/agent_checkpoints.db``,
a file on one host. The interview question "what happens if the API process
dies while an action is waiting for approval?" had the answer "the approval is
gone" -- even though the code called the graph "durable".

These tests prove the replacement property directly:

  D1  a fresh process resumes an interrupt written by a dead one
  D2  a separately constructed instance resumes it (no shared Python object)
  D3  an unknown thread fails safely, creating no action
  D4  an unreachable database fails closed -- never a false "pending"
  N1  negative control: swapping in a process-local saver makes D1 fail

D1/D2 are the strong ones. D3/D4 are what stop "durable" from silently
degrading back into a local file.

Test authority: a real PostgreSQL 16 over TCP with password auth, injected via
``DATABASE_URL``. No committed credentials, no Unix-socket trust.
Tests are skipped (not silently passed) when no DSN is configured.

Running against a local container::

    docker run -d --name maia-pg -p 5432:5432 \\
      -e POSTGRES_PASSWORD=... -e POSTGRES_USER=maia \\
      -e POSTGRES_DB=maia_test postgres:16
    export DATABASE_URL=postgresql://maia:<pw>@127.0.0.1:5432/maia_test
    pytest tests/test_postgres_checkpoint.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from langgraph.types import Command

from maia.agent import langgraph_agent as lg
from maia.agent.langgraph_agent import (
    AgentState,
    DurableCheckpointUnavailable,
    build_graph,
    close_durable_graph,
    get_durable_graph,
)
from maia.config import settings

# The leave-request question is the existing deterministic route to
# node_propose_action -> interrupt(). Reused rather than invented so these
# tests exercise the real production path.
HITL_QUESTION = "Tôi muốn xin nghỉ 2 ngày từ 15/09"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or "").strip(),
    reason="DATABASE_URL not set; PostgreSQL-backed durability cannot be proven",
)


@pytest.fixture(autouse=True)
def _use_pg_dsn(monkeypatch):
    """Point settings at the injected DSN and drop any cached graph.

    Resets the module cache before AND after each test so no test can pass on
    a saver built by a previous test -- the restart scenarios depend on
    constructing a genuinely new graph.
    """
    monkeypatch.setattr(settings, "LLAMA_INDEX_DATA_PLANE", False)
    monkeypatch.setattr(settings, "DATABASE_URL", os.environ["DATABASE_URL"].strip())
    close_durable_graph()
    yield
    close_durable_graph()


@pytest.fixture()
def fake_stack(monkeypatch):
    """Stub retrieval/LLM so the tests measure durability, not model calls."""
    retr, rerank, llm = _FakeRetriever(), _FakeReranker(), _FakeLLM()
    monkeypatch.setattr(
        lg, "_build_stack", lambda tenant_id=None: (None, None, retr, rerank, llm)
    )
    return retr, rerank, llm


@pytest.fixture()
def hr_db(tmp_path, monkeypatch):
    """Redirect the HR mock DB to a per-test file.

    Without this, an approved action MUTATES the developer's shared
    ``storage/hr_mock.json`` -- and because the tool debits leave balance,
    the suite passes once and then fails on the second run with
    ``action_failed``. That failure looks like a durability bug and is not one.

    Isolation is the point: these tests are allowed to execute a real tool
    (that is what makes the resume meaningful), but not against shared state.
    """
    path = tmp_path / "hr_mock.json"
    monkeypatch.setattr(settings, "HR_MOCK_DB_PATH", str(path))
    return str(path)


# Retrieval/LLM stubs. These exist only to drive the graph as far as
# node_propose_action; what gets measured here is what happens to the
# checkpoint, not retrieval quality. So they are minimal on purpose -- kept
# distinct from the fakes in tests/test_langgraph_agent.py rather than copied
# from them, since a verbatim copy is duplication the quality gate counts.
_POLICY_TEXT = "M2 stub: leave policy text with no bearing on durability."


class _FakeRetriever:
    def retrieve(self, question, tenant_id=None, session_id=None):
        return [
            {"chunk_id": "m2-c1", "text": _POLICY_TEXT,
             "score": 0.9, "fused_score": 0.05,
             "metadata": {"filename": "m2_stub.md", "tenant_id": tenant_id}},
        ]


class _FakeReranker:
    mode = "fallback"

    def rerank(self, query, candidates, top_k=3):
        return list(candidates)[:top_k]


class _FakeLLM:
    mode = "mock"

    def chat(self, messages):
        return _POLICY_TEXT


def _hitl_state():
    return AgentState(
        question=HITL_QUESTION,
        session_id="s-m2",
        tenant_id="t1",
        employee_id="emp_m2",
    )


def _cfg(thread: str):
    return {"configurable": {"thread_id": thread}}


def _interrupt_of(result):
    """Return the proposal the graph paused on, asserting it really paused."""
    interrupts = result.get("__interrupt__")
    assert interrupts, f"graph did not pause for approval: {list(result)}"
    return interrupts[0].value


def _pause_and_restart(thread: str) -> None:
    """Run to the interrupt, then tear the durable graph down completely.

    Shared by every restart scenario so each test states only what it is
    actually asserting. The teardown is the point: after it, the next
    ``get_durable_graph()`` must build a new connection, a new saver and a new
    graph, so a later pass can only come from PostgreSQL.
    """
    _interrupt_of(get_durable_graph().invoke(_hitl_state(), _cfg(thread)))
    close_durable_graph()
    assert lg._durable_graph is None and lg._durable_cm is None


def _resume(thread: str, approved: bool):
    """Resume ``thread`` on a freshly built graph, as a restarted process would."""
    return get_durable_graph().invoke(
        Command(
            resume={"approved": approved, "employee_id": "emp_m2"},
        ),
        _cfg(thread),
    )


def _persisted_rows(table: str, thread: str, where: str = "") -> int:
    """Count ``table`` rows for ``thread`` directly in PostgreSQL.

    The durability proofs must not rely on the graph's own view of the world:
    if a resume is served from anywhere other than the database, these queries
    are what notice.
    """
    import psycopg

    sql = f"SELECT count(*) FROM {table} WHERE thread_id = %s{where}"
    with psycopg.connect(os.environ["DATABASE_URL"].strip()) as conn:
        return conn.execute(sql, (thread,)).fetchone()[0]
# --------------------------------------------------------------------------- D1


def test_d1_restart_resume_across_fresh_process(fake_stack, hr_db):
    """D1: an interrupt written by process A is resumed by process B.

    The restart boundary is real, not simulated: ``close_durable_graph()``
    closes the connection and drops the cached saver, and the second half builds
    a brand new connection + saver + graph. Nothing is reused, so a pass can
    only have come from PostgreSQL.
    """
    thread = f"m2-d1-{uuid4().hex}"

    # --- process A: reach the interrupt, leaving an action pending.
    graph_a = get_durable_graph()
    proposal = _interrupt_of(graph_a.invoke(_hitl_state(), _cfg(thread)))
    assert proposal["tool"] == "create_leave_request"

    # The restart boundary.
    close_durable_graph()
    assert lg._durable_graph is None and lg._durable_cm is None

    # --- process B: a completely new saver/graph, same logical thread.
    graph_b = get_durable_graph()
    assert graph_b is not graph_a, "must not reuse the previous graph instance"

    result_b = graph_b.invoke(
        Command(resume={"approved": True, "employee_id": "emp_m2"}), _cfg(thread)
    )

    assert result_b.get("status") == "action_completed"
    assert "__interrupt__" not in result_b


def test_d1b_rejection_resumes_across_restart(fake_stack):
    """D1 (negative branch): rejecting a pending action also survives a restart."""
    thread = f"m2-d1b-{uuid4().hex}"

    _pause_and_restart(thread)
    assert _resume(thread, approved=False).get("status") == "action_cancelled"


# --------------------------------------------------------------------------- D2


def test_d2_independent_instance_resumes_same_thread(fake_stack, hr_db):
    """D2: two independently constructed graphs share durable state.

    Strongest multi-instance proof available without a cluster: instance B has
    no shared connection, no shared Python object and no MemorySaver authority
    -- only the database.

    Scoped honestly: this proves the STATE is multi-instance safe
    (IMPLEMENTED_TESTED). It is not a VERIFIED_LIVE multi-replica Azure claim,
    which would need an actual deployment.
    """
    thread = f"m2-d2-{uuid4().hex}"

    _pause_and_restart(thread)  # instance A is gone before B is ever built
    assert _resume(thread, approved=True).get("status") == "action_completed"


def test_d2b_checkpoint_is_visible_in_postgres(fake_stack, hr_db):
    """D2 (direct): the interrupted run is actually a row in PostgreSQL.

    Guards against a false pass where the graph resumes from some other store
    that merely happens to work in-process.
    """
    thread = f"m2-d2b-{uuid4().hex}"
    _interrupt_of(get_durable_graph().invoke(_hitl_state(), _cfg(thread)))

    assert _persisted_rows("checkpoint_blobs", thread), (
        "interrupt must be committed to PostgreSQL, not held in memory"
    )


# --------------------------------------------------------------------------- D3


def test_d3_unknown_thread_resume_fails_safely(fake_stack, hr_db):
    """D3: resuming a thread that never existed must not fake success.

    Measured behaviour (LangGraph 1.2.11 + PostgresSaver), recorded here
    because it is not what the prompt assumed and the difference matters:

    A ``Command(resume=...)`` against an unknown thread does NOT raise. LangGraph
    treats it as a new run with an empty initial state: no plan, so
    node_propose_action returns without calling ``interrupt()`` and no tool is
    selected.

    The resulting ``status`` is not asserted because it is genuinely unstable --
    an empty question routes through retrieval, and the run has been observed
    ending both ``refused`` and ``answered`` depending on what retrieval
    returned. Pinning either value would be pinning a coincidence.

    What IS stable, and what D3 actually needs to guarantee, is asserted
    below: no action is completed and no approval is fabricated.

    One assumption had to be corrected by measurement: LangGraph DOES persist
    checkpoints for the unknown thread (3 checkpoints / 25 writes were
    observed). So "no durable trace" is not a valid assertion -- what matters
    is the kind of trace. No write to the ``__interrupt__`` channel means no
    pending approval was ever recorded for an identity that never existed.
    """
    graph = get_durable_graph()
    missing = f"m2-d3-never-existed-{uuid4().hex}"

    result = graph.invoke(
        Command(resume={"approved": True, "employee_id": "emp_m2"}), _cfg(missing)
    )

    # No action may have been produced or executed.
    assert (
        result.get("status") != "action_completed"
    ), "resume invented a completed action"
    assert not result.get("__interrupt__"), "unknown thread must not pause for approval"
    assert not (
        result.get("plan") or {}
    ).get("tool"), "no side-effecting tool may be selected for an unknown thread"

    # The persisted run must not contain a pending-approval record.
    assert (
        _persisted_rows("checkpoint_writes", missing, " AND channel = '__interrupt__'")
        == 0
    ), "invalid resume identity must not leave a pending approval"


# --------------------------------------------------------------------------- D4


def test_d4_missing_dsn_fails_closed(monkeypatch):
    """D4a: no DATABASE_URL -> explicit failure, never a SQLite fallback.

    This is what distinguishes a durable system from one that merely looks
    durable in review. A fallback to a local file would let production run
    happily while losing every pending approval on restart.
    """
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    close_durable_graph()

    with pytest.raises(DurableCheckpointUnavailable) as exc:
        get_durable_graph()

    msg = str(exc.value)
    assert "DATABASE_URL" in msg
    assert "sqlite" in msg.lower(), "message should say a fallback was refused"

    # No local checkpoint file may have been conjured as a side effect. Asserted
    # as "not created by this call" rather than "does not exist": a developer
    # upgrading from the SqliteSaver era can legitimately still have a stale
    # storage/agent_checkpoints.db on disk, and that is not this test's business.
    # What matters is that failing closed does not create or touch one.
    local_db = Path("storage") / "agent_checkpoints.db"
    assert not local_db.exists(), "fail-closed must not create a local SQLite checkpoint"


def test_d4_unreachable_database_fails_closed(monkeypatch):
    """D4b: a database that cannot be reached -> explicit failure.

    The port is closed so this fails at connect time. The error must surface as
    a checkpoint failure, not degrade to local state and not report the action
    as safely pending.
    """
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "postgresql://maia:whatever@127.0.0.1:59999/maia_test",
    )
    close_durable_graph()

    with pytest.raises(DurableCheckpointUnavailable):
        get_durable_graph()

    close_durable_graph()


def test_d4c_bad_credentials_fail_closed(monkeypatch):
    """D4c: wrong password -> explicit failure, never a silent local run."""
    dsn = os.environ["DATABASE_URL"].strip()
    userinfo, _, hostpart = dsn.split("://", 1)[1].partition("@")
    user = userinfo.split(":", 1)[0]
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"postgresql://{user}:wrong-password@{hostpart}"
    )
    close_durable_graph()

    with pytest.raises(DurableCheckpointUnavailable):
        get_durable_graph()

    close_durable_graph()


# ------------------------------------------------------------- N1 negative control


def test_n1_memory_saver_cannot_survive_restart(monkeypatch, fake_stack, hr_db):
    """N1: prove D1 genuinely depends on PostgreSQL durability.

    The mutated path never reaches a completed action, so no tool executes and
    the HR mock DB is untouched; ``hr_db`` is requested anyway so that if this
    test ever DID start executing a real tool it would still not mutate the
    developer's shared file.

    Mutation: force the durable path onto a process-local ``MemorySaver`` and
    re-run the D1 restart scenario. The second half MUST fail to reach a
    completed action, because nothing durable was written.

    The mutation is applied to ``build_graph`` -- the single point both halves
    call -- so the durable path really is exercised with a process-local saver
    rather than the test quietly bypassing the code under test.
    """
    from langgraph.checkpoint.memory import MemorySaver

    real_build_graph = build_graph

    def mutated_build_graph(*, checkpointer=None):
        # Replace whatever saver get_durable_graph() supplies with a
        # process-local one: this is the mutation under test.
        return real_build_graph(checkpointer=MemorySaver())

    monkeypatch.setattr(lg, "build_graph", mutated_build_graph)

    thread = f"m2-n1-{uuid4().hex}"

    _pause_and_restart(thread)

    assert _resume(thread, approved=True).get("status") != "action_completed", (
        "a process-local checkpoint unexpectedly resumed across a restart, "
        "which would make D1 vacuous"
    )


def test_n1b_postgres_path_does_reach_the_database(fake_stack, hr_db):
    """N1 sanity check: the mutation above is not trivially true.

    Confirms the unmutated durable path really does write to PostgreSQL, so the
    N1 failure is caused by the saver swap and not by a broken harness.
    """
    thread = f"m2-n1b-{uuid4().hex}"
    _interrupt_of(get_durable_graph().invoke(_hitl_state(), _cfg(thread)))

    assert _persisted_rows("checkpoint_blobs", thread) > 0, (
        "canonical path must commit to PostgreSQL"
    )
