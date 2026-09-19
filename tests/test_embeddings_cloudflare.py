"""Cloudflare Vectors embedder (deploy fix 2026-09-11).

Remote Cloudflare Workers AI text-embeddings replaces on-box FastEmbed so
Render free tier (512Mi) no longer OOMs during ingestion.
"""
from unittest.mock import Mock, patch

import numpy as np

import maia.embeddings as emb


def _patch_settings(**kwargs):
    from maia import embeddings as m

    old = {}
    for k, v in kwargs.items():
        old[k] = getattr(m.settings, k)
        setattr(m.settings, k, v)
    return old


def _restore(patches):
    for k, v in patches.items():
        setattr(emb.settings, k, v)


def test_cloudflare_backend_used_with_creds(monkeypatch):
    """With Cloudflare creds set, Embedder selects cloudflare mode."""
    patches = _patch_settings(CLOUDFLARE_ACCOUNT_ID="acc", CLOUDFLARE_API_TOKEN="tok")
    try:
        monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
        fake = Mock()
        fake.mode = "cloudflare"
        fake.embed.return_value = np.zeros((1, 1024), dtype=np.float32)
        with patch("maia.embeddings.CloudflareEmbedder", return_value=fake):
            with patch.object(emb, "_embed_singleton", None):
                e = emb.get_embedder()
        assert e._backend is fake
        assert e.mode == "cloudflare"
    finally:
        _restore(patches)


def test_no_creds_falls_back_to_fastembed_or_hash(monkeypatch):
    """Without Cloudflare creds, degrade to fastembed/hash — never crash."""
    patches = _patch_settings(CLOUDFLARE_ACCOUNT_ID="", CLOUDFLARE_API_TOKEN="")
    try:
        monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
        with patch.object(emb, "_embed_singleton", None):
            e = emb.get_embedder()
        assert e.mode in ("fastembed", "hash")
    finally:
        _restore(patches)


def test_force_hash_env(monkeypatch):
    """MAIA_EMBED_FORCE_HASH=1 forces hash mode (CI/offline)."""
    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "1")
    with patch.object(emb, "_embed_singleton", None):
        e = emb.get_embedder()
    assert e.mode == "hash"
    assert e.embed(["hello world"]).shape == (1, e.dim)


def test_hash_embed_dim_uses_embed_dim(monkeypatch):
    """Hash embeddings use the configured dim (CI passes 384/1024)."""
    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "1")
    with patch.object(emb, "_embed_singleton", None):
        e = emb.get_embedder()
    v = e.embed(["a b c"])
    assert v.shape == (1, e.dim)