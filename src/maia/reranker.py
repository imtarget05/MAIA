"""Reranking (verify: rerank) - exact component documented.

Primary: cross-encoder/ms-marco-MiniLM-L-6-v2 via sentence-transformers
Fallback (no torch): keep RRF order, expose rerank_score = fused_score.
"""
import sys
import threading

# Single source of truth for the default model. The DEFAULT MODEL IS NOT
# CHANGING: cross-encoder/ms-marco-MiniLM-L-6-v2 stays the default; promoting a
# multilingual cross-encoder is a separate, later task with its own eval.
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class Reranker:
    def __init__(self, model: str = DEFAULT_RERANK_MODEL):
        self.model_name = model
        self._model = None
        try:
            from sentence_transformers import (  # pyright: ignore[reportMissingImports] - optional dep (requirements-rerank.txt)
                CrossEncoder,
            )

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

# ---------------------------------------------------------------------------
# Process-wide cache
# ---------------------------------------------------------------------------
# A Reranker is a PURE function of (query, candidate text): it holds no tenant,
# session, request or mutable-configuration state (see the class body above --
# the only attributes are ``model_name`` and the loaded ``_model``, and
# ``rerank()`` mutates only the candidate dicts it is handed). So one instance
# is safely shared across tenants and sessions inside a process, and
# reconstructing it per request re-loads ~90MB of weights for nothing.
_reranker_cache: dict[str, Reranker] = {}
_reranker_lock = threading.Lock()


def get_reranker(model: str | None = None) -> Reranker:
    """Return the process-wide Reranker for ``model``, constructing it once.

    The cache is KEYED BY MODEL NAME, not a single un-keyed global. Reason:
    ``Reranker.__init__`` takes a ``model`` argument, so an un-keyed global
    would let ``get_reranker("B")`` silently return the instance built for "A"
    -- a wrong-model result with no error, which is far worse than an extra
    load. Keying keeps the common path (default model, repeated calls) at
    exactly one construction while making a model change explicit and correct.

    CI contract: after installing requirements-rerank.txt the returned
    instance must expose ``mode == "cross-encoder"``. The fallback path
    keeps tests offline-green without the heavy torch install.
    """
    key = model or DEFAULT_RERANK_MODEL
    cached = _reranker_cache.get(key)
    if cached is not None:
        return cached
    with _reranker_lock:
        # Double-checked: construction is slow (~30s of model load) and must
        # happen once even if several threads race in from a warm server.
        cached = _reranker_cache.get(key)
        if cached is None:
            cached = Reranker(key)
            _reranker_cache[key] = cached
    return cached


def resident_reranker_mode(model: str | None = None) -> str:
    """Report the rerank mode of an ALREADY-CONSTRUCTED instance, or "not-loaded".

    Non-constructing on purpose: health/readiness probes must not pay a ~30s
    model load and must not mutate global state (i.e. must not be the thing
    that populates the cache and pins ~90MB in a liveness check).
    """
    key = model or DEFAULT_RERANK_MODEL
    with _reranker_lock:
        cached = _reranker_cache.get(key)
    return cached.mode if cached is not None else "not-loaded"


def reset_reranker_cache(model: str | None = None) -> int:
    """Drop cached Reranker instance(s); returns how many were released.

    Deterministic escape hatch, required in three places:

    * tests -- without it the first test that warms the cache leaks an instance
      (and ~90MB) into every later test, and monkeypatching ``Reranker`` in a
      later test would silently return the stale earlier instance;
    * config / model changes -- flip ``model=`` or the settings that feed it and
      call this so the next query rebuilds;
    * memory pressure -- the cache holds model weights for the process
      lifetime, so an operator may want to release them.

    ``model=None`` clears every entry; pass a model name to clear just that one.
    """
    with _reranker_lock:
        if model is None:
            dropped = len(_reranker_cache)
            _reranker_cache.clear()
        else:
            dropped = 1 if _reranker_cache.pop(model, None) is not None else 0
    return dropped
