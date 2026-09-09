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
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from .config import settings
from .loops.resilience import CircuitBreaker, CircuitOpenError, RetryConfig, with_retry

# G-06: per-dependency circuit breakers. Lazily initialized (client may be
# created at import time before settings are fully loaded in some paths).
_qdrant_breaker: CircuitBreaker | None = None
_qdrant_retry = RetryConfig(max_retries=settings.RELIABILITY_MAX_RETRIES,
                            backoff_base=settings.RELIABILITY_RETRY_BACKOFF_SEC,
                            retryable=(TimeoutError, ConnectionError, OSError))


def _get_qdrant_breaker() -> CircuitBreaker:
    global _qdrant_breaker
    if _qdrant_breaker is None:
        threshold = settings.RELIABILITY_QDRANT_THRESHOLD or settings.RELIABILITY_FAILURE_THRESHOLD
        _qdrant_breaker = CircuitBreaker("qdrant", failure_threshold=threshold,
                                         recovery_timeout=settings.RELIABILITY_RECOVERY_TIMEOUT_SEC)
    return _qdrant_breaker


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
            kwargs = {"url": candidate, "prefer_grpc": False, "timeout": 30,
                      "check_compatibility": False}
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
        from .config import settings as _s
        points = []
        for vec, ch in zip(vectors, chunks):
            payload = dict(ch.metadata)
            payload["text"] = ch.text
            payload["chunk_hash"] = _chunk_hash(ch.text)
            # RBAC: ensure tenant_id present (payload filter)
            payload.setdefault("tenant_id", _s.TENANT_ID)
            # stable UUID from chunk_id so re-ingest overwrites (dedup)
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, payload.get("chunk_id", payload["chunk_hash"])))
            points.append(PointStruct(id=pid, vector=vec.tolist(), payload=payload))
        if not points:
            return 0
        self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def upsert_one(self, chunk_id: str, vector, payload: dict) -> str:
        """Idempotent single-chunk upsert used by the streaming embedding workers.

        The point id is a stable UUID derived from ``chunk_id`` (spec §7) so a
        restart/redelivery with the same ``chunk_id`` overwrites instead of
        inserting a duplicate vector. Returns the stable point id.
        """
        from .config import settings as _s
        payload = dict(payload)
        payload["chunk_hash"] = _chunk_hash(payload.get("text", ""))
        payload.setdefault("tenant_id", _s.TENANT_ID)
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))
        vec = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        self.client.upsert(
            collection_name=self.collection,
            points=[PointStruct(id=pid, vector=vec, payload=payload)],
        )
        return pid

    def exists(self, chunk_id: str) -> bool:
        """Idempotency probe used by streaming workers (spec §7): true if the
        chunk's stable point id is already stored -> skip re-embedding."""
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))
        try:
            return bool(self.client.retrieve(collection_name=self.collection, ids=[pid]))
        except Exception:  # noqa: BLE001 - treat lookup failure as not-found
            return False

    def search(self, query_vec, top_k: int = 10, score_threshold: float | None = None, tenant_id: str | None = None, extra_filter: Filter | None = None) -> list[dict]:
        vec = query_vec.tolist() if hasattr(query_vec, "tolist") else list(query_vec)
        qfilter: Filter | None = extra_filter
        if tenant_id:
            tenant_cond = FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))
            if qfilter and qfilter.must:
                qfilter.must.append(tenant_cond)
            elif qfilter:
                qfilter.must = [tenant_cond]
            else:
                qfilter = Filter(must=[tenant_cond])
        # G-06: wrap the external Qdrant call with circuit breaker + retry.
        # On CircuitOpenError / timeout → graceful degraded (empty results),
        # letting the evidence gate refuse cleanly instead of hanging.
        try:
            return with_retry(_qdrant_retry, _get_qdrant_breaker().call,
                             self._do_search, vec, top_k, score_threshold, qfilter, tenant_id)
        except (CircuitOpenError, TimeoutError, ConnectionError, OSError):
            return []

    def _do_search(self, vec, top_k, score_threshold, qfilter, tenant_id) -> list[dict]:
        # qdrant-client >=1.10 uses query_points; older uses search
        try:
            res = self.client.query_points(
                collection_name=self.collection,
                query=vec,
                limit=top_k,
                score_threshold=score_threshold,
                query_filter=qfilter,
            )
            hits = res.points if hasattr(res, "points") else res
        except AttributeError:
            hits = self.client.search(
                collection_name=self.collection,
                query_vector=vec,
                limit=top_k,
                score_threshold=score_threshold,
                query_filter=qfilter,
            )
        except TypeError:
            # older client without query_filter support for query_points
            res = self.client.query_points(
                collection_name=self.collection,
                query=vec,
                limit=top_k,
                score_threshold=score_threshold,
            )
            hits = res.points if hasattr(res, "points") else res
            if tenant_id:
                hits = [h for h in hits if (h.payload or {}).get("tenant_id") in (tenant_id, None, "")]
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

    def scroll_all(self, limit: int = 10000, tenant_id: str | None = None) -> list[dict]:
        """For BM25 corpus rebuild + eval."""
        qfilter = None
        if tenant_id:
            qfilter = Filter(must=[FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))])
        pts, _ = self.client.scroll(collection_name=self.collection, limit=limit, with_payload=True, scroll_filter=qfilter)
        out = []
        for p in pts:
            payload = dict(p.payload or {})
            out.append({"chunk_id": payload.get("chunk_id", ""), "text": payload.get("text", ""), "metadata": payload})
        return out
