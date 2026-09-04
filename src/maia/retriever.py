"""Hybrid retrieval (§7, verify: hybrid): dense (Qdrant) + sparse (BM25) -> RRF fusion.

Exact algorithm documented here (per spec: do not claim without implementation):
- dense: Qdrant cosine top-k_d
- sparse: rank_bm25.OkapiBM25 over cached corpus (tokenized lowercase)
- fusion: Reciprocal Rank Fusion score = sum(1/(RRF_K + rank)) per chunk_id
- output top-k_fused with fused_score + dense_score + bm25_score
"""
import pickle
from pathlib import Path

import numpy as np


class HybridRetriever:
    def __init__(self, store, embedder, storage_dir: str = "./storage",
                 top_k_dense: int = 10, top_k_bm25: int = 10, top_k_fused: int = 8, rrf_k: int = 60):
        self.store = store
        self.embedder = embedder
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.top_k_dense = top_k_dense
        self.top_k_bm25 = top_k_bm25
        self.top_k_fused = top_k_fused
        self.rrf_k = rrf_k
        self._bm25 = None
        self._corpus: list[dict] = []  # [{chunk_id, text, metadata}]
        self._load_or_rebuild()

    # ---- corpus ----
    def _cache_path(self) -> Path:
        return self.storage_dir / "bm25_corpus.pkl"

    def _load_or_rebuild(self):
        cp = self._cache_path()
        if cp.exists():
            try:
                with open(cp, "rb") as f:
                    self._corpus = pickle.load(f)
                self._build_bm25()
                return
            except Exception:
                pass
        self.rebuild()

    def rebuild(self):
        try:
            self._corpus = self.store.scroll_all()
        except Exception:
            self._corpus = []
        self._build_bm25()
        try:
            with open(self._cache_path(), "wb") as f:
                pickle.dump(self._corpus, f)
        except Exception:
            pass

    def _build_bm25(self):
        if not self._corpus:
            self._bm25 = None
            return
        try:
            from rank_bm25 import BM25Okapi

            tokenized = [c["text"].lower().split() for c in self._corpus]
            self._bm25 = BM25Okapi(tokenized)
        except Exception:
            self._bm25 = None

    # ---- retrieve ----
    def retrieve(self, query: str) -> list[dict]:
        qvec = self.embedder.embed_query(query)
        dense = self.store.search(qvec, top_k=self.top_k_dense)

        # BM25
        bm25_ranked: list[dict] = []
        if self._bm25 is not None and self._corpus:
            try:
                scores = self._bm25.get_scores(query.lower().split())
                idx = np.argsort(scores)[::-1][: self.top_k_bm25]
                for i in idx:
                    if scores[i] <= 0:
                        continue
                    c = self._corpus[int(i)]
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
