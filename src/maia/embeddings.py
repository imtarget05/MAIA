"""Embeddings via HuggingFace (FastEmbed ONNX, no torch needed).

Model default: BAAI/bge-small-en-v1.5 (384 dim) from HF Hub.
Falls back to deterministic hash embedding if fastembed unavailable
(useful for CI/offline tests - NOT for production quality).
"""
import hashlib
import os
import numpy as np


class Embedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", dim: int = 384):
        self.model = model
        self.dim = dim
        self._backend = None
        self._mode = "hash"
        # MAIA_EMBED_FORCE_HASH=1 -> deterministic offline hash embedding
        # (no model download, no HF Hub/network). Used by CI/CD and offline tests.
        force_hash = os.environ.get("MAIA_EMBED_FORCE_HASH", "").strip()
        if not force_hash:
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
                vecs = list(self._backend.embed(texts))
                return np.array(vecs, dtype=np.float32)
            except Exception as e:
                print(f"[embeddings] fastembed error, fallback: {e}")
        # hash fallback: deterministic pseudo-embedding
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
