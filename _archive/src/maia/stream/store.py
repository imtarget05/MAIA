"""Vector-store adapters for the streaming workers (spec §7 idempotency).

Provides:
  * InMemoryVectorStore - dependency-free dict store (offline tests/benchmarks)
  * build_stream_store() - Qdrant when available, else the in-memory store.

Contract used by workers:
  upsert_one(chunk_id, vector, payload) -> idempotent upsert keyed by chunk_id
  exists(chunk_id) -> bool
  count() -> int
"""
from __future__ import annotations

import uuid

import numpy as np

from ..config import settings


class InMemoryVectorStore:
    """Offline vector store with the same contract as QdrantStore.

    Supports the streaming worker contract (upsert_one/exists/count) plus the
    retrieval/query-pipeline ops (search, scroll_all, delete_by_doc) so the full
    Production-RAG loops can be tested without a running Qdrant (Loop 1-4, CI).
    """

    def __init__(self) -> None:
        self._points: dict[str, dict] = {}  # chunk_id -> point

    def _point_id(self, chunk_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))

    def upsert_one(self, chunk_id: str, vector, payload: dict) -> str:
        from ..config import settings as _s
        payload = dict(payload)
        payload.setdefault("tenant_id", _s.TENANT_ID)
        vec = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        point = {"id": self._point_id(chunk_id), "vector": list(vec),
                 "payload": payload, "doc_id": payload.get("document_id", payload.get("doc_id", ""))}
        self._points[chunk_id] = point  # upsert: overwrite by chunk_id (no duplicates)
        return point["id"]

    def exists(self, chunk_id: str) -> bool:
        return chunk_id in self._points

    def count(self) -> int:
        return len(self._points)

    def get(self, chunk_id: str) -> dict | None:
        return self._points.get(chunk_id)

    def delete_by_doc(self, doc_id: str) -> None:
        """Remove every vector belonging to a document (Loop 1 re-index/delete)."""
        for cid in [c for c, p in self._points.items() if p["doc_id"] == doc_id]:
            del self._points[cid]

    def delete(self, chunk_id: str) -> None:
        self._points.pop(chunk_id, None)

    def scroll_all(self, limit: int = 10000, tenant_id: str | None = None) -> list[dict]:
        """Return corpus [{chunk_id, text, metadata}] for retrieval/BM25 (Loop 2)."""
        out = []
        for p in self._points.values():
            payload = dict(p["payload"])
            if tenant_id and payload.get("tenant_id") not in (tenant_id, None, ""):
                # skip other tenants when filtered
                if payload.get("tenant_id") != tenant_id:
                    continue
            out.append({"chunk_id": self._chunk_key(p),
                        "text": payload.get("text", ""),
                        "metadata": payload})
        return out

    @staticmethod
    def _chunk_key(p: dict) -> str:
        # derive chunk_id back from a stored point: try payload, else lookup map
        return p["payload"].get("chunk_id", p["id"])

    def search(self, query_vec, top_k: int = 10, score_threshold: float | None = None, tenant_id: str | None = None, extra_filter=None) -> list[dict]:
        """Cosine search over the stored vectors (Loop 2/query pipeline)."""
        q = np.asarray(query_vec, dtype=np.float32).flatten()
        scored = []
        for p in self._points.values():
            payload = dict(p["payload"])
            if tenant_id and payload.get("tenant_id") not in (tenant_id, None, ""):
                if payload.get("tenant_id") != tenant_id:
                    continue
            v = np.asarray(p["vector"], dtype=np.float32)
            denom = (np.linalg.norm(q) * np.linalg.norm(v)) or 1e-9
            score = float(np.dot(q, v) / denom)
            if score_threshold is not None and score < score_threshold:
                continue
            scored.append({"chunk_id": payload.get("chunk_id", p["id"]),
                           "text": payload.get("text", ""), "score": score,
                           "metadata": {k: v for k, v in payload.items() if k != "text"}})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]


def _qdrant_store(embedder) -> object | None:
    """Try to build a QdrantStore; return None if unreachable."""
    try:
        from ..vector_store import QdrantStore

        store = QdrantStore(url=settings.QDRANT_URL, collection=settings.QDRANT_COLLECTION,
                            dim=embedder.dim, api_key=settings.QDRANT_API_KEY)
        store._upsert_one = store.upsert_one
        return store
    except Exception as e:  # noqa: BLE001 - any connect/import failure -> fallback
        print(f"[stream.store] Qdrant unreachable ({e}); using InMemoryVectorStore")
        return None


def build_stream_store(embedder) -> object:
    """Qdrant on success, else an in-memory store that satisfies the same contract."""
    q = _qdrant_store(embedder)
    if q is not None:
        return q
    return InMemoryVectorStore()