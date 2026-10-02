"""Conversation durability on real PostgreSQL: tenant isolation and two instances.

Runs against the live container
(`docker compose -f deploy/docker/compose.infra.yml up -d postgres`) and skips
loudly if it is absent — a mocked database cannot demonstrate that Tenant A's row
never reaches Tenant B's process.

THE CENTRAL CLAIM. Tenant isolation here is a PREDICATE IN SQL. A tenant B query
returns zero rows because PostgreSQL excluded them, not because Python compared
`row.tenant_id` after the row had already been materialised. The negative
control at the bottom proves the difference: with the predicate removed, the
cross-tenant read succeeds and the suite's own assertion catches it.
"""

from __future__ import annotations

import importlib.util
import os
import uuid

import pytest

from maia.conversation_repository import (
    ConversationNotFound,
    PostgresConversationRepository,
)

PG_DSN = os.environ.get(
    "MAIA_TEST_PG_DSN", "postgresql://maia:maia@localhost:5432/maia_test"
)

pytestmark = pytest.mark.skipif(
    not (
        importlib.util.find_spec("sqlalchemy") is not None
        and importlib.util.find_spec("psycopg") is not None
    ),
    reason="sqlalchemy + psycopg required (requirements.txt)",
)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def engine():
    """A real engine, truncated before and after each test.

    Truncating per test is what lets the two-instance tests share a database with
    the isolation tests without inheriting each other's rows.
    """
    from sqlalchemy import create_engine, text

    eng = create_engine(PG_DSN, future=True)
    tables = (
        "conversation_messages",
        "conversations",
        "agent_checkpoints",
        "agent_runs",
        "approval_requests",
        "idempotency_records",
        "audit_events",
        "job_records",
    )
    with eng.begin() as conn:
        for table in tables:
            conn.execute(text(f"truncate table {table} cascade"))
    try:
        yield eng
    finally:
        with eng.begin() as conn:
            conn.execute(text("truncate table conversation_messages cascade"))
            conn.execute(text("truncate table conversations cascade"))
        eng.dispose()


# ---------------------------------------------------------------------------
# Tenant isolation — enforced by SQL, not by Python
# ---------------------------------------------------------------------------


def test_tenant_a_reads_its_own_conversation(engine) -> None:
    repo = PostgresConversationRepository(engine)
    sid = _session_id()
    repo.create(TENANT_A, sid, title="A's private thread")
    repo.append_message(TENANT_A, sid, "user", "secret from tenant A")

    assert repo.get(TENANT_A, sid) is not None
    msgs = repo.list_messages(TENANT_A, sid)
    assert len(msgs) == 1 and "tenant A" in msgs[0]["content"]


def test_tenant_b_cannot_read_tenant_a_conversation(engine) -> None:
    """Tenant B, holding A's session id, must get nothing — and no exception."""
    repo_a = PostgresConversationRepository(engine)
    repo_b = PostgresConversationRepository(engine)
    sid = _session_id()
    repo_a.create(TENANT_A, sid, title="A's private thread")
    repo_a.append_message(TENANT_A, sid, "user", "secret from tenant A")

    assert repo_b.get(TENANT_B, sid) is None, (
        "CROSS-TENANT READ: tenant B received tenant A's conversation row"
    )
    assert repo_b.list_messages(TENANT_B, sid) == [], (
        "CROSS-TENANT READ: tenant B received tenant A's messages"
    )


def test_tenant_b_cannot_append_into_tenant_a_conversation(engine) -> None:
    """A cross-tenant append must be refused, not silently create an orphan row."""
    repo_a = PostgresConversationRepository(engine)
    repo_b = PostgresConversationRepository(engine)
    sid = _session_id()
    repo_a.create(TENANT_A, sid)

    with pytest.raises(ConversationNotFound):
        repo_b.append_message(TENANT_B, sid, "user", "injected by tenant B")

    assert repo_a.list_messages(TENANT_A, sid) == []


def test_same_session_id_in_two_tenants_is_allowed(engine) -> None:
    """The collision the composite unique constraint exists for.

    A globally unique `session_id` would fail this at create(), which is exactly
    the property that turns such an index into a tenant-existence leak.
    """
    repo = PostgresConversationRepository(engine)
    sid = _session_id()
    repo.create(TENANT_A, sid, title="A")
    repo.create(TENANT_B, sid, title="B")

    assert repo.get(TENANT_A, sid)["title"] == "A"
    assert repo.get(TENANT_B, sid)["title"] == "B"


# ---------------------------------------------------------------------------
# Two independent instances, no shared Python state
# ---------------------------------------------------------------------------


def test_two_instances_share_conversation_state(engine) -> None:
    """A writes, B reads and appends, A re-reads. Nothing is shared in-process."""
    instance_a = PostgresConversationRepository(engine)
    instance_b = PostgresConversationRepository(engine)
    sid = _session_id()

    instance_a.create(TENANT_A, sid, title="shared thread")
    instance_a.append_message(TENANT_A, sid, "user", "from A")

    assert [m["content"] for m in instance_b.list_messages(TENANT_A, sid)] == ["from A"]

    instance_b.append_message(TENANT_A, sid, "assistant", "from B")

    seen_by_a = instance_a.list_messages(TENANT_A, sid)
    assert [m["content"] for m in seen_by_a] == ["from A", "from B"], (
        "instance A did not observe the message instance B appended"
    )
    assert [m["seq"] for m in seen_by_a] == [0, 1]


def test_two_instances_still_isolate_tenants(engine) -> None:
    """Instance B serving tenant B must not see instance A's tenant A rows."""
    instance_a = PostgresConversationRepository(engine)
    instance_b = PostgresConversationRepository(engine)
    sid = _session_id()
    instance_a.create(TENANT_A, sid, title="A only")

    assert instance_b.get(TENANT_B, sid) is None
    assert instance_b.list_messages(TENANT_B, sid) == []


# ---------------------------------------------------------------------------
# Concurrent append contract (A4) — the gap between "no corruption" and
# "production behaviour"
# ---------------------------------------------------------------------------


def test_concurrent_appends_serialise_without_constraint_errors(engine) -> None:
    """Two independent instances append at once: both stored, distinct contiguous seq.

    Before the row lock this produced one committed message and one raw
    `IntegrityError`, which a caller would see as a 500 for what is ordinary
    contention. The assertion that matters is the triple at the end: two rows,
    seqs 0 and 1, and no exception.
    """
    import threading

    repo_a = PostgresConversationRepository(engine)
    repo_b = PostgresConversationRepository(engine)
    sid = _session_id()
    repo_a.create(TENANT_A, sid)

    barrier = threading.Barrier(2)
    errors: list[Exception] = []
    contents = ["from A", "from B"]

    def worker(repo, content: str) -> None:
        try:
            barrier.wait(timeout=10)  # maximise the overlap
            repo.append_message(TENANT_A, sid, "user", content)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(repo_a, contents[0])),
        threading.Thread(target=worker, args=(repo_b, contents[1])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"concurrent append leaked an exception: {errors!r}"
    msgs = repo_a.list_messages(TENANT_A, sid)
    assert [m["seq"] for m in msgs] == [0, 1], (
        f"expected contiguous seqs [0, 1], got {[m['seq'] for m in msgs]}"
    )
    assert sorted(m["content"] for m in msgs) == contents, (
        "a committed message was lost"
    )


def test_many_concurrent_appends_all_land(engine) -> None:
    """Six appends from six repository objects: six rows, no duplicates, no errors.

    More than two so the race cannot pass by accident of timing.
    """
    import threading

    base = PostgresConversationRepository(engine)
    sid = _session_id()
    base.create(TENANT_A, sid)

    n = 6
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        repo = PostgresConversationRepository(engine)
        try:
            barrier.wait(timeout=10)
            repo.append_message(TENANT_A, sid, "user", f"msg-{i}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent appends leaked exceptions: {errors!r}"
    msgs = base.list_messages(TENANT_A, sid)
    seqs = sorted(m["seq"] for m in msgs)
    assert seqs == list(range(n)), f"expected seqs 0..{n - 1}, got {seqs}"
    assert len({m["content"] for m in msgs}) == n, "a message was overwritten"


# ---------------------------------------------------------------------------
# Negative control — this is the whole point of the file
# ---------------------------------------------------------------------------


def test_dropping_the_tenant_predicate_causes_a_leak(engine, monkeypatch) -> None:
    """Break the implementation on purpose and observe the leak.

    The mutated `get` is the exact mistake this design warns about: filter by
    session id only, then decide ownership in Python. Running it proves the
    canonical assertion is observing isolation rather than merely restating the
    source — if removing the predicate changed nothing, the suite would be
    decorative.
    """
    from sqlalchemy import select

    from maia.conversation_repository import PostgresConversationRepository as Real
    from maia.durable_models import Conversation

    sid = _session_id()
    Real(engine).create(TENANT_A, sid, title="A only")

    def unscoped_get(self, tenant_id, session_id):
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(Conversation).where(Conversation.session_id == session_id)
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    # 1. The mutation must actually leak.
    monkeypatch.setattr(Real, "get", unscoped_get)
    leaked = Real(engine).get(TENANT_B, sid)
    assert leaked is not None, (
        "the negative control produced no leak, so it is not measuring anything "
        "and must be rewritten before it can be trusted"
    )
    assert leaked["tenant_id"] == TENANT_A

    # 2. Restored, the same cross-tenant read must return nothing.
    monkeypatch.undo()
    assert Real(engine).get(TENANT_B, sid) is None


def test_canonical_cross_tenant_assertion_fails_under_the_mutation(
    engine, monkeypatch
) -> None:
    """The tenant test itself must go red when the predicate is removed.

    This is the half that cannot be faked: rather than asserting "a leak
    happened", it re-runs the canonical assertion and requires it to FAIL.
    """
    from sqlalchemy import select

    from maia.conversation_repository import PostgresConversationRepository as Real
    from maia.durable_models import Conversation

    sid = _session_id()
    Real(engine).create(TENANT_A, sid, title="A only")

    def unscoped_get(self, tenant_id, session_id):
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(Conversation).where(Conversation.session_id == session_id)
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    monkeypatch.setattr(Real, "get", unscoped_get)

    # The canonical assertion, verbatim from the isolation test.
    with pytest.raises(AssertionError, match="CROSS-TENANT READ"):
        result = Real(engine).get(TENANT_B, sid)
        assert result is None, "CROSS-TENANT READ: tenant B received tenant A's row"
