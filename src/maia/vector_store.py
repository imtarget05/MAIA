"""Qdrant adapter (§6): primary vector backend.

- create collection (COSINE, dim from embedder)
- upsert with payload = metadata + text
- dense search with score
- delete by doc_id (for doc update, §13 Q12)
- dedup via chunk hash check
"""
import hashlib
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams


def _chunk_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


class QdrantStore:
    def __init__(self, url: str, collection: str, dim: int, api_key: str = ""):
        self.collection = collection
        self.dim = dim
        # Some PaaS sandboxes (e.g. Streamlit Community Cloud) only allow
        # outbound TCP on 443. Try the given URL first, then fall back to :443.
        self.url = url.rstrip("/")
        candidates = [self.url]
        if ":6333" in self.url:
            candidates.append(self.url.replace(":6333", ":443"))
        last_err: Exception | None = None
        self.client = None
        for candidate in candidates:
            kwargs = {"url": candidate, "prefer_grpc": False, "timeout": 30}
            if api_key:
                kwargs["api_key"] = api_key
            try:
                client = QdrantClient(**kwargs)
                client.get_collections()  # connectivity probe
                self.client = client
                self.url = candidate
                break
            except Exception as e:  # try next candidate
                last_err = e
        if self.client is None:
            raise ConnectionError(
                f"Cannot reach Qdrant at {url} (tried {candidates}): {last_err}"
            )
        self.ensure_collection()

    def ensure_collection(self):
        try:
            cols = [c.name for c in self.client.get_collections().collections]
        except Exception:
            cols = []
        if self.collection not in cols:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def upsert(self, vectors, chunks) -> int:
        points = []
        for vec, ch in zip(vectors, chunks):
            payload = dict(ch.metadata)
            payload["text"] = ch.text
            payload["chunk_hash"] = _chunk_hash(ch.text)
            # stable UUID from chunk_id so re-ingest overwrites (dedup)
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, payload.get("chunk_id", payload["chunk_hash"])))
            points.append(PointStruct(id=pid, vector=vec.tolist(), payload=payload))
        if not points:
            return 0
        self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def search(self, query_vec, top_k: int = 10, score_threshold: float | None = None) -> list[dict]:
        vec = query_vec.tolist() if hasattr(query_vec, "tolist") else list(query_vec)
        # qdrant-client >=1.10 uses query_points; older uses search
        try:
            res = self.client.query_points(
                collection_name=self.collection,
                query=vec,
                limit=top_k,
                score_threshold=score_threshold,
            )
            hits = res.points if hasattr(res, "points") else res
        except AttributeError:
            hits = self.client.search(
                collection_name=self.collection,
                query_vector=vec,
                limit=top_k,
                score_threshold=score_threshold,
            )
        out = []
        for r in hits:
            p = dict(r.payload or {})
            out.append(
                {
                    "chunk_id": p.get("chunk_id", ""),
                    "text": p.get("text", ""),
                    "score": float(r.score),
                    "metadata": {k: v for k, v in p.items() if k != "text"},
                }
            )
        return out

    def delete_by_doc(self, doc_id: str) -> None:
        self.client.delete(
            collection_name=self.collection,
            points_selector=Filter(
                must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            ),
        )

    def count(self) -> int:
        try:
            return self.client.count(collection_name=self.collection).count
        except Exception:
            return 0

    def scroll_all(self, limit: int = 10000) -> list[dict]:
        """For BM25 corpus rebuild + eval."""
        pts, _ = self.client.scroll(collection_name=self.collection, limit=limit, with_payload=True)
        out = []
        for p in pts:
            payload = dict(p.payload or {})
            out.append({"chunk_id": payload.get("chunk_id", ""), "text": payload.get("text", ""), "metadata": payload})
        return out
