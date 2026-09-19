"""Hybrid retrieval (§7, verify: hybrid): dense (Qdrant) + sparse (BM25) -> RRF fusion.

Exact algorithm documented here (per spec: do not claim without implementation):
- dense: Qdrant cosine top-k_d
- sparse: rank_bm25.OkapiBM25 over cached corpus (accent-stripped lowercase
  tokens via maia.textnorm, so Vietnamese queries match)
- fusion: Reciprocal Rank Fusion score = sum(1/(RRF_K + rank)) per chunk_id
- output top-k_fused with fused_score + dense_score + bm25_score

Security note: corpus cache uses JSON (not pickle) to avoid arbitrary code
execution risk from untrusted cache files (P1-3).
"""
import json
from pathlib import Path

import numpy as np

from .config import settings
from .textnorm import norm_tokens


class HybridRetriever:
    def __init__(self, store, embedder, storage_dir: str = "./storage",
                 top_k_dense: int = 10, top_k_bm25: int = 10, top_k_fused: int = 8, rrf_k: int = 60,
                 tenant_id: str | None = None):
        self.store = store
        self.embedder = embedder
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.top_k_dense = top_k_dense
        self.top_k_bm25 = top_k_bm25
        self.top_k_fused = top_k_fused
        self.rrf_k = rrf_k
        self.tenant_id = tenant_id or settings.TENANT_ID
        self._bm25 = None
        self._corpus: list[dict] = []  # [{chunk_id, text, metadata}]
        self._load_or_rebuild()

    # ---- corpus ----
    def _cache_path(self, tenant_id: str | None = None) -> Path:
        tid = tenant_id if tenant_id is not None else self.tenant_id
        if tid:
            return self.storage_dir / f"bm25_corpus_{tid}.json"
        return self.storage_dir / "bm25_corpus.json"

    def _load_or_rebuild(self):
        cp = self._cache_path()
        if cp.exists():
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    self._corpus = json.load(f)
                self._build_bm25()
                return
            except Exception:
                pass
        self.rebuild()

    def rebuild(self, tenant_id: str | None = None):
        """Rebuild BM25 index for a specific tenant.

        Args:
            tenant_id: Tenant to rebuild for. Defaults to instance tenant_id.
                       Must be provided to ensure tenant isolation.
        """
        tid = tenant_id if tenant_id is not None else self.tenant_id
        if tid is None:
            raise ValueError("tenant_id is required for rebuild() to prevent cross-tenant leakage")
        try:
            # tenant-aware scroll if store supports it
            try:
                self._corpus = self.store.scroll_all(tenant_id=tid)
            except TypeError:
                self._corpus = self.store.scroll_all()
        except Exception:
            self._corpus = []
        self._build_bm25()
        try:
            with open(self._cache_path(tid), "w", encoding="utf-8") as f:
                json.dump(self._corpus, f, ensure_ascii=False)
        except Exception:
            pass

    def _build_bm25(self):
        if not self._corpus:
            self._bm25 = None
            return
        try:
            from rank_bm25 import BM25Okapi

            tokenized = [norm_tokens(c["text"]) for c in self._corpus]
            self._bm25 = BM25Okapi(tokenized)
        except Exception:
            self._bm25 = None

    # ---- retrieve ----
    @staticmethod
    def _scope_ok(meta: dict, session_id: str | None) -> bool:
        """Session scope: global chunks (no session_id) are always visible;
        URL chunks are visible only to the session that added them.
        session_id=None keeps legacy behavior (everything visible)."""
        if not session_id:
            return True
        sid = (meta or {}).get("session_id") or ""
        return sid == "" or sid == session_id

    def retrieve(self, query: str, tenant_id: str | None = None,
                 session_id: str | None = None) -> list[dict]:
        # tenant_id overrides instance default (RBAC)
        tid = tenant_id if tenant_id is not None else self.tenant_id
        qvec = self.embedder.embed_query(query)
        try:
            dense = self.store.search(qvec, top_k=self.top_k_dense, tenant_id=tid)
        except TypeError:
            dense = self.store.search(qvec, top_k=self.top_k_dense)
        dense = [d for d in dense if self._scope_ok(d.get("metadata", {}), session_id)]

        # BM25 (also tenant-filtered via corpus already filtered on rebuild)
        bm25_ranked: list[dict] = []
        if self._bm25 is not None and self._corpus:
            # if tenant_id specified but retriever was built without it, filter ad-hoc
            corpus_view = self._corpus
            if tid and any(c.get("metadata", {}).get("tenant_id") for c in self._corpus):
                corpus_view = [c for c in self._corpus if c.get("metadata", {}).get("tenant_id") in (tid, None, "")]
                # need to recompute BM25 scores against filtered corpus? fallback to brute filter of results
                if len(corpus_view) != len(self._corpus):
                    # fallback simple keyword filter on filtered corpus without rebuilding BM25
                    pass
            try:
                scores = self._bm25.get_scores(norm_tokens(query))
                idx = np.argsort(scores)[::-1][: self.top_k_bm25]
                for i in idx:
                    if scores[i] <= 0:
                        continue
                    c = self._corpus[int(i)]
                    if tid and c.get("metadata", {}).get("tenant_id") not in (tid, None, "", self.tenant_id):
                        continue
                    if not self._scope_ok(c.get("metadata", {}), session_id):
                        continue
                    bm25_ranked.append(
                        {
                            "chunk_id": c["chunk_id"],
                            "text": c["text"],
                            "score": float(scores[i]),
                            "metadata": c.get("metadata", {}),
                        }
                    )
            except Exception:
                bm25_ranked = []

        # RRF fusion
        fused: dict[str, dict] = {}
        for rank, d in enumerate(dense):
            cid = d["chunk_id"]
            e = fused.setdefault(cid, {"chunk_id": cid, "text": d["text"], "metadata": d["metadata"],
                                       "dense_score": 0.0, "bm25_score": 0.0, "fused_score": 0.0})
            e["dense_score"] = d["score"]
            e["fused_score"] += 1.0 / (self.rrf_k + rank + 1)
        for rank, b in enumerate(bm25_ranked):
            cid = b["chunk_id"]
            e = fused.setdefault(cid, {"chunk_id": cid, "text": b["text"], "metadata": b["metadata"],
                                       "dense_score": 0.0, "bm25_score": 0.0, "fused_score": 0.0})
            e["bm25_score"] = b["score"]
            e["fused_score"] += 1.0 / (self.rrf_k + rank + 1)

        ranked = sorted(fused.values(), key=lambda x: x["fused_score"], reverse=True)
        return ranked[: self.top_k_fused]
