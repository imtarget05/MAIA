"""Embeddings via Cloudflare Workers AI (BGE-M3, 1024-dim) — deploy fix.

Production uses remote Cloudflare ``text-embeddings`` (small HTTP POST, no
ONNX model in the web service): the on-box FastEmbed model OOM-killed Render
free tier (512Mi) during ingestion. Embedding compute moved out of the
worker -> the API server stays ~150Mi and fits the free plan.

Fallbacks (degraded, never crash): fastembed (if installed) -> deterministic
hash embedding (CI/offline tests only — too sparse for production retrieval).

The one exception is a vector-width mismatch, which raises
``EmbeddingDimMismatch`` rather than degrading: hash vectors of the configured
width would upsert cleanly and then be retrieved as though they were semantic.
"""
import hashlib
import os
import sys
import threading

import numpy as np
import requests

from .config import settings
from .embeddings_cloudflare import CloudflareEmbedder, EmbeddingDimMismatch
from .llm_endpoints import project_headers, resolve_base_urls, split_timeout

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


# Provider names that mean "use the OpenAI-compatible /v1/embeddings endpoint
# (the centralized llm-gateway, or the LAN upstream behind it)".
GATEWAY_EMBEDDING_PROVIDERS = ("local_openai", "lmstudio", "lms", "ollama", "gateway")


class LocalOpenAICompatEmbedder:
    """Embeddings via the OpenAI-compatible ``/v1/embeddings`` endpoint.

    Requests are sent to the centralized llm-gateway first, carrying the
    ``X-Project: MAIA`` header so embedding usage is attributable in the
    gateway's central telemetry, and fall back to the direct LAN upstream
    only when the gateway refuses the connection (see
    ``maia.llm_endpoints``). A vector width that disagrees with the
    configured ``dim`` raises ``EmbeddingDimMismatch`` instead of degrading:
    a wrong-width vector would upsert cleanly and then be retrieved as if it
    were meaningful.
    """

    def __init__(self, base_url: str = "", model: str = "", dim: int | None = None,
                 api_key: str = "", timeout: int | None = None):
        self.base_urls = resolve_base_urls(
            base_url or settings.EMBEDDINGS_BASE_URL,
            settings.LLM_GATEWAY_URL,
            settings.LLM_DIRECT_UPSTREAM_URL,
        )
        self.base_url = self.base_urls[0] if self.base_urls else ""
        self.model = (model or settings.EMBEDDINGS_MODEL).strip()
        self.dim = int(dim or settings.EMBEDDINGS_DIM or settings.EMBED_DIM)
        self.api_key = (api_key or settings.EMBEDDINGS_API_KEY).strip()
        self.timeout = int(timeout or settings.EMBEDDINGS_TIMEOUT_SEC)
        self.connect_timeout = float(settings.EMBEDDINGS_CONNECT_TIMEOUT_SEC)

    @property
    def mode(self) -> str:
        return "local_openai"

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if not self.base_urls:
            raise ConnectionError("no embeddings base URL configured")
        payload = {"model": self.model, "input": list(texts)}
        last_error: Exception | None = None
        for base_url in self.base_urls:
            headers = project_headers()
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            try:
                r = requests.post(
                    f"{base_url}/embeddings",
                    headers=headers,
                    json=payload,
                    timeout=split_timeout(self.timeout, self.connect_timeout),
                )
                r.raise_for_status()
                data = r.json().get("data") or []
                vecs = [d["embedding"] if isinstance(d, dict) else d for d in data]
            except requests.exceptions.ConnectionError as e:
                # Gateway (or host) not listening: safe to try the next
                # candidate, the batch was never executed anywhere.
                last_error = e
                continue
            arr = np.array(vecs, dtype=np.float32)
            if arr.ndim != 2:
                raise ValueError(f"expected a 2-D embedding matrix, got shape {arr.shape}")
            if arr.shape[1] != self.dim:
                raise EmbeddingDimMismatch(
                    f"embedding width {arr.shape[1]} != configured {self.dim}; "
                    "fix EMBEDDINGS_DIM before ingesting more documents"
                )
            return arr
        raise ConnectionError(
            f"no reachable embeddings endpoint among {self.base_urls}: {last_error}"
        )


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
        # Opt-in only: the gateway/LAN /v1/embeddings endpoint. Default ""
        # (auto) keeps the pre-existing chain below unchanged.
        provider = (settings.EMBEDDINGS_PROVIDER or "").strip().lower()
        if provider in GATEWAY_EMBEDDING_PROVIDERS:
            self._backend = LocalOpenAICompatEmbedder(
                model=self.model, dim=self.dim,
            )
            self._mode = "local_openai"
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
            # The configured EMBED_MODEL (BGE-M3) is 1024-dim. Previously this
            # was a bare literal that disagreed with the 384 passed into
            # CloudflareEmbedder, so the collection was created at 1024 while
            # the backend's own degraded hash fallback still produced 384-dim
            # vectors. One source of truth now.
            self.dim = settings.CLOUDFLARE_EMBED_DIM
            cf.dim = self.dim
            return
        # Fallback 1: fastembed (if installed and reachable)
        try:
            from fastembed import TextEmbedding

            self._backend = TextEmbedding(
                model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
            )
            # paraphrase-multilingual-MiniLM-L12-v2 is genuinely 384-dim, which
            # is what settings.EMBED_DIM describes (see config.py).
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
                # fastembed returns a lazy iterable; the cloudflare backend
                # returns an ndarray — normalize so callers always get one.
                res = self._backend.embed(texts)
                return res if isinstance(res, np.ndarray) else np.asarray(list(res), dtype=np.float32)
            except EmbeddingDimMismatch:
                # A width mismatch means the model and CLOUDFLARE_EMBED_DIM
                # disagree. Falling back to hash vectors here would write
                # meaningless vectors into a correctly-sized collection and
                # serve them as real matches, so propagate instead.
                raise
            except Exception as e:
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