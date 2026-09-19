"""URL sources (notebook-style "paste a link") — fully offline.

Network is never touched: fetch_url is monkeypatched, DNS is stubbed,
vectors live in InMemoryVectorStore with the deterministic hash embedder
(MAIA_EMBED_FORCE_HASH=1 via conftest).

Contract under test:
- validate_url blocks non-http(s), localhost/private IPs, userinfo URLs
- ingest twice in one session -> same doc_id, no chunk growth (dedup)
- session scope: A sees its links + global docs; B sees only global docs;
  legacy (session None) sees everything
- delete removes only that session's copy
- agent chat cites the link source inside its own session only
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from maia.agent.agent import EnterpriseAgent
from maia.chunking import Chunk
from maia.config import settings
from maia.embeddings import Embedder
from maia.ingestion import (
    RawDoc,
    _strip_html_fallback,
    fetch_url,
    normalize_url,
    validate_url,
)
from maia.ingestion_pipeline import delete_source, ingest_url, list_sources
from maia.llm import CloudflareLLM
from maia.reranker import Reranker
from maia.retriever import HybridRetriever
from maia.test_utils import InMemoryVectorStore

FAKE_URL = "https://example.com/vpn-guide"
FAKE_TEXT = ("VPN Guide: connect via vpn dot company dot com with SSO. "
             "Contact IT Help Desk ext 202 for token reset. " * 8)


def _fake_fetch(url, session_id=""):
    import hashlib
    norm = url.strip()
    doc_id = hashlib.sha1(f"{session_id}|{norm}".encode()).hexdigest()[:12]
    return RawDoc(text=FAKE_TEXT, metadata={
        "doc_id": doc_id, "filename": "VPN Guide — example.com", "source": norm,
        "section": "", "page": "", "timestamp": "123", "origin": "url"})


def _stack(tmp_path, chunks=()):
    embedder = Embedder()
    store = InMemoryVectorStore()
    for ch in chunks:
        v = embedder.embed([ch.text])[0]
        store.upsert_one(ch.metadata["chunk_id"], v,
                         {"chunk_id": ch.metadata["chunk_id"], "text": ch.text,
                          **{k: v2 for k, v2 in ch.metadata.items() if k != "chunk_id"}})
    d = tmp_path / "bm25"
    d.mkdir(exist_ok=True)
    retriever = HybridRetriever(store, embedder, storage_dir=str(d))
    return embedder, store, retriever


def _global_chunks():
    return [Chunk(text="Leave Policy: each employee has 12 annual leave days.",
                  metadata={"chunk_id": "lp1", "filename": "Leave_Policy.md"})]


# ---- URL validation (no network: DNS stubbed) -----------------------------

def _public_dns(monkeypatch):
    import socket
    real = socket.getaddrinfo
    def fake(host, *a, **k):
        if host in ("example.com", "docs.example.org"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_normalize_url():
    assert normalize_url("https://Example.COM/a?x=1#frag") == "https://example.com/a?x=1"
    assert normalize_url("  http://example.com/  ") == "http://example.com/"


def test_validate_rejects_bad_schemes():
    _, err = validate_url("ftp://example.com/f")
    assert err and "http" in err
    _, err = validate_url("notaurl")
    assert err


def test_validate_rejects_userinfo_and_local(monkeypatch):
    _public_dns(monkeypatch)
    for bad in ("http://user:pass@example.com/", "http://localhost:8000/x",
                "http://127.0.0.1/x", "http://10.0.0.5/x", "http://[::1]/x"):
        _, err = validate_url(bad)
        assert err, bad


def test_validate_accepts_public(monkeypatch):
    _public_dns(monkeypatch)
    norm, err = validate_url("https://example.com/vpn-guide")
    assert err is None and norm == "https://example.com/vpn-guide"


def test_strip_html_fallback():
    title, text = _strip_html_fallback(
        "<html><head><title>VPN Guide</title></head><body>"
        "<script>evil()</script><h1>VPN Guide</h1><p>Use SSO.</p></body></html>")
    assert title == "VPN Guide"
    assert "Use SSO" in text and "evil" not in text


# ---- ingest / scope / dedup / delete ---------------------------------------

def test_ingest_url_dedups_same_session(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.ingestion_pipeline.fetch_url", _fake_fetch)
    stack = _stack(tmp_path, _global_chunks())
    r1 = ingest_url(FAKE_URL, session_id="sessA", stack=stack)
    n1 = stack[1].count()
    r2 = ingest_url(FAKE_URL, session_id="sessA", stack=stack)
    assert r1["doc_id"] == r2["doc_id"]
    assert stack[1].count() == n1  # overwrite, no growth
    assert r1["url"] == FAKE_URL and r1["session_id"] == "sessA"


def test_session_scope_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.ingestion_pipeline.fetch_url", _fake_fetch)
    stack = _stack(tmp_path, _global_chunks())
    _, store, retriever = stack
    ingest_url(FAKE_URL, session_id="sessA", stack=stack)

    q = "how to connect VPN with SSO token"
    got_a = {c["chunk_id"] for c in retriever.retrieve(q, session_id="sessA")}
    got_b = {c["chunk_id"] for c in retriever.retrieve(q, session_id="sessB")}
    got_none = {c["chunk_id"] for c in retriever.retrieve(q, session_id=None)}
    url_cids = {c["chunk_id"] for c in store.scroll_all()
                if (c["metadata"].get("session_id") or "") == "sessA"}
    assert url_cids and url_cids <= got_a          # A sees its link
    assert not (url_cids & got_b)                  # B does not
    assert url_cids <= got_none                    # legacy sees everything
    assert "lp1" in got_a and "lp1" in got_b       # global visible in both


def test_same_url_in_two_sessions_independent(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.ingestion_pipeline.fetch_url", _fake_fetch)
    stack = _stack(tmp_path, _global_chunks())
    ra = ingest_url(FAKE_URL, session_id="sessA", stack=stack)
    rb = ingest_url(FAKE_URL, session_id="sessB", stack=stack)
    assert ra["doc_id"] != rb["doc_id"]
    assert delete_source(ra["doc_id"], "sessA", stack=stack)["ok"] is True
    srcs_b = list_sources("sessB", stack=stack)
    assert [s["doc_id"] for s in srcs_b] == [rb["doc_id"]]  # B survives
    assert list_sources("sessA", stack=stack) == []


def test_delete_rejects_foreign_doc(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.ingestion_pipeline.fetch_url", _fake_fetch)
    stack = _stack(tmp_path, _global_chunks())
    r = ingest_url(FAKE_URL, session_id="sessA", stack=stack)
    out = delete_source(r["doc_id"], "sessB", stack=stack)
    assert out["ok"] is False
    assert len(list_sources("sessA", stack=stack)) == 1


def test_list_sources_groups_by_doc(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.ingestion_pipeline.fetch_url", _fake_fetch)
    stack = _stack(tmp_path, _global_chunks())
    ingest_url(FAKE_URL, session_id="sessA", stack=stack)
    srcs = list_sources("sessA", stack=stack)
    assert len(srcs) == 1
    assert srcs[0]["url"] == FAKE_URL and srcs[0]["chunks"] >= 1
    assert "example.com" in srcs[0]["title"]
    assert list_sources("sessB", stack=stack) == []


def test_session_cap(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.ingestion_pipeline.fetch_url", _fake_fetch)
    monkeypatch.setattr(settings, "URL_MAX_LINKS_PER_SESSION", 1)
    stack = _stack(tmp_path, _global_chunks())
    ingest_url(FAKE_URL, session_id="sessA", stack=stack)
    with pytest.raises(ValueError, match="max"):
        ingest_url("https://example.com/other", session_id="sessA", stack=stack)


# ---- agent level: citation points at the link ------------------------------

def test_agent_cites_link_in_own_session(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.ingestion_pipeline.fetch_url", _fake_fetch)
    embedder, store, retriever = _stack(tmp_path, _global_chunks())
    stack = (embedder, store, retriever, None, None)
    ingest_url(FAKE_URL, session_id="sessA", stack=stack)
    agent = EnterpriseAgent(embedder, store, retriever, Reranker(), CloudflareLLM())
    r = agent.chat("how do I connect VPN with SSO?", session_id="sessA")
    assert r["status"] == "answered"
    assert any("example.com" in (c.get("filename", "")) for c in r["citations"])
    r2 = agent.chat("how do I connect VPN with SSO?", session_id="sessB")
    assert not any("example.com" in (c.get("filename", ""))
                   for c in r2.get("citations", []))


def test_fetch_url_rejects_without_fetch(monkeypatch):
    # fetch_url itself validates before any network use
    with pytest.raises(ValueError):
        fetch_url("http://127.0.0.1/secret")
