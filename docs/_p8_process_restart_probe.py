#!/usr/bin/env python3
"""
NOT THE CANONICAL DURABILITY PROBE.

This script exercises `maia.persistence.durable_checkpointer`
(`AsyncPostgresSaver`), which has no runtime caller. It proves PostgreSQL
checkpointing works; it does NOT prove the served `POST /agent/chat` path uses
it, because that endpoint builds its graph through
`langgraph_agent.get_durable_graph()`.

Kept because tests/test_persistence.py drives it, and because it stays the
async reference if the API ever moves to async execution end-to-end.

The served-path proof is `docs/_p9_served_path_probe.py`, which calls the same
two functions the endpoint calls.

Original module docstring follows.
--------------------------------------------------------------------
"""
"""
Process-restart probe for durable agent state (tier 1: thread/checkpoint).

WHY THIS EXISTS AS A SEPARATE SCRIPT
------------------------------------
`tests/test_persistence.py::test_state_persists_across_two_sequential_processes`
spawns this file twice as two independent OS processes. In-process assertions
cannot demonstrate durability: a module-level dict, a LangGraph `MemorySaver`,
or an open SQLite handle would all "pass" a same-process test while proving
nothing about what survives the process dying. The only honest way to test
"state outlives the process" is to have the process actually exit and a
second, unrelated interpreter read the state back.

It was previously SKIPPING because this file did not exist — a green test
observing nothing, which is the same defect class this repository has already
rejected once (see the empty `terraform test` run and the Redis-absent skip in
tests/test_distributed_state.py). The skip was silent, so the suite reported
PASS while executing zero durability assertions.

USAGE (the test does this for you; this is for manual reproduction)
    MAIA_POSTGRES_DSN='postgresql://...' python docs/_p8_process_restart_probe.py write
    MAIA_POSTGRES_DSN='postgresql://...' python docs/_p8_process_restart_probe.py read

`read` prints a JSON verdict and asserts on it, exiting non-zero on failure so
a broken restart fails the build instead of skipping.

This deliberately exercises the raw checkpointer contract — write a value,
exit, reopen, read it back — rather than a full agent run. The thing under
test is "does Postgres hold this across a process boundary", which is a
persistence property, not an agent-behaviour property.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

DSN = os.environ["MAIA_POSTGRES_DSN"]
# These are deliberately NOT module-level constants resolved from the
# environment at import time. The reader learns the thread id and marker from
# the writer's state file inside main(), which runs long after import — so a
# module-level `os.environ.get(...)` would silently capture a freshly generated
# random id and the reader would query a thread that was never written. That
# bug is invisible in the output (it looks like "state did not persist") when
# it is really "the probe read the wrong key", so the values are passed
# explicitly instead.
DEFAULT_SENTINEL = f"sentinel-{uuid.uuid4().hex}"
# Bounded, so a dead database produces a clear failure instead of a hang.
CONNECT_TIMEOUT_S = 10.0


async def _with_saver(fn):
    """Run `fn(saver)` against a freshly-opened Postgres checkpointer.

    Uses the same `pool.connection()` context manager as
    src/maia/persistence.py so the probe exercises the real production
    pattern rather than a parallel one that could drift from it.

    `wait=True` with a bounded timeout is REQUIRED, not cosmetic. Calling
    `await pool.open()` with no arguments returns before the pool holds a live
    connection, and the first checkout then blocks indefinitely with no
    timeout — measured during development: the probe hung past 120s and the
    test only failed because pytest's own timeout fired. A durability probe
    that hangs is worse than one that fails, because it converts a
    connectivity problem into a mysterious timeout.
    """
    pool = AsyncConnectionPool(
        DSN, min_size=1, max_size=2, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open(wait=True, timeout=CONNECT_TIMEOUT_S)
    try:
        # `connection()` returns the connection to the pool on exit, so
        # `pool.close()` cannot block waiting for a connection that was
        # checked out and never returned.
        async with pool.connection(timeout=CONNECT_TIMEOUT_S) as conn:
            saver = AsyncPostgresSaver(conn)
            await saver.setup()
            return await fn(saver)
    finally:
        await pool.close()


async def _write(thread_id: str, marker: str) -> None:
    """Drive a REAL compiled LangGraph graph, then exit with the process.

    Deliberately not a hand-built `aput(...)` call. `aput` requires a full
    `Checkpoint` plus `ChannelVersions`, and hand-assembling one would test an
    understanding of LangGraph's internal tuple format rather than whether MAIA
    state is durable. Compiling the production graph means the persisted state
    is the same shape the application actually produces.

    The marker is carried in `question`, a real `AgentState` field. An earlier
    attempt invented a `sentinel` key, which LangGraph silently dropped because
    it is not in the state schema — so the write "succeeded" while persisting
    nothing that the reader could find. Asserting on a field the schema
    actually declares is what makes this a test rather than a tautology.
    """
    from maia.agent.langgraph_agent import build_graph

    async def run(saver):
        graph = build_graph(checkpointer=saver)
        config = {"configurable": {"thread_id": thread_id}}
        await graph.ainvoke(
            {"question": marker, "session_id": "durability-probe",
             "tenant_id": "probe-tenant"},
            config,
        )

    await _with_saver(run)
    print(json.dumps({"wrote": True, "thread_id": thread_id, "value": marker}))


async def _read(thread_id: str, expected: str) -> dict:
    """Recover the state the previous process wrote, using the graph's own API.

    `aget_state` is the same call the application uses to inspect a thread, so
    this asserts the state is not merely present in a database row but is
    actually readable as agent state by a fresh process.
    """
    from maia.agent.langgraph_agent import build_graph

    async def get(saver):
        graph = build_graph(checkpointer=saver)
        return await graph.aget_state({"configurable": {"thread_id": thread_id}})

    snapshot = await _with_saver(get)

    values = dict(snapshot.values) if snapshot.values else {}
    nexts = tuple(snapshot.next) if snapshot.next else ()

    # A fresh process is by construction a different interpreter with a
    # different PID and no shared memory with the writer.
    processes_differ = os.getpid() != int(os.environ["MAIA_PROBE_WRITER_PID"])
    state_survived_restart = bool(values)
    # The decisive assertion: the exact marker the dead process wrote is the
    # value this new process reads back. A row that merely EXISTS is not
    # durability; the payload has to survive intact.
    resumed_correctly = values.get("question") == expected

    verdict = {
        "thread_id": thread_id,
        "processes_differ": processes_differ,
        "state_survived_restart": state_survived_restart,
        "pending_tasks": list(nexts),

        "resumed_correctly": resumed_correctly,
    }
    print(json.dumps(verdict))
    if not (processes_differ and state_survived_restart and resumed_correctly):
        print("RESULT: FAIL", file=sys.stderr)
        raise SystemExit(1)
    print("RESULT: PASS")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "write":
        thread_id = f"probe-{uuid.uuid4().hex[:12]}"
        marker = DEFAULT_SENTINEL
        state_file = os.environ.get("MAIA_PROBE_STATE")
        if state_file:
            # Record the thread id, the marker and the writer PID so the
            # reader can prove the two runs really are different processes and
            # is looking for exactly what was written.
            Path(state_file).write_text(json.dumps({
                "thread_id": thread_id, "value": marker, "pid": os.getpid(),
            }))
        asyncio.run(_write(thread_id, marker))
    elif mode == "read":
        state_file = os.environ.get("MAIA_PROBE_STATE")
        if not state_file or not Path(state_file).is_file():
            print("RESULT: FAIL (no writer state)", file=sys.stderr)
            raise SystemExit(1)
        prior = json.loads(Path(state_file).read_text())
        os.environ["MAIA_PROBE_WRITER_PID"] = str(prior["pid"])
        asyncio.run(_read(prior["thread_id"], prior["value"]))
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
