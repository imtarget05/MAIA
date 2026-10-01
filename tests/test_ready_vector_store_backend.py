"""`/ready` must report the vector-store backend that is actually configured.

Root cause: `api.ready()` constructed `QdrantStore(...)` directly instead of
going through `build_vector_store` (the factory `pipeline_query.build_stack`
uses). With `VECTOR_STORE_BACKEND=azure_ai_search` the readiness probe still
probed Qdrant while every real query hit AI Search -- a green probe over a
backend nothing else was using, which is the failure a readiness probe exists
to catch.

The contract pinned here:
* the probe resolves through `maia.retrieval_backends.build_vector_store`,
* it publishes the resolved backend identity as `vector_store_backend`,
* an unresolvable backend stays FAIL-CLOSED (`status: degraded`), and no
  dependency error is ever translated into a different backend's name.

Fully offline: no real Qdrant / AI Search client is constructed. Every test
stubs the store class or picks the SDK-free `memory` backend.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from maia import api
from maia import retrieval_backends as rb


class _CountingStore:
    """Stand-in for `QdrantStore`: count() must answer, nothing else runs."""

    def __init__(self, url: str, collection: str, dim: int, api_key: str = "") -> None:
        self.url = url
        self.collection = collection
        self.dim = dim
        self.api_key = api_key
        self.client = object()

    def count(self) -> int:
        return 7


class _StubDB:
    """Just enough for the three `.query(...).count()` calls in the handler."""

    def query(self, *_a, **_k):
        class _Res:
            def count(self) -> int:
                return 0

            def filter(self, *_a, **_k):
                return self

        return _Res()


def _ready() -> dict:
    return TestClient(api.app).get("/ready").json()


def test_ready_resolves_the_memory_backend_without_touching_qdrant(monkeypatch):
    """The proof that /ready is not hardcoded to Qdrant: with a non-Qdrant
    backend configured, Qdrant must not be constructed AT ALL and the probe
    must succeed against the backend that is really in use."""
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "memory")
    with patch.object(
        rb.QdrantStore, "__init__", side_effect=AssertionError("Qdrant must not be built")
    ):
        body = _ready()

    assert body["status"] == "ok", body
    assert body["vector_store_backend"] == rb.BACKEND_MEMORY
    assert body["qdrant_points"] == 0, "the in-memory store is genuinely empty"


def test_ready_reports_qdrant_for_the_qdrant_backend(monkeypatch):
    """The legacy field names and the Qdrant identity are preserved."""
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", rb.BACKEND_QDRANT)
    with patch.object(rb, "QdrantStore", _CountingStore):
        body = _ready()

    assert body["status"] == "ok", body
    assert body["vector_store_backend"] == rb.BACKEND_QDRANT
    assert body["qdrant_points"] == 7
    assert body["collection"] == rb.settings.QDRANT_COLLECTION


def test_ready_does_not_report_a_qdrant_status_for_azure_ai_search(monkeypatch):
    """`azure_ai_search` must never be reported as Qdrant.

    Whether the Azure SDK is installed decides HOW it fails
    (AzureSearchUnavailable vs. a reachable service), never WHICH backend is
    named: both are domain failures that must surface as `status: degraded`.
    """
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", rb.BACKEND_AZURE_AI_SEARCH)
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_ENDPOINT", "https://maia.search.windows.net")
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_INDEX", "maia_knowledge")

    with patch.object(
        rb.QdrantStore, "__init__", side_effect=AssertionError("Qdrant must not be built")
    ):
        body = _ready()

    if body["status"] == "ok":
        assert body["vector_store_backend"] == rb.BACKEND_AZURE_AI_SEARCH
    else:
        assert body.get("vector_store_backend") in (None, rb.BACKEND_AZURE_AI_SEARCH)
        assert rb.BACKEND_QDRANT not in str(body.get("error"))


def test_ready_fails_closed_on_an_unknown_backend(monkeypatch):
    """A typo must not produce a green probe over SOME backend."""
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "pinecone")
    with patch.object(
        rb.QdrantStore, "__init__", side_effect=AssertionError("Qdrant must not be built")
    ):
        body = _ready()

    assert body["status"] == "degraded"
    assert "BackendConfigurationError" in body["error"]
    assert "pinecone" in body["error"]


def test_ready_fails_closed_when_the_backend_dependency_is_missing(monkeypatch):
    """A dependency error must stay a dependency error: it may not be
    re-labelled as another backend."""
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", rb.BACKEND_AZURE_AI_SEARCH)
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_ENDPOINT", "https://maia.search.windows.net")
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_INDEX", "maia_knowledge")

    with patch.object(
        rb.AzureAISearchAdapter, "_make_client",
        side_effect=rb.AzureSearchUnavailable("no sdk"),
    ):
        body = _ready()

    assert body["status"] == "degraded"
    assert "AzureSearchUnavailable" in body["error"]


def test_ready_still_never_loads_the_embedding_model(monkeypatch):
    """The original OOM guard must survive the factory swap: /ready must stay
    cheap, so it must NOT construct an Embedder to learn `dim`."""
    import maia.embeddings as emb

    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", rb.BACKEND_MEMORY)
    with patch.object(emb, "Embedder", side_effect=AssertionError("must not load model")):
        body = _ready()

    assert body["status"] == "ok", body


def test_the_backend_name_helper_normalises_like_the_factory_does(monkeypatch):
    """`/ready` reads the name through the same normaliser the dispatch uses,
    so `AZURE_AI_SEARCH` in an env file behaves identically."""
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "  QDRANT  ")
    assert rb.resolve_backend_name() == rb.BACKEND_QDRANT

    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "Azure_AI_Search")
    assert rb.resolve_backend_name() == rb.BACKEND_AZURE_AI_SEARCH


def test_the_backend_name_helper_rejects_an_unknown_value(monkeypatch):
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "pinecone")
    with pytest.raises(rb.BackendConfigurationError) as exc:
        rb.resolve_backend_name()
    for name in rb.SUPPORTED_BACKENDS:
        assert name in str(exc.value)


def test_admin_stats_reports_the_generation_state_too(monkeypatch):
    """`/admin/stats` publishes `llm_mode` as well; the outcome belongs next to
    it there too, otherwise the two surfaces disagree about the same object.

    The handler is called directly with a stubbed DB rather than through the
    router: `/admin/stats` is behind `get_current_admin_user`, and what this
    test is about is the payload, not the RBAC gate (covered by test_p2_rbac).
    """
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", rb.BACKEND_MEMORY)

    stats = api.get_admin_stats(db=_StubDB(), current_user=object())  # type: ignore[arg-type]

    assert stats["llm_mode"] == "local"
    assert stats["llm_generation_state"] in ("ok", "degraded", "mock", "unknown")