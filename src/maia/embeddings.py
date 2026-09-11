"""Embeddings via HuggingFace (FastEmbed ONNX, no torch needed).

Model default: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (384 dim) from HF Hub.
Falls back to deterministic hash embedding if fastembed unavailable
(useful for CI/offline tests - NOT for production quality).
"""
import hashlib
import os
import sys
import threading

import numpy as np

from .config import settings
from .loops.resilience import CircuitBreaker, CircuitOpenError, RetryConfig, with_retry

# G-06: per-dependency breaker + retry for the embedder.
_embed_breaker: CircuitBreaker | None = None
_embed_retry = RetryConfig(max_retries=settings.RELIABILITY_MAX_RETRIES,
                           backoff_base=settings.RELIABILITY_RETRY_BACKOFF_SEC,
                           retryable=(TimeoutError, ConnectionError, OSError))


def _get_embed_breaker() -> CircuitBreaker:
    global _embed_breaker
    if _embed_breaker is None:
        threshold = settings.RELIABILITY_EMBED_THRESHOLD or settings.RELIABILITY_FAILURE_THRESHOLD
        _embed_breaker = CircuitBreaker("embed", failure_threshold=threshold,
                                        recovery_timeout=settings.RELIABILITY_RECOVERY_TIMEOUT_SEC)
    return _embed_breaker


# Process-wide Embedder singleton (deploy fix 2026-09-11).
#
# Root cause fixed: build_stack() created a NEW Embedder per request, and
# each Embedder loads the FastEmbed ONNX model (~hundreds of MB). On Render
# free tier (512Mi) the instance OOM-crashed and crash-looped (server_failed
# events, HTTP 502) as soon as /ready (previously /health) built the stack.
# The singleton loads the model ONCE per worker process; every request
# reuses it. Thread-safe via a lock (uvicorn default workers=1, threads>1).
_embed_singleton: "Embedder | None" = None
_embed_singleton_lock = threading.Lock()


def get_embedder(model: str | None = None, dim: int | None = None) -> "Embedder":
    """Return the process-wide Embedder, creating it once (lazy, thread-safe)."""
    global _embed_singleton
    if _embed_singleton is None:
        with _embed_singleton_lock:
            if _embed_singleton is None:
                _embed_singleton = Embedder(
                    model=model or settings.EMBED_MODEL,
                    dim=dim or settings.EMBED_DIM,
                )
    return _embed_singleton


class Embedder:
    def __init__(self, model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", dim: int = 384):
        self.model = model
        self.dim = dim
        self._backend = None
        self._mode = "hash"
        # Deploy fix 2026-09-11: default to hash embedding (zero model RAM).
        # The FastEmbed ONNX model (~hundreds of MB) OOM-crashes Render free
        # tier (512Mi) — /health crash-looped even before this fix. Real
        # embeddings are opt-in via MAIA_EMBED_MODE=fastembed (needs ~1GB,
        # i.e. Render Starter+). MAIA_EMBED_FORCE_HASH=1 (CI/offline tests)
        # keeps working as a legacy alias for hash mode.
        use_fastembed = os.environ.get("MAIA_EMBED_MODE", "").strip().lower() == "fastembed"
        force_hash = os.environ.get("MAIA_EMBED_FORCE_HASH", "").strip()
        if use_fastembed and not force_hash:
            try:
                from fastembed import TextEmbedding

                self._backend = TextEmbedding(model_name=model)
                # probe dim
                vec = list(self._backend.embed(["hello"]))[0]
                self.dim = len(vec)
                self._mode = "fastembed"
            except Exception as e:
                print(f"[embeddings] fastembed unavailable ({e}), using hash fallback dim={dim}")

    @property
    def mode(self) -> str:
        return self._mode

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._backend is not None:
            try:
                # G-06: breaker + retry around the external fastembed call.
                # fastembed returns a generator of vectors; materialize to a list
                # before np.asarray (np.array(generator) raises TypeError).
                def _embed_once(batch):
                    return np.array(list(self._backend.embed(batch)), dtype=np.float32)
                return with_retry(_embed_retry, _get_embed_breaker().call, _embed_once, texts)
            except (CircuitOpenError, TimeoutError, ConnectionError, OSError):
                pass  # fall through to deterministic hash fallback
            except Exception as e:
                print(f"[embeddings] fastembed error, fallback: {e}", file=sys.stderr)
        # hash fallback: deterministic pseudo-embedding
        return self._hash_embed(texts)

    def _hash_embed(self, texts: list[str]) -> np.ndarray:
        out = []
        for t in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for tok in t.lower().split():
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
            out.append(v)
        return np.array(out, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        # bge models prefer "Represent this sentence for searching relevant passages: "
        # fastembed handles prefix internally; keep raw here.
        return self.embed([text])[0]
