"""Embeddings via Cloudflare Workers AI (BGE-M3, 1024-dim) — deploy fix.

Production uses remote Cloudflare ``text-embeddings`` (small HTTP POST, no
ONNX model in the web service): the on-box FastEmbed model OOM-killed Render
free tier (512Mi) during ingestion. Embedding compute moved out of the
worker -> the API server stays ~150Mi and fits the free plan.

Fallbacks (degraded, never crash): fastembed (if installed) -> deterministic
hash embedding (CI/offline tests only — too sparse for production retrieval).
"""
import hashlib
import os
import sys
import threading

import numpy as np

from .config import settings
from .embeddings_cloudflare import CloudflareEmbedder

# Process-wide Embedder singleton (deploy fix 2026-09-11).
# Root cause fixed: build_stack() created a NEW Embedder per request, loading
# the FastEmbed ONNX model each time -> OOM crash-loop. The singleton reuses
# ONE embedder per worker process. Thread-safe via a lock (uvicorn
# default workers=1, threads>1).
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
    def __init__(self, model: str | None = None, dim: int | None = None):
        self.model = model or settings.EMBED_MODEL
        self.dim = int(dim or settings.EMBED_DIM)
        self._backend = None
        self._mode = "hash"
        force_hash = os.environ.get("MAIA_EMBED_FORCE_HASH", "").strip()
        if force_hash:
            print("[embeddings] MAIA_EMBED_FORCE_HASH=1 -> hash mode (CI/offline only)", file=sys.stderr)
            return
        # Production: Cloudflare Workers AI text-embeddings (no local model).
        cf = CloudflareEmbedder(
            account_id=settings.CLOUDFLARE_ACCOUNT_ID,
            api_token=settings.CLOUDFLARE_API_TOKEN,
            model=self.model,
            dim=self.dim,
        )
        if cf.mode == "cloudflare":
            self._backend = cf
            self._mode = "cloudflare"
            self.dim = 1024
            return
        # Fallback 1: fastembed (if installed and reachable)
        try:
            from fastembed import TextEmbedding

            self._backend = TextEmbedding(
                model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
            )
            self.dim = settings.EMBED_DIM
            self._mode = "fastembed"
        except Exception as e:
            print(
                f"[embeddings] fastembed unavailable ({e}), using hash fallback dim={self.dim}",
                file=sys.stderr,
            )

    @property
    def mode(self) -> str:
        return self._mode

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._backend is not None:
            try:
                return self._backend.embed(texts)
            except Exception as e:  # noqa: BLE001 - degraded fallback by design
                print(f"[embeddings] backend embed error, fallback: {e}", file=sys.stderr)
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
        # Cloudflare BGE instruct models: prefix the question for retrieval.
        # The corpus is embedded as plain text; the fixed suffix does not
        # affect cosine ranking. Skip the prefix in hash mode (CI/offline
        # tests) so the deterministic hash embedding is unchanged.
        if self._mode == "cloudflare":
            text = f"Represent this sentence for searching relevant passages: {text}"
        return self.embed([text])[0]