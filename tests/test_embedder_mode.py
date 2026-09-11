"""Embedder backend mode (deploy fix 2026-09-11).

- Production default: fastembed (real sentence vectors) loaded at most ONCE
  per worker via the get_embedder() singleton.
- MAIA_EMBED_FORCE_HASH=1 forces hash embedding — CI / offline test
  runners only (conftest sets it; hash vectors are too sparse for
  production retrieval quality).
"""
import sys
from unittest.mock import MagicMock, patch

import maia.embeddings as emb


def test_fastembed_is_default_without_env(monkeypatch):
    """No env -> Embedder tries real fastembed model (NOT hash)."""
    monkeypatch.delenv("MAIA_EMBED_MODE", raising=False)
    monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)

    fake_backend = MagicMock()
    fake_backend.embed.return_value = iter([[1.0] * 6])

    class FakeTextEmbedding:
        def __init__(self, model_name):
            self._vec = [1.0] * 6

        def embed(self, texts):
            return iter([[1.0] * 6])

    with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=FakeTextEmbedding)}):
        with patch.object(emb, "_embed_singleton", None):
            e = emb.get_embedder()
    assert e.mode == "fastembed"
    assert e.dim == 6


def test_force_hash_env_still_works(monkeypatch):
    """MAIA_EMBED_FORCE_HASH=1 forces hash mode (CI/offline)."""
    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "1")
    monkeypatch.delenv("MAIA_EMBED_MODE", raising=False)
    with patch.object(emb, "_embed_singleton", None):
        e = emb.get_embedder()
    assert e.mode == "hash"
    assert e.embed(["hello world"]).shape == (1, e.dim)


def test_fastembed_unavailable_falls_back_to_hash(monkeypatch):
    """When fastembed import/load fails, degrade to hash (never crash)."""
    monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
    with patch.dict(sys.modules, {"fastembed": None}):
        with patch.object(emb, "_embed_singleton", None):
            e = emb.get_embedder()
    assert e.mode == "hash"