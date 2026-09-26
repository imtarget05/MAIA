"""Cloudflare Vectors embeddings instead of FastEmbed (deploy fix 2026-09-11).

Root cause: the on-box FastEmbed ONNX model could not run on Render free
tier — ingestion OOM-killed the 512Mi instance (``server_failed`` events).
Cloudflare Workers AI ``@cf/baai/bge-m3`` (text-embeddings, 1024-dim)
moved the embedding compute out of the web service: the API server only
makes a small HTTP POST per batch, so peak memory stays ~150Mi and the
free plan suffices. The Qdrant collection needs 1024d (BGE-m3).
"""
import os

import numpy as np
import requests

from .config import settings

_CF_EMBED_MODEL = os.environ.get("MAIA_EMBED_MODEL", "@cf/baai/bge-m3")


class EmbeddingDimMismatch(RuntimeError):
    """The backend returned vectors of a different width than configured.

    This is a configuration error, not a transient fault, so it is deliberately
    NOT absorbed by the degraded-mode fallback. Hash vectors of the configured
    width would upsert cleanly and then be retrieved as if they carried
    meaning, turning a loud misconfiguration into silently wrong answers.
    """


class CloudflareEmbedder:
    """Batched text embeddings via Cloudflare Workers AI.

    - POST /client/v4/accounts/{account}/ai/run/{model}  (instruct embedding
      models want the prefix "Represent this sentence ..."). We handle both
      response shapes: {"result":{"data":[{...}]}} and {"result":{"text":"..."}}.
    - Graceful degraded mode: when creds are missing or the call fails, falls back
      to a deterministic hash embedding so nothing crashes. The fallback uses the
      *same* dimension as the live path (``CLOUDFLARE_EMBED_DIM``), because the
      Qdrant collection has already been created at that width by the time the
      first embed happens — a fallback vector of a different width would fail to
      upsert.
    """

    def __init__(self, account_id: str = "", api_token: str = "",
                 model: str = _CF_EMBED_MODEL,
                 dim: int | None = None):
        self.account_id = (account_id or "").strip()
        self.api_token = (api_token or "").strip()
        self.model = (model or _CF_EMBED_MODEL).strip()
        # Default to the configured EMBED_MODEL's width, not settings.EMBED_DIM
        # (which describes the offline fastembed/hash fallbacks). Callers that
        # know better (Embedder) pass dim explicitly.
        self.dim = int(dim or settings.CLOUDFLARE_EMBED_DIM)
        self._mode = "cloudflare" if (self.account_id and self.api_token) else "hash"
        self._session = requests.Session()

    @property
    def mode(self) -> str:
        return self._mode

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._mode != "cloudflare" or not texts:
            return self._hash_embed(texts)
        try:
            url = (f"https://api.cloudflare.com/client/v4/accounts/"
                   f"{self.account_id}/ai/run/{self.model}")
            headers = {"Authorization": f"Bearer {self.api_token}",
                       "Content-Type": "application/json"}
            payload = {"text": texts}
            r = self._session.post(url, headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            result = r.json().get("result", {})
            if isinstance(result, dict) and "data" in result:
                vecs = [d["embedding"] if isinstance(d, dict) else d
                        for d in result["data"]]
                arr = np.array(vecs, dtype=np.float32)
                if arr.ndim != 2:
                    raise ValueError(
                        f"expected a 2-D embedding matrix, got shape {arr.shape}"
                    )
                # Resolved the TODO(embedding-dim): the dimension is now pinned
                # by configuration and validated, rather than inferred and
                # silently reassigned. Reassigning self.dim here used to change
                # the vector width *after* the Qdrant collection had already
                # been created at the configured width, which surfaced much later
                # as an opaque upsert failure.
                if arr.shape[1] != self.dim:
                    raise EmbeddingDimMismatch(
                        f"{self.model} returned {arr.shape[1]}-dim vectors but "
                        f"CLOUDFLARE_EMBED_DIM is {self.dim}; the Qdrant "
                        "collection is created at the configured width, so these "
                        "vectors would not upsert. Set CLOUDFLARE_EMBED_DIM to "
                        "the model's real output width."
                    )
                return arr
            raise ValueError(f"unexpected Cloudflare embedding response: {r.text[:200]}")
        except EmbeddingDimMismatch:
            raise
        except Exception as e:
            print(f"[embeddings] cloudflare embed failed ({e}), hash fallback", file=__import__("sys").stderr)
            return self._hash_embed(texts)

    def embed_query(self, text: str) -> np.ndarray:
        # BGE instruct models: prefix question for retrieval. NOTE: the
        # corpus was embedded as plain text, but the suffix is fixed across
        # all docs, so cosine ranking is unaffected.
        return self.embed([f"Represent this sentence for searching relevant passages: {text}"])[0]

    def _hash_embed(self, texts: list[str]) -> np.ndarray:
        return _hash_embed(texts, self.dim)


def _hash_embed(texts: list[str], dim: int) -> np.ndarray:
    import hashlib

    out = []
    for t in texts:
        v = np.zeros(dim, dtype=np.float32)
        for tok in t.lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % dim] += 1.0
        n = np.linalg.norm(v)
        if n > 0:
            v = v / n
        out.append(v)
    return np.array(out, dtype=np.float32)