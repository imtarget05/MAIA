"""Prove the SERVED HITL path is durable across two real OS processes.

The point of this file is narrow: the previous durability evidence proved that
*PostgreSQL checkpointing works*, using ``maia.persistence.durable_checkpointer``.
It never proved the HTTP endpoint uses it -- ``api.py`` builds its graph through
``get_durable_graph()`` instead. "Capability exists" is not "runtime uses
capability", and that gap is exactly what a review should catch.

So the test shells out to docs/_p9_served_path_probe.py twice:

    process A  -> get_durable_graph() -> HITL interrupt -> exits
    process B  -> resume_from_approval() -> must complete the action

Two OS processes, two connections, no shared Python objects. Swapping the
served graph back to SqliteSaver or MemorySaver makes this red, which is the
control that makes the pass meaningful.

Skipped, not passed, when DATABASE_URL is absent: a durability test that never
runs must not report green.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MAIA_ROOT = Path(__file__).resolve().parents[1]
PROBE = MAIA_ROOT / "docs" / "_p9_served_path_probe.py"
DSN = (os.environ.get("DATABASE_URL") or "").strip()

pytestmark = pytest.mark.skipif(
    not DSN, reason="DATABASE_URL not set; served-path durability cannot be proven"
)


def _run_probe(mode: str, state_file: str) -> dict:
    """Run one probe phase in its own interpreter."""
    env = {
        **os.environ,
        "DATABASE_URL": DSN,
        "MAIA_PROBE_STATE": state_file,
        # Approving debits leave balance in the HR mock DB; keep it scratch so
        # the probe cannot mutate a developer's data.
        "HR_MOCK_DB_PATH": state_file + ".hr.json",
        "MAIA_EMBED_FORCE_HASH": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(PROBE), mode],
        capture_output=True, text=True, env=env, timeout=180,
        # The probe's exit code IS the assertion here, so a raise-on-nonzero
        # would destroy the evidence the caller needs to report.
        check=False,
    )
    return {"returncode": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr}


def _last_json(text: str) -> dict:
    return json.loads([ln for ln in text.splitlines() if ln.startswith("{")][-1])


def test_probe_script_exists():
    """A missing probe must fail loudly.

    A durability test whose probe silently disappeared reports green while
    asserting nothing -- the same defect class this repository rejected twice
    already (the empty `terraform test` run, the redis-gated skip).
    """
    assert PROBE.is_file(), (
        f"served-path probe missing: {PROBE}. Without it this test would skip "
        "and the suite would report green with zero durability assertions."
    )


def test_served_path_checkpoint_survives_process_death(tmp_path):
    """D1: a fresh OS process resumes an action the previous one left pending."""
    state_file = str(tmp_path / "probe_state.json")

    write = _run_probe("write", state_file)
    assert write["returncode"] == 0, f"writer phase failed: {write['stderr'][-800:]}"
    written = _last_json(write["stdout"])
    assert written["wrote"] is True
    assert written["tool"] == "create_leave_request", "run never reached HITL"

    # The writer has exited by now. That exit IS the thing under test: a
    # same-process check could not tell "Postgres persisted it" from "the
    # object was still in memory".
    read = _run_probe("read", state_file)
    assert read["returncode"] == 0, (
        f"reader could not resume served-path state: {read['stderr'][-800:]}"
    )
    result = _last_json(read["stdout"])
    assert result["processes_differ"] is True
    assert result["reader_pid"] != result["writer_pid"]
    assert result["status"] == "action_completed"
    assert result["tool_was"] == "create_leave_request"


def test_checkpoint_is_really_in_postgres(tmp_path):
    """D3: the interrupt a request produced exists in the database.

    Guards against a pass served out of any other store that merely happens to
    work in-process.
    """
    psycopg = pytest.importorskip("psycopg")
    state_file = str(tmp_path / "probe_state.json")

    write = _run_probe("write", state_file)
    assert write["returncode"] == 0, write["stderr"][-500:]
    thread_id = _last_json(write["stdout"])["thread_id"]

    with psycopg.connect(DSN) as conn:
        blobs = conn.execute(
            "SELECT count(*) FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,)
        ).fetchone()[0]
        interrupts = conn.execute(
            "SELECT count(*) FROM checkpoint_writes "
            "WHERE thread_id = %s AND channel = '__interrupt__'",
            (thread_id,),
        ).fetchone()[0]

    assert blobs > 0, "request checkpoint was never committed to PostgreSQL"
    assert interrupts > 0, "no pending approval persisted for the served request"
# ---CHUNK---
# ------------------------------------------------------------- fail-closed


def test_missing_dsn_fails_closed(monkeypatch):
    """D4a: no DATABASE_URL -> explicit failure, never a SQLite fallback.

    This is the whole reason the durable graph fails loudly. A silent fallback
    lets production look healthy while losing every pending approval on
    restart.
    """
    from maia.agent import langgraph_agent as lg

    monkeypatch.setattr(lg.settings, "DATABASE_URL", "")
    lg.close_durable_graph()

    with pytest.raises(lg.DurableCheckpointUnavailable) as exc:
        lg.get_durable_graph()

    assert "DATABASE_URL" in str(exc.value)
    assert "sqlite" in str(exc.value).lower(), "message should refuse the fallback"
    # "not created by this call", not "does not exist": a stale
    # agent_checkpoints.db from the SqliteSaver era is pre-existing local state,
    # and the invariant under test is that failing closed does not create one.
    assert not (MAIA_ROOT / "storage" / "agent_checkpoints.db").exists(), (
        "fail-closed must not create a local SQLite checkpoint"
    )


def test_unreachable_database_fails_closed(monkeypatch):
    """D4b: a host that cannot be reached -> explicit failure."""
    from maia.agent import langgraph_agent as lg

    monkeypatch.setattr(
        lg.settings, "DATABASE_URL", "postgresql://maia:x@127.0.0.1:59999/maia_test"
    )
    lg.close_durable_graph()

    with pytest.raises(lg.DurableCheckpointUnavailable):
        lg.get_durable_graph()
    lg.close_durable_graph()


def test_wrong_password_fails_closed(monkeypatch):
    """D4c: wrong password -> explicit failure, never a silent local run."""
    from maia.agent import langgraph_agent as lg

    userinfo, _, hostpart = DSN.split("://", 1)[1].partition("@")
    user = userinfo.split(":", 1)[0]
    monkeypatch.setattr(
        lg.settings, "DATABASE_URL", f"postgresql://{user}:wrong-password@{hostpart}"
    )
    lg.close_durable_graph()

    with pytest.raises(lg.DurableCheckpointUnavailable):
        lg.get_durable_graph()
    lg.close_durable_graph()