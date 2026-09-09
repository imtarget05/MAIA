"""Tests for the multi-agent team (offline: hash embedder + mock LLM)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.teams import AgentTeam
from maia.chunking import Chunk
from maia.embeddings import Embedder
from maia.llm import CloudflareLLM
from maia.reranker import Reranker
from maia.retriever import HybridRetriever
from maia.stream.store import InMemoryVectorStore


def _team(chunks):
    embedder = Embedder()
    store = InMemoryVectorStore()
    for ch in chunks:
        v = embedder.embed([ch.text])[0]
        store.upsert_one(ch.metadata["chunk_id"], v,
                         {"chunk_id": ch.metadata["chunk_id"], "text": ch.text,
                          **{k: v2 for k, v2 in ch.metadata.items() if k != "chunk_id"}})
    retr = HybridRetriever(store, embedder, storage_dir="/tmp/maia_test_teams")
    return AgentTeam(retriever=retr, reranker=Reranker(), llm=CloudflareLLM())


def _leave_chunks():
    return [
        Chunk(text="Leave Policy: each employee has 12 annual leave days. Request via MAIA.",
              metadata={"chunk_id": "lp1", "filename": "Leave_Policy.md"}),
        Chunk(text="IT Security Policy v4.2 Section 7.1: report lost device within 1 hour.",
              metadata={"chunk_id": "sec1", "filename": "IT_Security_Policy_v4.2.md"}),
    ]


def test_team_completed_with_citations():
    team = _team(_leave_chunks())
    res = team.run("Chính sách nghỉ phép như thế nào?")
    assert res.status == "completed"
    assert res.final_answer
    assert len(res.citations) > 0
    roles = [m.role for m in res.members]
    assert "researcher" in roles and "writer" in roles and "reviewer" in roles
    d = team.to_dict(res)
    assert d["answer"] == res.final_answer
    assert d["citations"][0]["tag"] == "[S1]"


def test_team_refused_on_empty_store():
    team = _team([])
    res = team.run("Chính sách nghỉ phép như thế nào?")
    assert res.status == "refused"
    assert res.citations == []


def test_team_custom_members():
    team = _team(_leave_chunks(), )
    team.members = ["writer"]
    res = team.run("Chính sách nghỉ phép như thế nào?")
    assert res.status == "completed"
    assert [m.role for m in res.members] == ["writer"]
