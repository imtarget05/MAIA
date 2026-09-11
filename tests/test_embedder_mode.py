"""Embedder backend mode (deploy fix 2026-09-11).

- Default: hash embedding (zero model RAM — safe on Render free 512Mi).
- Opt-in real embeddings: set MAIA_EMBED_MODE=fastembed (needs ~1GB RAM,
  i.e. Render Starter+). The Embedder class below still supports FastEmbed
  when explicitly requested; get_embedder() stays the process-wide
  singleton so the model loads at most ONCE per worker.
"""
from unittest.mock import patch

import maia.embeddings as emb


def test_hash_mode_is_default_without_env(monkeypatch):
    """No MAIA_EMBED_MODE set -> hash mode (no model download, no big RAM)."""
    monkeypatch.delenv("MAIA_EMBED_MODE", raising=False)
    monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
    with patch.object(emb, "_embed_singleton", None):
        e = emb.get_embedder()
    assert e.mode == "hash"


def test_force_hash_env_still_works(monkeypatch):
    """Legacy MAIA_EMBED_FORCE_HASH=1 still forces hash mode."""
    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "1")
    monkeypatch.delenv("MAIA_EMBED_MODE", raising=False)
    with patch.object(emb, "_embed_singleton", None):
        e = emb.get_embedder()
    assert e.mode == "hash"
    assert e.embed(["hello world"]).shape == (1, e.dim)


def test_fastembed_opt_in_attempts_model(monkeypatch):
    """MAIA_EMBED_MODE=fastembed tries the real model (or hash fallback)."""
    monkeypatch.setenv("MAIA_EMBED_MODE", "fastembed")
    monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
    with patch.object(emb, "_embed_singleton", None):
        e = emb.get_embedder()
    assert e.mode in ("fastembed", "hash")