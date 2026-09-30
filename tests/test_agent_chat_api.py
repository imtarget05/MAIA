"""Tests for the POST /agent/chat FastAPI endpoint.

Covers:
- Non-streaming knowledge query -> status=answered
- Action query with full slots -> status=needs_approval + pending_action
- Resume approved -> action_completed (balance 12->10)
- Resume rejected -> action_cancelled
- SSE streaming -> text/event-stream with node + done events
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


# These tests pin the legacy HybridRetriever retrieval path (the LlamaIndex
# data-plane path has its own tests in test_llama_index_dataplane.py), so the
# config default flip (WS2) must not change what they exercise.
@pytest.fixture(autouse=True)
def _legacy_retrieval_path(monkeypatch):
    from maia.config import settings
    monkeypatch.setattr(settings, "LLAMA_INDEX_DATA_PLANE", False)


def _setup():
    """Build a mock stack + TestClient with auth override."""
    tmp = tempfile.mkdtemp()
    os.environ["MAIA_STORAGE_DIR"] = tmp
    os.environ["JWT_SECRET_KEY"] = "test-secret"
    from maia.config import settings
    settings.STORAGE_DIR = tmp
    settings.HR_MOCK_DB_PATH = str(Path(tmp) / "hr_mock.json")
    settings.TOOL_TENANT_CHECK = False

    from fastapi.testclient import TestClient

    import maia.agent.langgraph_agent as lg
    from maia.api import app, get_current_active_user
    from maia.chunking import Chunk
    from maia.embeddings import Embedder
    from maia.reranker import Reranker
    from maia.retriever import HybridRetriever
    from maia.test_utils import InMemoryVectorStore

    class MockLLM:
        mode = "mock"
        def chat(self, messages):
            return "The leave policy provides 12 annual leave days. [S1]"

    embedder = Embedder()
    store = InMemoryVectorStore()
    chunks = [
        Chunk(text="Leave Policy: each employee has 12 annual leave days.",
              metadata={"chunk_id": "c1", "filename": "Leave_Policy.md"}),
        Chunk(text="IT Security Policy v4.2: report lost device within 1 hour.",
              metadata={"chunk_id": "c2", "filename": "IT_Security_Policy_v4.2.md"}),
    ]
    for c in chunks:
        v = embedder.embed([c.text])[0]
        store.upsert_one(c.metadata["chunk_id"], v,
                        {"chunk_id": c.metadata["chunk_id"], "text": c.text,
                         "filename": c.metadata["filename"]})
    retriever = HybridRetriever(store, embedder, storage_dir=tmp)
    lg._build_stack = lambda tenant_id=None: (embedder, store, retriever, Reranker(), MockLLM())

    class _U:
        email = "tester@company.com"
        employee_id = "emp_test"
        tenant_id = "default"
        is_active = True
        role = "USER"

    app.dependency_overrides[get_current_active_user] = lambda: _U()
    client = TestClient(app, raise_server_exceptions=True)
    return client


def test_knowledge_query_returns_answered():
    client = _setup()
    r = client.post("/agent/chat", json={"question": "How many leave days?", "session_id": "api_know"})
    body = r.json()
    assert body["status"] == "answered"
    assert body["intent"] == "general"
    assert "classify_query" in body["node_trace"]
    assert "finalize" in body["node_trace"]


def test_action_query_returns_needs_approval():
    client = _setup()
    r = client.post("/agent/chat", json={
        "question": "Tôi muốn xin nghỉ phép 2 ngày từ 15/09",
        "session_id": "api_action",
    })
    body = r.json()
    assert body["status"] == "needs_approval"
    assert body["pending_action"]["tool"] == "create_leave_request"
    assert body["pending_action"]["params"]["days"] == 2


def test_resume_approved_executes_tool():
    client = _setup()
    # First call: trigger interrupt
    client.post("/agent/chat", json={
        "question": "Tôi muốn xin nghỉ phép 2 ngày từ 15/09",
        "session_id": "api_resume_ok",
    })
    # Second call: resume with approval
    r = client.post("/agent/chat", json={
        "question": "", "session_id": "api_resume_ok",
        "resume": {"approved": True},
    })
    body = r.json()
    assert body["status"] == "action_completed"
    assert body["action_result"]["result"]["ok"] is True
    assert body["action_result"]["result"]["remaining_balance"] == 10


def test_resume_rejected_cancels():
    client = _setup()
    client.post("/agent/chat", json={
        "question": "Tôi muốn xin nghỉ phép 1 ngày từ 15/09",
        "session_id": "api_resume_rej",
    })
    r = client.post("/agent/chat", json={
        "question": "", "session_id": "api_resume_rej",
        "resume": {"approved": False},
    })
    body = r.json()
    assert body["status"] == "action_cancelled"


def test_streaming_returns_sse_with_node_and_done_events():
    client = _setup()
    r = client.post("/agent/chat", json={
        "question": "leave days?", "session_id": "api_stream", "stream": True,
    })
    assert "text/event-stream" in r.headers["content-type"]
    text = r.text
    assert "event: node" in text
    assert "event: done" in text

"""POST /agent/chat API tests (offline, TestClient + faked stack).

Covers all 4 endpoint modes with auth stubbed via dependency_overrides:
- non-streaming knowledge query -> answered + citations
- action query -> needs_approval + pending_action (HITL pause)
- resume approved -> action_completed / resume rejected -> action_cancelled
- stream=true -> SSE event stream ending with event: done
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.checkpoint.memory import MemorySaver

from maia.agent import langgraph_agent as lg
from maia.api import _state_to_response, app, get_current_active_user
from maia.config import settings

POLICY_TEXT = (
    "Chinh sach nghi phep: nhan vien duoc nghi 12 ngay phep nam, "
    "don xin nghi can ghi ro so ngay va ngay bat dau."
)


class _FakeRetriever:
    def retrieve(self, question, tenant_id=None, session_id=None):
        return [{"chunk_id": "c1", "text": POLICY_TEXT, "score": 0.9,
                 "fused_score": 0.05,
                 "metadata": {"filename": "Leave_Policy.md",
                              "tenant_id": tenant_id or "t_api"}}]


class _FakeReranker:
    mode = "fallback"

    def rerank(self, query, candidates, top_k=3):
        for c in candidates:
            c["rerank_score"] = float(c.get("fused_score", 0.0))
        return candidates[:top_k]


class _FakeLLM:
    mode = "mock"

    def chat(self, messages):
        return POLICY_TEXT + " [S1]"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        lg, "_build_stack",
        lambda tenant_id=None: (None, None, _FakeRetriever(),
                                _FakeReranker(), _FakeLLM()),
    )
    # Fresh in-memory graph per test (isolates HITL threads between tests).
    # Use SAME graph instance everywhere so checkpoints persist across API calls.
    fresh_graph = lg.build_graph(checkpointer=MemorySaver())
    def _get_graph():
        return fresh_graph
    monkeypatch.setattr(lg, "get_durable_graph", _get_graph)
    monkeypatch.setattr(lg, "graph", fresh_graph)
    # The endpoint imports get_durable_graph locally at runtime
    import maia.agent.langgraph_agent as lg_mod
    monkeypatch.setattr(lg_mod, "get_durable_graph", _get_graph)
    monkeypatch.setattr(lg_mod, "graph", fresh_graph)
    settings.HR_MOCK_DB_PATH = str(tmp_path / "hr_api.json")
    # The streaming-budget registry is a module singleton, so give each test a
    # fresh one — otherwise threads leak between tests and the release/budget
    # assertions below would measure another test's leftovers. monkeypatch
    # restores the original on teardown.
    from maia.agent import session_registry as sr_mod
    from maia.agent.session_registry import StreamingSessionRegistry

    monkeypatch.setattr(
        sr_mod, "streaming_sessions",
        StreamingSessionRegistry(
            max_sessions=512,
            ttl_sec=3600,
            on_evict=lambda tid: fresh_graph.checkpointer.delete_thread(tid),
        ),
    )

    settings.HR_MOCK_DB_PATH = str(tmp_path / "hr_api.json")
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        tenant_id="t_api", employee_id="emp_api", email="api@company.com")
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.clear()


def test_state_to_response_shape():
    resp = _state_to_response({"answer": "a", "intent": "hr_policy",
                               "status": "answered", "citations": [{"tag": "[S1]"}],
                               "has_evidence": True, "grounding_score": 0.8,
                               "cites_valid": True, "slots": {},
                               "pending_action": None, "approval_needed": False,
                               "node_trace": ["finalize"]})
    assert resp["answer"] == "a" and resp["status"] == "answered"
    assert resp["citations"] == [{"tag": "[S1]"}]
    assert resp["needs_approval"] is False
    assert resp["action"] is None and resp["action_result"] is None


def test_agent_chat_knowledge_query(client):
    r = client.post("/agent/chat", json={"question": "Chính sách nghỉ phép?",
                                         "session_id": "api-k"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answered"
    assert body["has_evidence"] is True
    assert body["citations"], "API must surface citations"
    assert "classify_query" in body["node_trace"]


def test_agent_chat_action_needs_approval(client):
    r = client.post("/agent/chat", json={"question": "Tôi muốn xin nghỉ 5 ngày từ 10/09",
                                         "session_id": "api-a"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "needs_approval"
    assert body["needs_approval"] is True
    assert body["pending_action"]["tool"] == "create_leave_request"


def test_agent_chat_resume_approved(client):
    sid = "api-res-ok"
    r1 = client.post("/agent/chat", json={"question": "Tôi muốn xin nghỉ 5 ngày từ 10/09",
                                          "session_id": sid})
    assert r1.json()["status"] == "needs_approval"
    r2 = client.post("/agent/chat", json={"question": "Tôi muốn xin nghỉ 5 ngày từ 10/09",
                                          "session_id": sid,
                                          "resume": {"approved": True}})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "action_completed"


def test_agent_chat_resume_rejected(client):
    sid = "api-res-no"
    r1 = client.post("/agent/chat", json={"question": "Tôi muốn xin nghỉ 5 ngày từ 10/09",
                                          "session_id": sid})
    assert r1.json()["status"] == "needs_approval"
    r2 = client.post("/agent/chat", json={"question": "q", "session_id": sid,
                                          "resume": {"approved": False}})
    assert r2.json()["status"] == "action_cancelled"


def test_agent_chat_stream_sse(client):
    r = client.post("/agent/chat", json={"question": "Chính sách nghỉ phép?",
                                         "session_id": "api-s",
                                         "stream": True})
    assert r.status_code == 200, r.text
    text = r.text
    assert "event: node" in text
    assert "event: done" in text
    done_line = [ln for ln in text.splitlines() if ln.startswith("data: ")][-1]
    final = json.loads(done_line[len("data: "):])
    assert final["status"] in ("answered", "needs_approval")


def test_agent_chat_stream_sse_resume_after_hitl(client):
    """WS3: HITL resume contract — resume is JSON-only by design.

    The durable (SqliteSaver) graph serves resume; the SSE stream runs on an
    ephemeral MemorySaver path (SqliteSaver is sync-only and a resumed run has
    no live connection to stream into — see _agent_chat_stream docstring).
    So resume with stream=True still returns the final JSON, not SSE frames.
    """
    sid = "api-sse-hitl"
    r1 = client.post("/agent/chat", json={"question": "Tôi muốn xin nghỉ 5 ngày từ 10/09",
                                          "session_id": sid})
    assert r1.json()["status"] == "needs_approval"
    r2 = client.post("/agent/chat", json={"question": "", "session_id": sid,
                                          "resume": {"approved": True},
                                          "stream": True})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "action_completed"


# --- streaming session budget ------------------------------------------- #
#
# The SSE path runs on a process-wide MemorySaver graph, so without a bound
# every new session_id leaks a thread for the life of the process. These pin
# the wiring: the endpoint tracks the thread while the stream is open and
# releases it when the stream ends.

def test_streaming_releases_its_thread_when_the_stream_completes(client):
    from maia.agent import session_registry as sr_mod

    r = client.post("/agent/chat", json={
        "question": "leave days?", "session_id": "budget-completed", "stream": True,
    })
    assert r.status_code == 200
    assert "t_api:budget-completed" not in sr_mod.streaming_sessions.live_threads()


def test_streaming_releases_its_thread_on_client_disconnect(client):
    """An abandoned stream must not strand its thread.

    The generator is closed without being drained, which is what a browser tab
    closing mid-stream does; the ``finally`` in ``tracked_stream`` still runs.
    """
    from maia.agent import session_registry as sr_mod

    with client.stream("POST", "/agent/chat", json={
        "question": "leave days?", "session_id": "budget-abandoned", "stream": True,
    }) as response:
        assert response.status_code == 200
        next(response.iter_lines())  # consume one frame, then leave early

    assert "t_api:budget-abandoned" not in sr_mod.streaming_sessions.live_threads()


def test_streaming_budget_never_exceeds_the_configured_cap(client):
    """Many distinct sessions in a row must not grow the map past the cap."""
    from maia.agent import session_registry as sr_mod

    original = sr_mod.streaming_sessions.max_sessions
    sr_mod.streaming_sessions._max = 3
    try:
        for i in range(8):
            client.post("/agent/chat", json={
                "question": "leave days?", "session_id": f"budget-flood-{i}",
                "stream": True,
            })
        assert len(sr_mod.streaming_sessions) <= 3
    finally:
        sr_mod.streaming_sessions._max = original


def test_non_streaming_chat_does_not_touch_the_streaming_budget(client):
    """The durable graph serves non-streaming turns; it is not this budget's job."""
    from maia.agent import session_registry as sr_mod

    before = set(sr_mod.streaming_sessions.live_threads())
    client.post("/agent/chat", json={
        "question": "How many leave days?", "session_id": "budget-durable",
    })
    assert set(sr_mod.streaming_sessions.live_threads()) == before
