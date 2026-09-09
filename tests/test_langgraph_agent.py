"""Tests for the LangGraph control plane: nodes, routing, HITL interrupt/resume.

Covers:
- AgentState defaults + trace accumulation
- node_classify_query extracts intent + slots
- route_after_classify: general -> retrieve (not simple_answer)
- node_propose_action calls interrupt() with a proposal
- resume_from_approval: approved -> action_completed, rejected -> action_cancelled
- get_durable_graph returns a SqliteSaver-backed graph
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


# These tests exercise the legacy HybridRetriever path with a mocked stack;
# the LlamaIndex data-plane path has its own dedicated tests
# (tests/test_llama_index_dataplane.py). Pin the flag OFF regardless of the
# config default so the default flip (WS2) doesn't break the legacy fixtures.
@pytest.fixture(autouse=True)
def _legacy_retrieval_path(monkeypatch):
    from maia.config import settings
    monkeypatch.setattr(settings, "LLAMA_INDEX_DATA_PLANE", False)


from maia.agent.langgraph_agent import (
    AgentState,
    build_graph,
    get_durable_graph,
    node_classify_query,
    route_after_classify,
)


def test_agent_state_defaults_and_trace():
    s = AgentState(question="hello", session_id="s1")
    assert s.status == "pending"
    assert s.intent == "general"
    assert s.node_trace == []
    s.trace("classify_query")
    s.trace("retrieve")
    assert s.node_trace == ["classify_query", "retrieve"]


def test_route_after_classify_general_goes_to_retrieve():
    s = AgentState(intent="general")
    assert route_after_classify(s) == "retrieve"


def test_route_after_classify_greeting_goes_to_simple_answer():
    s = AgentState(intent="greeting")
    assert route_after_classify(s) == "simple_answer"


def test_node_classify_query_extracts_intent_and_slots():
    s = AgentState(question="Tôi muốn xin nghỉ phép 2 ngày từ 15/09", session_id="s1")
    out = node_classify_query(s)
    assert out.intent == "leave_request"
    assert out.slots.get("days") == 2
    assert "classify_query" in out.node_trace


def test_node_propose_action_interrupts_with_proposal(fake_stack, monkeypatch):
    """propose_action must pause the graph — verified via a real graph invoke.

    NOTE: ``interrupt()`` can only run inside a LangGraph runnable context
    (calling ``node_propose_action()`` directly raises
    ``RuntimeError: Called get_config outside of a runnable context`` on
    langgraph>=1.0).  So this test drives the node through a compiled
    ``StateGraph`` and asserts the ``__interrupt__`` payload carries the
    proposal (state before interrupt is NOT committed to the checkpoint).
    """
    from langgraph.checkpoint.memory import MemorySaver
    retr, rerank, llm = fake_stack
    monkeypatch.setattr(lg, "_build_stack",
                        lambda tenant_id=None: (None, None, retr, rerank, llm))
    g = build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "ws1-direct-propose"}}
    res = g.invoke(
        AgentState(
            question="Tôi muốn xin nghỉ 2 ngày từ 15/09",
            session_id="s-direct",
            tenant_id="t1",
            employee_id="emp_h",
        ),
        cfg,
    )
    interrupts = res.get("__interrupt__")
    assert interrupts, "graph must pause at propose_action"
    val = interrupts[0].value if interrupts else None
    assert val is not None
    assert val["tool"] == "create_leave_request"


def test_resume_from_approval_approved_executes_tool(fake_stack, monkeypatch):
    """Full HITL cycle on a durable graph: interrupt -> approve -> action_completed."""
    tmp = tempfile.mkdtemp()
    import os
    os.environ["MAIA_STORAGE_DIR"] = tmp
    from maia.config import settings
    settings.STORAGE_DIR = tmp
    settings.HR_MOCK_DB_PATH = str(Path(tmp) / "hr_mock.json")
    settings.TOOL_TENANT_CHECK = False
    retr, rerank, llm = fake_stack
    monkeypatch.setattr(lg, "_build_stack",
                        lambda tenant_id=None: (None, None, retr, rerank, llm))
    import maia.agent.langgraph_agent as lg_mod
    lg_mod._durable_graph = None  # fresh SqliteSaver DB under new STORAGE_DIR

    g = get_durable_graph()
    thread_id = "test_tenant:resume_ok"
    config = {"configurable": {"thread_id": thread_id}}

    init_state = AgentState(
        question="Tôi muốn xin nghỉ phép 2 ngày từ 15/09",
        session_id="resume_ok",
        tenant_id="test_tenant",
        employee_id="emp_test",
    )
    try:
        g.invoke(init_state, config)
    except Exception:
        pass

    from langgraph.types import Command
    result = g.invoke(Command(resume={"approved": True, "employee_id": "emp_test"}), config)
    assert result["status"] == "action_completed"
    assert result["action_result"]["result"]["ok"] is True
    assert result["action_result"]["result"]["remaining_balance"] == 10


def test_resume_from_approval_rejected_cancels(fake_stack, monkeypatch):
    """Full HITL cycle: interrupt -> reject -> action_cancelled."""
    tmp = tempfile.mkdtemp()
    import os
    os.environ["MAIA_STORAGE_DIR"] = tmp
    from maia.config import settings
    settings.STORAGE_DIR = tmp
    settings.HR_MOCK_DB_PATH = str(Path(tmp) / "hr_mock.json")
    settings.TOOL_TENANT_CHECK = False
    retr, rerank, llm = fake_stack
    monkeypatch.setattr(lg, "_build_stack",
                        lambda tenant_id=None: (None, None, retr, rerank, llm))

    import maia.agent.langgraph_agent as lg_mod
    lg_mod._durable_graph = None  # fresh SqliteSaver DB under new STORAGE_DIR

    g = get_durable_graph()
    thread_id = "test_tenant:resume_rej"
    config = {"configurable": {"thread_id": thread_id}}

    init_state = AgentState(
        question="Tôi muốn xin nghỉ phép 1 ngày từ 15/09",
        session_id="resume_rej",
        tenant_id="test_tenant",
        employee_id="emp_test",
    )
    try:
        g.invoke(init_state, config)
    except Exception:
        pass

    from langgraph.types import Command
    result = g.invoke(Command(resume={"approved": False, "employee_id": "emp_test"}), config)
    assert result["status"] == "action_cancelled"


def test_build_graph_compiles_with_memory_saver():
    from langgraph.checkpoint.memory import MemorySaver
    g = build_graph(checkpointer=MemorySaver())
    assert g is not None

"""LangGraph control-plane tests (offline, no Qdrant/LLM creds needed).

Covers the compiled StateGraph with a faked retrieval/LLM stack:
- classify extracts slots and routes (general -> retrieve, greeting -> simple)
- full knowledge-query path yields answered + evidence + citations
- HITL propose_action pauses via interrupt; resume approved -> action_completed,
  resume rejected -> action_cancelled
- helpers _is_approved / _execute_tool
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from maia.agent import langgraph_agent as lg
from maia.agent.langgraph_agent import (
    _execute_tool,
    _is_approved,
    route_after_verify,
)
from maia.config import settings

POLICY_TEXT = (
    "Chinh sach nghi phep: nhan vien duoc nghi 12 ngay phep nam, "
    "don xin nghi can ghi ro so ngay va ngay bat dau."
)


class _FakeRetriever:
    def retrieve(self, question, tenant_id=None, session_id=None):
        return [
            {"chunk_id": "c1", "text": POLICY_TEXT,
             "score": 0.9, "fused_score": 0.05,
             "metadata": {"filename": "Leave_Policy.md", "tenant_id": tenant_id or "default"}},
            {"chunk_id": "c2", "text": "Huong dan VPN: lien he it-help@company.com de duoc ho tro.",
             "score": 0.2, "fused_score": 0.01,
             "metadata": {"filename": "VPN_Guide.md", "tenant_id": tenant_id or "default"}},
        ]


class _FakeReranker:
    mode = "fallback"

    def rerank(self, query, candidates, top_k=3):
        for c in candidates:
            c["rerank_score"] = float(c.get("fused_score", 0.0))
        return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)[:top_k]


class _FakeLLM:
    mode = "mock"

    def __init__(self, answer=None):
        self._answer = answer

    def chat(self, messages):
        if self._answer is not None:
            return self._answer
        # Grounded by construction: echo the policy text with a citation.
        return POLICY_TEXT + " [S1]"


@pytest.fixture()
def fake_stack(monkeypatch):
    retr, rerank, llm = _FakeRetriever(), _FakeReranker(), _FakeLLM()
    monkeypatch.setattr(
        lg, "_build_stack",
        lambda tenant_id=None: (None, None, retr, rerank, llm),
    )
    return retr, rerank, llm


def _invoke(graph, state, thread="t1"):
    return graph.invoke(state, {"configurable": {"thread_id": thread}})


# ---------------------------------------------------------------- classify/routing

def test_classify_full_slots_proposes_tool():
    s = AgentState(question="Tôi muốn xin nghỉ 5 ngày từ 10/09")
    out = node_classify_query(s)
    assert out.intent == "leave_request"
    assert out.slots["days"] == 5
    assert out.slots["start_date"] == "10/09"
    assert out.plan["tool"] == "create_leave_request"
    assert out.plan["requires_approval"] is True


def test_classify_missing_slots_no_retrieval():
    s = AgentState(question="Tôi muốn xin nghỉ phép")
    out = node_classify_query(s)
    assert out.intent == "leave_request"
    assert out.plan["tool"] is None
    assert out.plan["retrieve"] is False


def test_route_general_goes_to_retrieve():
    assert route_after_classify(AgentState(intent="general")) == "retrieve"
    assert route_after_classify(AgentState(intent="hr_policy")) == "retrieve"
    assert route_after_classify(AgentState(intent="greeting")) == "simple_answer"


def test_route_after_verify():
    assert route_after_verify(AgentState(has_evidence=True, plan={})) == "finalize"
    assert route_after_verify(
        AgentState(has_evidence=True,
                   plan={"tool": "create_leave_request"}, intent="leave_request")
    ) == "propose_action"
    # leave_balance is answered directly (no approval loop)
    assert route_after_verify(
        AgentState(has_evidence=True,
                   plan={"tool": "check_leave_balance"}, intent="leave_balance")
    ) == "finalize"
    assert route_after_verify(AgentState(has_evidence=False, retries=0)) == "retry_retrieve"
    s = AgentState(has_evidence=False, retries=settings.AGENT_MAX_ITER)
    assert route_after_verify(s) == "finalize"


# ---------------------------------------------------------------- full graph

def test_full_graph_knowledge_query(fake_stack):
    g = build_graph(checkpointer=MemorySaver())
    res = _invoke(g, AgentState(question="Chính sách nghỉ phép cho phép bao nhiêu ngày?",
                                session_id="s1"), thread="ws1-know")
    assert res["status"] == "answered"
    assert res["has_evidence"] is True
    assert res["citations"], "citations must be populated (contract)"
    assert "[S1]" in res["answer"]
    for node in ("classify_query", "retrieve", "rerank", "generate",
                 "verify_grounding", "finalize"):
        assert node in res["node_trace"]


def test_full_graph_insufficient_evidence(fake_stack, monkeypatch):
    # LLM answers off-topic -> grounding fails -> retries exhausted -> finalize
    retr, rerank, _ = fake_stack
    bad_llm = _FakeLLM(answer="Xin chào, hôm nay trời đẹp quá.")
    monkeypatch.setattr(lg, "_build_stack",
                        lambda tenant_id=None: (None, None, retr, rerank, bad_llm))
    g = build_graph(checkpointer=MemorySaver())
    res = _invoke(g, AgentState(question="Chính sách nghỉ phép?", session_id="s2"),
                  thread="ws1-noev")
    assert res["has_evidence"] is False
    assert res["citations"] == []
    assert res["retries"] >= 1


# ---------------------------------------------------------------- HITL

def test_hitl_propose_and_approve(fake_stack, tmp_path, monkeypatch):
    tmp_db = str(tmp_path / "hr_mock.json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp_db
    try:
        g = build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "ws1-hitl-ok"}}
        res = g.invoke(AgentState(question="Tôi muốn xin nghỉ 5 ngày từ 10/09",
                                  session_id="s3", employee_id="emp_hitl"), cfg)
        interrupts = res.get("__interrupt__")
        assert interrupts, "graph must pause for approval"
        assert interrupts[0].value["tool"] == "create_leave_request"

        res2 = g.invoke(Command(resume={"approved": True}), cfg)
        assert res2["status"] == "action_completed"
        assert res2["action_result"]["result"]["ok"] is True
    finally:
        settings.HR_MOCK_DB_PATH = old


def test_hitl_resume_rejected(fake_stack):
    g = build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "ws1-hitl-no"}}
    res = g.invoke(AgentState(question="Tôi muốn xin nghỉ 5 ngày từ 10/09",
                              session_id="s4"), cfg)
    assert res.get("__interrupt__")
    res2 = g.invoke(Command(resume={"approved": False}), cfg)
    assert res2["status"] == "action_cancelled"


def test_is_approved_and_execute_tool():
    assert _is_approved(True) is True
    assert _is_approved({"approved": True}) is True
    assert _is_approved({"approved": False}) is False
    assert _is_approved(False) is False
    assert _is_approved(None) is False
    out = _execute_tool("no_such_tool", {}, None, None)
    assert out["ok"] is False


def test_resume_from_approval_helper(monkeypatch, tmp_path):
    tmp_db = str(tmp_path / "hr_mock2.json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp_db
    monkeypatch.setattr(lg, "_build_stack",
                        lambda tenant_id=None: (None, None, _FakeRetriever(),
                                                _FakeReranker(), _FakeLLM()))
    try:
        import maia.agent.langgraph_agent as m
        m._durable_graph = None
        monkeypatch.setattr(settings, "STORAGE_DIR", tempfile.mkdtemp())
        g = lg.get_durable_graph()
        tid = "ws1-helper-approve"
        r1 = g.invoke(AgentState(question="Tôi muốn xin nghỉ 5 ngày từ 10/09",
                                 session_id="s5", employee_id="emp_h"),
                      {"configurable": {"thread_id": tid}})
        assert r1.get("__interrupt__")
        r2 = lg.resume_from_approval(tid, True, employee_id="emp_h")
        assert r2["status"] == "action_completed"
    finally:
        settings.HR_MOCK_DB_PATH = old
        import maia.agent.langgraph_agent as m2
        m2._durable_graph = None
