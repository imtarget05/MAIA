"""Tests for the LlamaIndex data-plane adapter (MaiaQdrantStore).

Covers:
- add() ingests TextNodes into the underlying Qdrant store
- query() returns VectorStoreQueryResult with ids, nodes, similarities
- delete_doc() removes only the matching doc's vectors (tenant-scoped)
- VectorStoreIndex.from_vector_store + as_retriever works end-to-end
- Hybrid retrieval (dense + BM25 → RRF) when LLAMA_INDEX_DATA_PLANE is on
"""
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llama_index.core import VectorStoreIndex
from llama_index.core.schema import TextNode
from llama_index.core.vector_stores.types import VectorStoreQuery

from maia.llamaindex_store import MaiaQdrantStore


class FakeQdrantStore:
    """Minimal QdrantStore stand-in: dense cosine search + delete_by_doc."""
    def __init__(self):
        self._data: dict[str, dict] = {}

    def upsert(self, vectors, chunks):
        for vec, chunk in zip(vectors, chunks):
            cid = chunk.metadata.get("chunk_id", str(len(self._data)))
            self._data[cid] = {"vec": np.asarray(vec), "text": chunk.text,
                               "meta": dict(chunk.metadata)}

    def search(self, qvec, top_k=5, tenant_id=None, with_payload=True, with_vectors=False):
        q = np.asarray(qvec)
        results = []
        for cid, p in self._data.items():
            if tenant_id:
                tid = p["meta"].get("tenant_id")
                if tid and tid != tenant_id:
                    continue
            sim = float(np.dot(q, p["vec"]) / (np.linalg.norm(q) * np.linalg.norm(p["vec"]) + 1e-9))
            # Return the real QdrantStore hit shape so the adapter is
            # exercised exactly as in production.
            results.append({"chunk_id": cid, "text": p["text"],
                            "score": sim, "metadata": dict(p["meta"])})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def delete_by_doc(self, doc_id):
        to_del = [cid for cid, p in self._data.items() if p["meta"].get("doc_id") == doc_id]
        for cid in to_del:
            del self._data[cid]

    @property
    def client(self):
        return "fake"

    def close(self):
        pass


def test_add_ingests_nodes():
    fq = FakeQdrantStore()
    vs = MaiaQdrantStore(fq, tenant_id="t1")
    nodes = [
        TextNode(id_="n1", text="hello world", embedding=[0.1, 0.2, 0.3],
                 metadata={"chunk_id": "n1", "doc_id": "d1"}),
    ]
    ids = vs.add(nodes)
    assert "n1" in ids
    assert "n1" in fq._data


def test_query_returns_matching_node():
    fq = FakeQdrantStore()
    vs = MaiaQdrantStore(fq, tenant_id="t1")
    vs.add([TextNode(id_="n1", text="hello world", embedding=[0.1, 0.2, 0.3],
                      metadata={"chunk_id": "n1", "doc_id": "d1"})])
    qr = vs.query(VectorStoreQuery(query_embedding=[0.1, 0.2, 0.3], similarity_top_k=5))
    assert qr.ids == ["n1"]
    assert qr.nodes[0].text == "hello world"
    assert qr.similarities[0] > 0.9


def test_delete_doc_removes_only_matching_doc():
    fq = FakeQdrantStore()
    vs = MaiaQdrantStore(fq, tenant_id="t1")
    vs.add([
        TextNode(id_="n1", text="doc1", embedding=[0.1, 0.0, 0.0],
                 metadata={"chunk_id": "n1", "doc_id": "d1"}),
        TextNode(id_="n2", text="doc2", embedding=[0.0, 0.1, 0.0],
                 metadata={"chunk_id": "n2", "doc_id": "d2"}),
    ])
    assert "n1" in fq._data and "n2" in fq._data
    vs.delete_doc("d1")
    assert "n1" not in fq._data
    assert "n2" in fq._data  # other doc untouched


def test_vector_store_index_from_vector_store():
    from llama_index.core.embeddings import BaseEmbedding
    fq = FakeQdrantStore()
    vs = MaiaQdrantStore(fq, tenant_id="t1")
    vs.add([TextNode(id_="n1", text="hello world", embedding=[0.1, 0.2, 0.3],
                      metadata={"chunk_id": "n1", "doc_id": "d1"})])

    class _E(BaseEmbedding):
        def _get_text_embedding(self, text):
            return [0.1, 0.2, 0.3]
        def _get_query_embedding(self, text):
            return [0.1, 0.2, 0.3]
        def _get_text_embeddings(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]
        async def _aget_text_embedding(self, text):
            return self._get_text_embedding(text)
        async def _aget_query_embedding(self, text):
            return self._get_query_embedding(text)
        async def _aget_text_embeddings(self, texts):
            return self._get_text_embeddings(texts)

    from llama_index.core import Settings
    Settings.embed_model = _E()
    idx = VectorStoreIndex.from_vector_store(vs)
    retriever = idx.as_retriever(similarity_top_k=2)
    results = retriever.retrieve("hello world")
    assert len(results) == 1
    assert results[0].node.text == "hello world"


def test_tenant_isolation_in_query():
    fq = FakeQdrantStore()
    vs = MaiaQdrantStore(fq, tenant_id="tA")
    vs.add([
        TextNode(id_="n1", text="tenant A doc", embedding=[0.1, 0.2, 0.3],
                 metadata={"chunk_id": "n1", "doc_id": "d1", "tenant_id": "tA"}),
        TextNode(id_="n2", text="tenant B doc", embedding=[0.1, 0.2, 0.3],
                 metadata={"chunk_id": "n2", "doc_id": "d2", "tenant_id": "tB"}),
    ])
    qr = vs.query(VectorStoreQuery(query_embedding=[0.1, 0.2, 0.3], similarity_top_k=5))
    # Only tenant A's node should be returned
    assert qr.ids == ["n1"]


def test_hybrid_retrieval_with_bm25_and_rrf():
    """When LLAMA_INDEX_DATA_PLANE is on, _llama_index_retrieve fuses dense + BM25."""
    import os
    tmp = tempfile.mkdtemp()
    os.environ["MAIA_STORAGE_DIR"] = tmp
    os.environ["LLAMA_INDEX_DATA_PLANE"] = "1"
    from maia.config import settings
    settings.STORAGE_DIR = tmp
    settings.LLAMA_INDEX_DATA_PLANE = True

    from maia.agent.langgraph_agent import AgentState, _llama_index_retrieve
    from maia.chunking import Chunk
    from maia.embeddings import Embedder
    from maia.retriever import HybridRetriever
    from maia.test_utils import InMemoryVectorStore

    embedder = Embedder()
    store = InMemoryVectorStore()
    chunks = [
        Chunk(text="Leave Policy: each employee has 12 annual leave days.",
              metadata={"chunk_id": "c1", "filename": "Leave_Policy.md", "doc_id": "d1"}),
        Chunk(text="IT Security Policy v4.2: report lost device within 1 hour.",
              metadata={"chunk_id": "c2", "filename": "IT_Security_Policy_v4.2.md", "doc_id": "d2"}),
    ]
    for c in chunks:
        v = embedder.embed([c.text])[0]
        store.upsert_one(c.metadata["chunk_id"], v,
                        {"chunk_id": c.metadata["chunk_id"], "text": c.text,
                         "filename": c.metadata["filename"], "doc_id": c.metadata["doc_id"]})

    retr = HybridRetriever(store, embedder, storage_dir=tmp, tenant_id="default")
    retr._corpus = [{"chunk_id": "c1", "text": chunks[0].text,
                     "metadata": {"chunk_id": "c1", "filename": "Leave_Policy.md"}},
                    {"chunk_id": "c2", "text": chunks[1].text,
                     "metadata": {"chunk_id": "c2", "filename": "IT_Security_Policy_v4.2.md"}}]
    retr._build_bm25()

    import maia.agent.langgraph_agent as lg
    orig = lg._build_stack
    lg._build_stack = lambda tenant_id=None: (embedder, store, retr, None, None)
    try:
        state = AgentState(question="nghỉ phép leave days", session_id="s1", tenant_id="default")
        results = _llama_index_retrieve(state)
        assert len(results) >= 1
        assert all("fused_score" in r for r in results)
    finally:
        lg._build_stack = orig
        settings.LLAMA_INDEX_DATA_PLANE = False


class _FakeBackend:
    """Minimal QdrantStore-shaped backend (search/upsert/delete_by_doc)."""

    def __init__(self):
        self.points = {}
        self.client = object()

    def upsert(self, vectors, chunks):
        for v, c in zip(vectors, chunks):
            cid = (c.metadata.get("chunk_id") or "noid")
            self.points[cid] = {"vec": np.asarray(v, dtype=np.float32),
                                "chunk": c}
        return len(self.points)

    def search(self, qvec, top_k=10, tenant_id=None):
        q = np.asarray(qvec, dtype=np.float32).flatten()
        scored = []
        for cid, p in self.points.items():
            meta = dict(p["chunk"].metadata)
            if tenant_id and meta.get("tenant_id") not in (tenant_id, None, ""):
                if meta.get("tenant_id") != tenant_id:
                    continue
            v = p["vec"]
            denom = (np.linalg.norm(q) * np.linalg.norm(v)) or 1e-9
            scored.append({"chunk_id": cid, "text": p["chunk"].text,
                           "score": float(np.dot(q, v) / denom),
                           "metadata": meta})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def delete_by_doc(self, ref_doc_id):
        for cid in [c for c, p in self.points.items()
                    if p["chunk"].metadata.get("doc_id") == ref_doc_id]:
            del self.points[cid]


def _seeded(tenant="t1"):
    from maia.chunking import Chunk
    backend = _FakeBackend()
    texts = [
        "Chinh sach nghi phep: 12 ngay phep nam cho nhan vien.",
        "Huong dan VPN: cai dat client va lien he IT de duoc ho tro.",
    ]
    vecs = [np.eye(4, dtype=np.float32)[i] for i in range(2)]
    chunks = [Chunk(text=t, metadata={"chunk_id": f"c{i}",
                                      "tenant_id": tenant,
                                      "filename": f"doc{i}.md"})
              for i, t in enumerate(texts)]
    backend.upsert(vecs, chunks)
    return backend


def test_client_exposes_backend():
    backend = _FakeBackend()
    vs = MaiaQdrantStore(backend, tenant_id="t1")
    assert vs.client is backend.client


def test_query_returns_nodes_and_similarities():
    from llama_index.core.vector_stores.types import VectorStoreQuery
    backend = _seeded("t1")
    vs = MaiaQdrantStore(backend, tenant_id="t1")
    q = VectorStoreQuery(query_embedding=np.eye(4, dtype=np.float32)[0].tolist(),
                         similarity_top_k=2)
    res = vs.query(q)
    assert res.ids and res.nodes and res.similarities
    assert res.ids[0] == "c0"
    assert "nghi phep" in res.nodes[0].get_content()
    assert res.similarities[0] > 0.9


def test_query_tenant_isolation():
    from llama_index.core.vector_stores.types import VectorStoreQuery
    backend = _seeded("t1")
    vs = MaiaQdrantStore(backend, tenant_id="other")
    q = VectorStoreQuery(query_embedding=np.eye(4, dtype=np.float32)[0].tolist(),
                         similarity_top_k=5)
    res = vs.query(q)
    assert res.ids == [] and res.nodes == []


def test_query_no_embedding_returns_empty():
    from llama_index.core.vector_stores.types import VectorStoreQuery
    vs = MaiaQdrantStore(_FakeBackend(), tenant_id="t1")
    res = vs.query(VectorStoreQuery(query_embedding=None))
    assert res.ids == [] and res.nodes == []


def test_add_and_delete_delegate():
    from llama_index.core.schema import TextNode
    backend = _FakeBackend()
    vs = MaiaQdrantStore(backend, tenant_id="t1")
    nodes = [TextNode(id_="n1", text="Chinh sach nghi phep 12 ngay.",
                      embedding=np.eye(4, dtype=np.float32)[0].tolist(),
                      metadata={"chunk_id": "n1", "tenant_id": "t1",
                                "doc_id": "d1"})]
    ids = vs.add(nodes)
    assert ids == ["n1"] and "n1" in backend.points
    vs.delete("d1")
    assert "n1" not in backend.points
    assert vs.add([]) == []


def test_vector_store_index_integration():
    from llama_index.core import VectorStoreIndex
    from llama_index.core.embeddings import BaseEmbedding
    backend = _seeded("t1")
    vs = MaiaQdrantStore(backend, tenant_id="t1")

    class _E(BaseEmbedding):
        def _get_text_embedding(self, text):
            return np.eye(4, dtype=np.float32)[0].tolist()
        def _get_query_embedding(self, text):
            return np.eye(4, dtype=np.float32)[0].tolist()
        def _get_text_embeddings(self, texts):
            return [self._get_text_embedding(t) for t in texts]
        async def _aget_text_embedding(self, text):
            return self._get_text_embedding(text)
        async def _aget_query_embedding(self, text):
            return self._get_query_embedding(text)
        async def _aget_text_embeddings(self, texts):
            return self._get_text_embeddings(texts)

    from llama_index.core import Settings
    Settings.embed_model = _E()
    index = VectorStoreIndex.from_vector_store(vs)
    retriever = index.as_retriever(similarity_top_k=1)
    hits = retriever.retrieve("nghi phep bao nhieu ngay?")
    assert hits, "LlamaIndex retriever must return hits from the same store"
    assert "nghi phep" in hits[0].node.get_content()
