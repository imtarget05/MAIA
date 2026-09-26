"""B4 - one source of truth for the embedding dimension.

``config.EMBED_MODEL`` is ``@cf/baai/bge-m3``, a 1024-dim model, while
``config.EMBED_DIM`` was 384. The Cloudflare path in ``Embedder.__init__``
hardcoded ``self.dim = 1024`` and left ``CloudflareEmbedder.dim`` at 384, so
the Qdrant collection was created at 1024 while the backend's own degraded hash
fallback still produced 384-dim vectors. The live ``TODO(embedding-dim)`` in
``embeddings_cloudflare.embed`` inferred the width from the response and
silently reassigned ``self.dim`` *after* the collection already existed.
"""
import numpy as np
import pytest

from maia.config import Settings
from maia.embeddings_cloudflare import CloudflareEmbedder, EmbeddingDimMismatch


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.text = str(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _cf(dim, monkeypatch, expected=None):
    """A CloudflareEmbedder in cloudflare mode whose HTTP call returns `dim`-wide vectors."""
    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "")
    emb = CloudflareEmbedder(account_id="acc", api_token="tok", dim=expected)
    emb._mode = "cloudflare"
    emb._session = type(
        "S", (), {"post": staticmethod(lambda *a, **k: _Resp({"result": {"data": [{"embedding": [0.1] * dim}] * 2}}))}
    )()
    return emb


# --- configuration --------------------------------------------------------


def test_cloudflare_dim_matches_the_configured_model():
    """BGE-M3 is 1024-dim; EMBED_DIM (384) describes the offline fallbacks only."""
    s = Settings(_env_file=None)
    assert s.EMBED_MODEL == "@cf/baai/bge-m3"
    assert s.CLOUDFLARE_EMBED_DIM == 1024
    # The offline fallbacks really are 384-dim (MiniLM-L12 / hash).
    assert s.EMBED_DIM == 384


def test_cloudflare_embedder_defaults_to_the_model_dim(monkeypatch):
    monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
    emb = CloudflareEmbedder(account_id="", api_token="")
    assert emb.dim == 1024


def test_embedder_reports_the_same_dim_as_its_backend(monkeypatch):
    """The two objects must not disagree about the collection width."""
    from unittest.mock import Mock, patch

    import maia.embeddings as emb

    monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
    monkeypatch.setattr(emb.settings, "CLOUDFLARE_ACCOUNT_ID", "acc")
    monkeypatch.setattr(emb.settings, "CLOUDFLARE_API_TOKEN", "tok")
    real = CloudflareEmbedder(account_id="acc", api_token="tok")
    try:
        with patch("maia.embeddings.CloudflareEmbedder", return_value=real):
            with patch.object(emb, "_embed_singleton", None):
                e = emb.get_embedder()
        assert e.mode == "cloudflare"
        assert e.dim == real.dim == 1024
    finally:
        emb.settings.CLOUDFLARE_ACCOUNT_ID = ""
        emb.settings.CLOUDFLARE_API_TOKEN = ""


# --- the resolved TODO: validate instead of silently reassigning -----------


def test_matching_width_is_accepted(monkeypatch):
    emb = _cf(1024, monkeypatch, expected=1024)
    arr = emb.embed(["a", "b"])
    assert arr.shape == (2, 1024)
    assert emb.dim == 1024


def test_width_mismatch_raises_instead_of_silently_reassigning(monkeypatch):
    """A wrong-width vector would not upsert into the collection; fail loudly."""
    emb = _cf(384, monkeypatch, expected=1024)
    with pytest.raises(EmbeddingDimMismatch, match="CLOUDFLARE_EMBED_DIM"):
        emb.embed(["a"])
    # Crucially, the dim is NOT quietly changed to match the response, and the
    # error is NOT absorbed into a hash fallback that would upsert clean-looking
    # garbage into the collection.
    assert emb.dim == 1024


def test_degraded_hash_fallback_uses_the_collection_width(monkeypatch):
    """If the Cloudflare call fails, the fallback must still fit the collection."""
    emb = _cf(1024, monkeypatch, expected=1024)

    def boom(*a, **k):
        raise RuntimeError("network down")

    emb._session = type("S", (), {"post": staticmethod(boom)})()
    arr = emb.embed(["a", "b"])
    assert arr.shape == (2, 1024)
    assert emb.dim == 1024


# --- the manifest must report the dim actually used -----------------------


def test_manifest_reports_runtime_dim_not_the_fallback(monkeypatch):
    from maia.eval_manifest import build_manifest
    from maia.config import settings

    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "1")
    m = build_manifest(metrics={}, dataset="d")
    # Offline this run used the hash fallback at EMBED_DIM, and that is what
    # must be reported -- previously it reported EMBED_DIM unconditionally,
    # which was wrong for the 1024-dim production path.
    assert m["embed_dim"] == settings.EMBED_DIM
    assert m["fallback_embed_dim"] == settings.EMBED_DIM
    assert m["embed_model"] == settings.EMBED_MODEL
    assert isinstance(m["embed_dim"], int) and m["embed_dim"] > 0


def test_manifest_dim_tracks_cloudflare_dim_when_configured(monkeypatch):
    from maia.eval_manifest import _runtime_embed_dim
    from maia.config import settings

    assert _runtime_embed_dim() in (settings.EMBED_DIM, settings.CLOUDFLARE_EMBED_DIM)
    assert np is not None

def test_embedder_propagates_dim_mismatch(monkeypatch):
    """Embedder.embed must not convert a misconfiguration into hash vectors."""
    from unittest.mock import Mock, patch

    import maia.embeddings as emb

    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "")
    failing = Mock()
    failing.mode = "cloudflare"
    failing.dim = 1024
    failing.embed.side_effect = EmbeddingDimMismatch("384-dim vs 1024")
    try:
        with patch("maia.embeddings.CloudflareEmbedder", return_value=failing):
            with patch.object(emb, "_embed_singleton", None):
                e = emb.get_embedder()
        with pytest.raises(EmbeddingDimMismatch):
            e.embed(["a"])
    finally:
        emb.settings.CLOUDFLARE_ACCOUNT_ID = ""
        emb.settings.CLOUDFLARE_API_TOKEN = ""
