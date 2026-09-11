"""Health vs readiness probes (deploy fix 2026-09-11).

Root cause: GET /health called build_stack() which loads the FastEmbed
ONNX model (~hundreds of MB) per request. On Render free tier (512Mi)
combined with the already-loaded app, the instance OOMs and crash-loops
(server_failed events, HTTP 502).

Contract:
- GET /health -> lightweight liveness probe, NEVER builds the ML stack.
  Always 200 {"status": "ok"} (+ version). Safe for Render health checks.
- GET /ready -> full readiness probe: builds the stack, reports qdrant /
  llm / rerank state. Used by scripts/deploy_check.py instead of /health.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

import maia.api as api


def _client() -> TestClient:
    return TestClient(api.app)


def test_health_is_lightweight_liveness():
    """GET /health must NOT touch build_stack (no model load -> no OOM)."""
    with patch.object(api, "build_stack", side_effect=AssertionError("must not build stack")):
        r = _client().get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_reports_full_stack():
    """GET /ready builds the stack and reports qdrant/llm/rerank state."""
    from types import SimpleNamespace

    fake_store = SimpleNamespace(count=lambda: 7)
    fake_llm = SimpleNamespace(mode="mock")
    fake_reranker = SimpleNamespace(mode="fallback")
    with patch.object(
        api, "build_stack", return_value=(None, fake_store, None, fake_reranker, fake_llm)
    ):
        r = _client().get("/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["qdrant_points"] == 7
    assert data["llm_mode"] == "mock"
    assert data["rerank_mode"] == "fallback"


def test_ready_degraded_on_stack_error():
    """GET /ready returns degraded (not 500) when the stack fails."""
    with patch.object(api, "build_stack", side_effect=ConnectionError("no qdrant")):
        r = _client().get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
