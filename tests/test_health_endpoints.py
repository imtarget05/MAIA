"""Health vs readiness probes (deploy fix 2026-09-11).

Root cause: GET /health called build_stack() which loads the FastEmbed
ONNX model (~hundreds of MB) per request. On Render free tier (512Mi)
combined with the already-loaded app, the instance OOMs and crash-loops
(server_failed events, HTTP 502).

Contract:
- GET /health -> lightweight liveness probe, NEVER builds the ML stack.
  Always 200 {"status": "ok"} (+ version). Safe for Render health checks.
- GET /ready -> readiness probe: checks Qdrant connectivity + reports llm /
  rerank modes WITHOUT loading the embedding model (embed check is lazy).
  Used by scripts/deploy_check.py instead of /health.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from maia import api


def _client() -> TestClient:
    return TestClient(api.app)


def test_health_is_lightweight_liveness():
    """GET /health must NOT touch build_stack (no model load -> no OOM)."""
    with patch.object(api, "build_stack", side_effect=AssertionError("must not build stack")):
        r = _client().get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_does_not_load_embed_model():
    """GET /ready must NOT construct Embedder (ONNX model load OOMs 512Mi)."""
    import maia.embeddings as emb

    with patch.object(emb, "Embedder", side_effect=AssertionError("must not load model")):
        r = _client().get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] in ("ok", "degraded")


def test_embedder_singleton_reused():
    """build_stack must reuse ONE Embedder (no per-request model reload -> OOM)."""
    import maia.embeddings as emb

    with patch.object(emb, "Embedder") as mock_cls, patch.object(emb, "_embed_singleton", None):
        from maia.pipeline_query import build_stack as bs

        with (
            patch("maia.pipeline_query.QdrantStore"),
            patch("maia.pipeline_query.HybridRetriever"),
            patch("maia.pipeline_query.Reranker"),
            patch("maia.pipeline_query.CloudflareLLM"),
        ):
            bs()
            bs()
    assert mock_cls.call_count == 1


def test_get_embedder_returns_same_instance():
    """get_embedder returns the identical object across calls."""
    import maia.embeddings as emb

    with patch.object(emb, "Embedder") as mock_cls, patch.object(emb, "_embed_singleton", None):
        first = emb.get_embedder()
        second = emb.get_embedder()
    assert first is second
    assert mock_cls.call_count == 1
