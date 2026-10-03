"""Prove the SERVED HITL path is durable in PostgreSQL, across OS processes.

Why this probe exists
---------------------
This repository previously proved that PostgreSQL checkpointing works
(docs/_p8_process_restart_probe.py) -- but it did so against
``maia.persistence.durable_checkpointer``, a helper with zero runtime callers.
The HTTP endpoint ``POST /agent/chat`` goes through
``maia.agent.langgraph_agent.get_durable_graph()`` instead. So the capability
existed and the served path did not use it. That gap is what this probe closes.

So this probe deliberately calls the same two functions the endpoint calls:

    get_durable_graph()        -- what api.py:1468 calls
    resume_from_approval()     -- what the approval-confirm endpoint calls

Nothing here reaches around the production construction path. If the served
graph is swapped back to SqliteSaver or MemorySaver, this probe fails, which is
the point: it is a control, not a demonstration.

Two phases, two processes
-------------------------
    write   process A: drive to the HITL interrupt, then exit
    read    process B: resume the SAME thread and approve

Process A is gone before B starts. A same-process assertion could not tell
"Postgres persisted it" apart from "the object was still in memory", which is
exactly the confusion this is meant to eliminate.

Usage:

    DATABASE_URL=postgresql://... python docs/_p9_served_path_probe.py write
    DATABASE_URL=postgresql://... python docs/_p9_served_path_probe.py read

MAIA_PROBE_STATE points at a temp file carrying the thread id and the writer
PID between the two runs. Nothing is shared through Python memory.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

STATE_ENV = "MAIA_PROBE_STATE"
WRITER_PID_ENV = "MAIA_PROBE_WRITER_PID"

# Deterministic question that routes to node_propose_action -> interrupt().
HITL_QUESTION = "Tôi muốn xin nghỉ 2 ngày từ 15/09"


def _require_dsn() -> None:
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print(
            "RESULT: FAIL (DATABASE_URL unset -- the served path must fail "
            "closed rather than fall back to SQLite)",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _stub_stack():
    """Neutralise the model stack so the probe depends only on durability.

    ``propose_action`` is reachable only when ``verify_grounding`` marks the run
    grounded, which a bare process never is: the probe would fail at routing
    long before touching a checkpoint, and such a failure says nothing about
    durability. Stubbing retrieval/generation makes the run reach the HITL
    interrupt deterministically, which is what this probe is actually about.
    """
    from maia.agent import langgraph_agent as lg
    from maia.config import settings

    # Pin the legacy retrieval path. With the default LlamaIndex data plane,
    # node_retrieve returns before _build_stack is ever consulted, so the stub
    # below is ignored, used_chunks stays empty, has_evidence is never set and
    # the graph routes to finalize instead of propose_action. The probe would
    # then fail at routing and say nothing about the checkpoint.
    settings.LLAMA_INDEX_DATA_PLANE = False

    text = "M9 served-path probe stub (synthetic, no business content)."

    class _Retriever:
        def retrieve(self, question, tenant_id=None, session_id=None):
            return [{
                "chunk_id": "p9-c1", "text": text,
                "score": 0.9, "fused_score": 0.05,
                "metadata": {"filename": "p9_stub.md", "tenant_id": tenant_id},
            }]

    class _Reranker:
        mode = "fallback"

        def rerank(self, query, candidates, top_k=3):
            for c in candidates:
                c["rerank_score"] = float(c.get("fused_score", 0.0))
            return sorted(
                candidates, key=lambda x: x["rerank_score"], reverse=True
            )[:top_k]

    class _LLM:
        # `mode` is read into state.llm_mode and drives the grounded-answer path.
        # Without it the run reaches verify_grounding with a non-grounded answer,
        # never gets has_evidence, and never routes to propose_action -- so the
        # probe would fail at routing instead of at the checkpoint.
        mode = "mock"

        def chat(self, messages):
            return text + " [S1]"

    retr, rank, llm = _Retriever(), _Reranker(), _LLM()
    lg._build_stack = lambda tenant_id=None: (None, None, retr, rank, llm)


def _write() -> None:
    """Process A: reach the HITL interrupt through the served construction path."""
    _require_dsn()
    _stub_stack()

    from maia.agent.langgraph_agent import AgentState, get_durable_graph

    thread_id = f"served-probe-{uuid.uuid4().hex[:12]}"

    # Exactly what api.py does for a non-resume, non-stream request.
    graph = get_durable_graph()
    result = graph.invoke(
        AgentState(
            question=HITL_QUESTION,
            session_id="served-path-probe",
            tenant_id="probe-tenant",
            employee_id="probe-emp",
        ),
        {"configurable": {"thread_id": thread_id}},
    )

    interrupts = result.get("__interrupt__")
    if not interrupts:
        print(
            "RESULT: FAIL (graph did not pause for approval; nothing to resume)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    proposal = interrupts[0].value
    state_file = os.environ.get(STATE_ENV)
    if state_file:
        Path(state_file).write_text(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "tool": proposal.get("tool"),
                    "pid": os.getpid(),
                }
            )
        )
    print(json.dumps({"wrote": True, "thread_id": thread_id, "tool": proposal.get("tool")}))


def _read() -> None:
    """Process B: rebuild the graph and resume the thread process A left behind."""
    _require_dsn()
    _stub_stack()

    state_file = os.environ.get(STATE_ENV)
    if not state_file or not Path(state_file).is_file():
        print("RESULT: FAIL (no writer state)", file=sys.stderr)
        raise SystemExit(1)
    prior = json.loads(Path(state_file).read_text())

    writer_pid = int(prior["pid"])
    os.environ[WRITER_PID_ENV] = str(writer_pid)

    # A different OS process must actually own this checkpoint recovery.
    processes_differ = os.getpid() != writer_pid
    if not processes_differ:
        print("RESULT: FAIL (reader shares the writer's PID)", file=sys.stderr)
        raise SystemExit(1)

    from maia.agent.langgraph_agent import close_durable_graph, resume_from_approval

    # Force a cold build: nothing may be served from a cached graph.
    close_durable_graph()

    result = resume_from_approval(prior["thread_id"], approved=True, employee_id="probe-emp")

    status = result.get("status")
    resumed_correctly = status == "action_completed"
    interrupted_again = bool(result.get("__interrupt__"))

    print(
        json.dumps(
            {
                "processes_differ": processes_differ,
                "reader_pid": os.getpid(),
                "writer_pid": writer_pid,
                "status": status,
                "tool_was": prior.get("tool"),
            }
        )
    )

    if not (resumed_correctly and not interrupted_again):
        print(
            f"RESULT: FAIL (resume did not complete the action; status={status!r})",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print("RESULT: PASS")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "write":
        _write()
    elif mode == "read":
        _read()
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()