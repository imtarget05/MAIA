"""Tests for the router-delegation team (agent/team).

Covers the plan: routing correctness, delegation result, hop guard,
approval-gate never bypassed, unknown-intent fallback, soft timeout.
Fully offline (hash embedder + in-memory store + MOCK LLM).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.agent import EnterpriseAgent
from maia.agent.team import (
    AgentMessage,
    HRAgent,
    ITAgent,
    KnowledgeAgent,
    RouterAgent,
    TeamBus,
    TeamOrchestrator,
    route_for_intent,
)
from maia.agent.team.base import TeamAgent
from maia.chunking import Chunk
from maia.config import settings
from maia.embeddings import Embedder
from maia.llm import CloudflareLLM
from maia.reranker import Reranker
from maia.retriever import HybridRetriever
from maia.stream.store import InMemoryVectorStore


def _stack(chunks):
    embedder = Embedder()
    store = InMemoryVectorStore()
    for ch in chunks:
        vec = embedder.embed([ch.text])[0]
        store.upsert_one(ch.metadata["chunk_id"], vec,
                         {"chunk_id": ch.metadata["chunk_id"], "text": ch.text,
                          **{k: v for k, v in ch.metadata.items() if k != "chunk_id"}})
    retriever = HybridRetriever(store, embedder, storage_dir="/tmp/maia_test_team")
    return EnterpriseAgent(embedder, store, retriever, Reranker(), CloudflareLLM())


def _leave_chunks():
    return [
        Chunk(text="Leave Policy: each employee has 12 annual leave days. Request via MAIA.",
              metadata={"chunk_id": "lp1", "filename": "Leave_Policy.md"}),
        Chunk(text="IT Security Policy v4.2 Section 7.1: report lost device within 1 hour.",
              metadata={"chunk_id": "sec1", "filename": "IT_Security_Policy_v4.2.md"}),
    ]


def _orch(agent):
    bus = TeamBus()
    members = {"router": RouterAgent(bus),
               "knowledge": KnowledgeAgent(bus, agent),
               "hr": HRAgent(bus, agent),
               "it": ITAgent(bus, agent)}
    return TeamOrchestrator(members=members, bus=bus)


def test_route_for_intent():
    assert route_for_intent("leave_request") == "hr"
    assert route_for_intent("leave_balance") == "hr"
    assert route_for_intent("hr_policy") == "hr"
    assert route_for_intent("it_help") == "it"
    assert route_for_intent("vpn") == "it"
    assert route_for_intent("security") == "it"
    assert route_for_intent("general") == "knowledge"


def test_router_routes_hr_question():
    bus = TeamBus()
    router = RouterAgent(bus)
    task = bus.post("orchestrator", "router", "task",
                    {"question": "Chính sách nghỉ phép như thế nào?"})
    out = router.handle(task)
    assert out.kind == "result"
    assert out.payload["route"] == "hr"
    assert out.payload["intent"] == "hr_policy"


def test_router_routes_it_question():
    bus = TeamBus()
    router = RouterAgent(bus)
    task = bus.post("orchestrator", "router", "task",
                    {"question": "Tôi làm mất laptop công ty. Tôi cần làm gì?"})
    out = router.handle(task)
    assert out.payload["route"] == "it"


def test_router_empty_question_falls_back():
    bus = TeamBus()
    router = RouterAgent(bus)
    task = bus.post("orchestrator", "router", "task", {"question": "   "})
    out = router.handle(task)
    assert out.payload["route"] == "knowledge"


def test_delegation_hr_end_to_end():
    orch = _orch(_stack(_leave_chunks()))
    res = orch.run("Chính sách nghỉ phép như thế nào?", session_id="team_t1")
    assert res["member"] == "hr"
    assert res["route"] == "hr"
    assert res["status"] in ("answered", "insufficient_evidence")
    assert res["hops"] == 1
    assert res["trace_id"]
    assert isinstance(res["team_trace"], list) and len(res["team_trace"]) >= 2


def test_unknown_intent_falls_back_to_knowledge():
    orch = _orch(_stack(_leave_chunks()))
    res = orch.run("Xin chào", session_id="team_t2")
    assert res["route"] == "knowledge"
    assert res["member"] == "knowledge"


def test_approval_gate_never_bypassed():
    import tempfile
    tmp = tempfile.mktemp(suffix=".json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp
    try:
        orch = _orch(_stack(_leave_chunks()))
        res = orch.run("Tôi muốn xin nghỉ phép 2 ngày từ 15/09",
                       session_id="team_t3", employee_id="emp_team_test")
        # side effect must stay proposed, never executed by the team
        assert res["status"] == "needs_approval"
        assert res["pending_action"]["type"] == "create_leave_request"
        assert res["action"] is None
        # HR DB file untouched: no request persisted without confirm
        assert not Path(tmp).exists()
    finally:
        settings.HR_MOCK_DB_PATH = old
        try:
            Path(tmp).unlink()
        except OSError:
            pass


def test_hop_guard_stops_reroute_loop():
    class AlwaysOutOfScope(TeamAgent):
        from maia.agent.team.base import AgentRole
        role = AgentRole(name="hr", description="stub")

        def handle(self, msg: AgentMessage):
            return self._reply(msg, "result", {"status": "out_of_scope",
                                               "member": "hr", "intent": "hr_policy"})

    class KnowledgeStub(TeamAgent):
        from maia.agent.team.base import AgentRole
        role = AgentRole(name="knowledge", description="stub")

        def handle(self, msg: AgentMessage):
            return self._reply(msg, "result", {"status": "out_of_scope",
                                               "member": "knowledge"})

    bus = TeamBus()
    members = {"router": RouterAgent(bus), "hr": AlwaysOutOfScope(bus),
               "knowledge": KnowledgeStub(bus),
               "it": ITAgent(bus, _stack(_leave_chunks()))}
    orch = TeamOrchestrator(members=members, bus=bus, max_hops=1)
    res = orch.run("Chính sách nghỉ phép như thế nào?", session_id="team_t4")
    assert res["status"] == "error"
    assert res["hops"] <= 1
    assert len(res["team_trace"]) >= 2


def test_soft_timeout_returns_error():
    orch = _orch(_stack(_leave_chunks()))
    orch.timeout_sec = 0  # any elapsed time exceeds the budget
    res = orch.run("Chính sách nghỉ phép như thế nào?", session_id="team_t5")
    assert res["status"] == "error"
    assert "hết thời gian" in res["answer"]


def test_missing_router_errors_cleanly():
    bus = TeamBus()
    orch = TeamOrchestrator(members={}, bus=bus)
    res = orch.run("hello", session_id="team_t6")
    assert res["status"] == "error"
