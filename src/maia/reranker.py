"""Reranking (verify: rerank) - exact component documented.

Primary: cross-encoder/ms-marco-MiniLM-L-6-v2 via sentence-transformers
Fallback (no torch): keep RRF order, expose rerank_score = fused_score.
"""
import sys


class Reranker:
    def __init__(self, model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self.model_name = model
        self._model = None
        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(model)
        except Exception as e:
            print(f"[reranker] CrossEncoder unavailable ({e}), using score fallback", file=sys.stderr)

    @property
    def mode(self) -> str:
        return "cross-encoder" if self._model is not None else "fallback"

    def rerank(self, query: str, candidates: list[dict], top_k: int = 3) -> list[dict]:
        if not candidates:
            return []
        if self._model is None:
            for c in candidates:
                c["rerank_score"] = float(c.get("fused_score", 0.0))
            return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)[:top_k]
        try:
            pairs = [(query, c["text"]) for c in candidates]
            scores = self._model.predict(pairs)
            for c, s in zip(candidates, scores):
                c["rerank_score"] = float(s)
            return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)[:top_k]
        except Exception as e:
            print(f"[reranker] predict failed: {e}", file=sys.stderr)
            for c in candidates:
                c["rerank_score"] = float(c.get("fused_score", 0.0))
            return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)[:top_k]
