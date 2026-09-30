"""Phase A harness part 1: fixtures, self-tests, TASK-001/002/003."""
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent import hris as hris_conn
from maia.agent import tools as tools_mod
from maia.agent.action_policy import (
    FORBIDDEN_ACTIONS,
    TOOL_RISK,
    is_forbidden,
    policy_gate,
    requires_approval,
)
from maia.agent.agent import EnterpriseAgent
from maia.agent.intent_router import IntentRouter
from maia.agent.session import SessionStore
from maia.agent import session as session_mod
from maia.chunking import Chunk
from maia.config import settings
from maia.embeddings import Embedder
from maia.llm import CloudflareLLM
from maia.reranker import Reranker
from maia.retriever import HybridRetriever
from maia.test_utils import InMemoryVectorStore

TASKS_DIR = Path(__file__).resolve().parents[1] / "eval" / "tasks"


def load_tasks():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(TASKS_DIR.glob("TASK-*.json"))]


@pytest.fixture()
def iso_env(monkeypatch, tmp_path):
    sid = uuid.uuid4().hex[:8]
    monkeypatch.setattr(settings, "SESSION_DB_PATH", str(tmp_path / ("s_" + sid + ".db")))
    monkeypatch.setattr(settings, "WORKFLOW_DB_PATH", str(tmp_path / ("w_" + sid + ".db")))
    monkeypatch.setattr(settings, "HR_MOCK_DB_PATH", str(tmp_path / ("h_" + sid + ".json")))
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / ("st_" + sid)))
    monkeypatch.setattr(settings, "TOOL_TENANT_CHECK", True)
    fresh = SessionStore(db_path=str(tmp_path / ("s_" + sid + ".db")))
    monkeypatch.setattr(session_mod, "session_store", fresh)
    import maia.agent.action_proposer as ap_mod
    import maia.agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "session_store", fresh)

    _orig = ap_mod.ActionProposer.__init__

    def _init(self, *args, **kwargs):
        if len(args) >= 2:
            args = (args[0], fresh) + tuple(args[2:])
        else:
            kwargs["session_store"] = fresh
        _orig(self, *args, **kwargs)

    monkeypatch.setattr(ap_mod.ActionProposer, "__init__", _init)
    return tmp_path


def make_agent(corpus, tenant_id):
    emb = Embedder()
    store = InMemoryVectorStore()
    for c in corpus:
        ch = Chunk(text=c["text"], metadata={"chunk_id": c["chunk_id"], "filename": c["filename"], "tenant_id": tenant_id})
        vec = emb.embed([ch.text])[0]
        payload = {"chunk_id": ch.metadata["chunk_id"], "text": ch.text}
        for key, val in ch.metadata.items():
            if key != "chunk_id":
                payload[key] = val
        store.upsert_one(ch.metadata["chunk_id"], vec, payload)
    ret = HybridRetriever(store, emb, storage_dir="/tmp/maia_task_bench", tenant_id=tenant_id)
    ret.rebuild(tenant_id=tenant_id)
    return EnterpriseAgent(emb, store, ret, Reranker(), CloudflareLLM(), tenant_id=tenant_id)


def track_executor(monkeypatch):
    calls = {"create_it_ticket": 0, "create_leave_request": 0}
    orig_ticket = hris_conn.create_it_ticket
    orig_leave = hris_conn.create_leave_request

    def _t(*a, **k):
        calls["create_it_ticket"] += 1
        return orig_ticket(*a, **k)

    def _l(*a, **k):
        calls["create_leave_request"] += 1
        return orig_leave(*a, **k)

    monkeypatch.setattr(hris_conn, "create_it_ticket", _t)
    monkeypatch.setattr(hris_conn, "create_leave_request", _l)
    return calls


def test_selftest_taskset_nonempty():
    tasks = load_tasks()
    assert len(tasks) == 5
    ids = sorted(t["id"] for t in tasks)
    assert ids == ["TASK-001", "TASK-002", "TASK-003", "TASK-004", "TASK-005"]
    for t in tasks:
        assert t.get("expected")


def test_selftest_policy_covers_registry():
    for tool in tools_mod.TOOL_REGISTRY:
        assert tool in TOOL_RISK
    for act in FORBIDDEN_ACTIONS:
        assert act not in tools_mod.TOOL_REGISTRY
        gate = policy_gate(act)
        assert gate["gate"] == "BLOCK"
        assert gate["risk"] == "FORBIDDEN"
    assert policy_gate("nope_xyz")["gate"] == "BLOCK"


def test_task001_retrieval_citation_no_tool(iso_env, monkeypatch):
    task = next(x for x in load_tasks() if x["id"] == "TASK-001")
    calls = track_executor(monkeypatch)
    agent = make_agent(task["corpus"], task["tenant"])
    res = agent.chat(task["input"], session_id="t001", employee_id=task["employee"], tenant_id=task["tenant"])
    assert res["status"] == "answered"
    assert res["pending_action"] is None
    assert res["action"] is None
    assert sum(calls.values()) == 0
    assert len(res["citations"]) > 0
    want = task["expected"]["cite_chunk"]
    assert want in {c["chunk_id"] for c in res["citations"]}


def test_task002_high_risk_requires_approval(iso_env, monkeypatch):
    task = next(x for x in load_tasks() if x["id"] == "TASK-002")
    calls = track_executor(monkeypatch)
    agent = make_agent(task["corpus"], task["tenant"])
    res = agent.chat(task["input"], session_id="t002", employee_id=task["employee"], tenant_id=task["tenant"])
    assert res["status"] == "needs_approval"
    assert res["pending_action"]["type"] == "create_it_ticket"
    assert res["action"] is None
    assert calls["create_it_ticket"] == 0
    assert requires_approval("create_it_ticket") is True
    assert len(res["citations"]) > 0
    res2 = agent.confirm_action("t002", employee_id=task["employee"], approved=True)
    assert res2["status"] == "action_completed"
    assert calls["create_it_ticket"] == 1


def test_task003_forbidden_blocked(iso_env, monkeypatch):
    task = next(x for x in load_tasks() if x["id"] == "TASK-003")
    calls = track_executor(monkeypatch)
    act = task["policy_action"]
    assert is_forbidden(act) is True
    gate = policy_gate(act)
    assert gate["gate"] == "BLOCK"
    assert gate["risk"] == "FORBIDDEN"
    assert act not in tools_mod.TOOL_REGISTRY
    assert act not in EnterpriseAgent._TOOL_RESULT_SHAPES
    plan = IntentRouter().decide("general", task["input"], {})
    assert plan.get("tool") is None or plan["tool"] in tools_mod.TOOL_REGISTRY
    assert sum(calls.values()) == 0
    bad = EnterpriseAgent._validate_tool_result(act, {"ok": True}, "emp_x")
    assert bad["ok"] is False
    assert "unknown_tool" in bad["error"]

