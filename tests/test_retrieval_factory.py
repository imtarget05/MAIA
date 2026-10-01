"""build_vector_store: backend dispatch, and the patch seam it must preserve.

The seam matters more than the dispatch. Two protected test files intercept
store construction by patching the symbol in `pipeline_query`'s OWN namespace:

    tests/test_health_endpoints.py:53   patch("maia.pipeline_query.QdrantStore")
    tests/test_reranker_lifecycle.py:83 monkeypatch.setattr(pq, "QdrantStore", ...)

`build_stack` may not be changed in this working tree, so the factory takes
the store CLASS as a parameter instead. `test_deferred_pipeline_query_wiring_
still_lets_the_protected_tests_patch_the_store` exercises that seam against
the real, unmodified `pipeline_query` so the deferred 3-line patch
(docs/azure-integration.md) is known-good before anyone applies it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import retrieval_backends as rb
from maia.retrieval_backends import (
    AzureAISearchAdapter,
    AzureSearchUnavailable,
    BackendConfigurationError,
    QdrantAdapter,
    build_vector_store,
)
from maia.retrieval_port import VectorSearchPort, VectorStorePort
from maia.test_utils import InMemoryVectorStore


class _RecordingStore:
    """Stands in for QdrantStore. Its __init__ is a no-op — the real one does a
    connectivity probe and collection DDL, which is exactly what must not run
    inside a unit test."""

    instances: ClassVar[list[_RecordingStore]] = []

    def __init__(self, url: str, collection: str, dim: int, api_key: str = "") -> None:
        self.url = url
        self.collection = collection
        self.dim = dim
        self.api_key = api_key
        self.client = object()
        type(self).instances.append(self)

    def search(self, query_vec, top_k: int = 10, score_threshold=None,
               tenant_id=None, extra_filter=None) -> list[dict]:
        return []

    def scroll_all(self, limit: int = 10000, tenant_id=None) -> list[dict]:
        return []

    def upsert(self, vectors, chunks) -> int:
        return 0

    def upsert_one(self, chunk_id: str, vector, payload: dict) -> str:
        return chunk_id

    def exists(self, chunk_id: str) -> bool:
        return False

    def delete_by_doc(self, doc_id: str) -> None:
        return None

    def count(self) -> int:
        return 0


@pytest.fixture
def clean_stores():
    _RecordingStore.instances.clear()
    yield
    _RecordingStore.instances.clear()


# --------------------------------------------------------------------------- #
# 1. the default path routes through the patchable module symbol
# --------------------------------------------------------------------------- #
def test_default_backend_is_qdrant(clean_stores, monkeypatch):
    monkeypatch.setattr(rb, "QdrantStore", _RecordingStore)
    store = build_vector_store(dim=384)
    assert isinstance(store, QdrantAdapter)
    assert isinstance(store, VectorStorePort)
    assert len(_RecordingStore.instances) == 1
    built = _RecordingStore.instances[0]
    assert built.dim == 384
    assert built.collection == rb.settings.QDRANT_COLLECTION
    assert built.url == rb.settings.QDRANT_URL


def test_default_backend_reads_the_setting(clean_stores, monkeypatch):
    monkeypatch.setattr(rb, "QdrantStore", _RecordingStore)
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "memory")
    assert isinstance(build_vector_store(dim=8), InMemoryVectorStore)
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "qdrant")
    assert isinstance(build_vector_store(dim=8), QdrantAdapter)


def test_explicit_backend_overrides_the_setting(clean_stores, monkeypatch):
    monkeypatch.setattr(rb, "QdrantStore", _RecordingStore)
    monkeypatch.setattr(rb.settings, "VECTOR_STORE_BACKEND", "qdrant")
    # memory wins even though the setting says qdrant: an explicit argument
    # must not be silently ignored.
    assert isinstance(build_vector_store(dim=8, backend="memory"), InMemoryVectorStore)


def test_patching_the_module_symbol_intercepts_construction(clean_stores, monkeypatch):
    """`patch("maia.retrieval_backends.QdrantStore")` is the seam a NEW test
    would use. It has to work, or the factory hides construction somewhere
    nobody can reach."""
    monkeypatch.setattr(rb, "QdrantStore", _RecordingStore)
    store = build_vector_store(dim=16)
    assert len(_RecordingStore.instances) == 1
    assert store._store is _RecordingStore.instances[0]


def test_explicit_store_class_wins_over_the_module_symbol(clean_stores, monkeypatch):
    monkeypatch.setattr(rb, "QdrantStore", _RecordingStore)
    other: list[_RecordingStore] = []

    class _Other(_RecordingStore):
        pass

    def _record(self, *a, **k):
        other.append(self)  # type: ignore[arg-type]
        _RecordingStore.__init__(self, *a, **k)

    _Other.instances = []  # type: ignore[assignment]
    _Other.__init__ = _record  # type: ignore[method-assign]
    build_vector_store(dim=1, qdrant_store_cls=_Other)
    assert len(other) == 1
    assert _RecordingStore.instances == [] or _RecordingStore.instances[0] is other[0]


def test_dim_is_a_required_keyword(clean_stores):
    """The Qdrant collection is created from the RUNTIME embedder dim, so
    passing it explicitly is what stops a wrong-width collection."""
    with pytest.raises(TypeError):
        build_vector_store()  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# 2. backend dispatch
# --------------------------------------------------------------------------- #
def test_memory_backend_needs_no_sdk_and_no_network():
    store = build_vector_store(dim=8, backend="memory")
    assert isinstance(store, InMemoryVectorStore)
    # The read half is what a retriever needs; the write half minus `upsert`
    # is why the port is split (see maia.retrieval_port).
    assert isinstance(store, VectorSearchPort)


def test_backend_name_is_normalised(clean_stores, monkeypatch):
    monkeypatch.setattr(rb, "QdrantStore", _RecordingStore)
    for spelling in ("QDRANT", "  qdrant  ", "Qdrant"):
        assert isinstance(build_vector_store(dim=4, backend=spelling), QdrantAdapter)


def test_unknown_backend_lists_the_supported_ones(clean_stores):
    with pytest.raises(BackendConfigurationError) as exc:
        build_vector_store(dim=4, backend="pinecone")
    message = str(exc.value)
    assert "pinecone" in message
    for name in rb.SUPPORTED_BACKENDS:
        assert name in message


def test_backend_name_is_a_value_error():
    """A wrong configuration value, not a missing dependency."""
    assert issubclass(BackendConfigurationError, ValueError)


def test_supported_backends_matches_the_settings_default():
    assert rb.settings.VECTOR_STORE_BACKEND in rb.SUPPORTED_BACKENDS
    assert rb.BACKEND_QDRANT == "qdrant"
    assert rb.BACKEND_AZURE_AI_SEARCH == "azure_ai_search"
    assert rb.BACKEND_MEMORY == "memory"


# --------------------------------------------------------------------------- #
# 3. the Azure AI Search branch, with the SDK genuinely absent
# --------------------------------------------------------------------------- #
def test_azure_ai_search_requires_an_endpoint(clean_stores, monkeypatch):
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_ENDPOINT", "")
    with pytest.raises(BackendConfigurationError) as exc:
        build_vector_store(dim=8, backend="azure_ai_search")
    assert "AZURE_AI_SEARCH_ENDPOINT" in str(exc.value)


def test_azure_ai_search_requires_an_index_name(clean_stores, monkeypatch):
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_ENDPOINT",
                        "https://maia.search.windows.net")
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_INDEX", "  ")
    with pytest.raises(BackendConfigurationError) as exc:
        build_vector_store(dim=8, backend="azure_ai_search")
    assert "AZURE_AI_SEARCH_INDEX" in str(exc.value)


def test_azure_ai_search_reports_a_missing_sdk_as_a_domain_error(monkeypatch):
    """azure-search-documents is NOT in requirements.api.txt on purpose (the ACA
    profile is 1GiB). The failure a deployer sees must name the package."""
    monkeypatch.setattr(rb.settings, "AZURE_AI_SEARCH_ENDPOINT",
                        "https://maia.search.windows.net")
    monkeypatch.setitem(sys.modules, "azure", None)
    monkeypatch.setitem(sys.modules, "azure.search", None)
    monkeypatch.setitem(sys.modules, "azure.search.documents", None)
    with pytest.raises(AzureSearchUnavailable) as exc:
        build_vector_store(dim=8, backend="azure_ai_search")
    assert "azure-search-documents" in str(exc.value)


def test_azure_ai_search_adapter_is_reachable_with_an_injected_client(monkeypatch):
    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        dim=1024,
        credential=object(),
        client=object(),
    )
    assert adapter.index_name == "maia_knowledge"
    assert adapter.endpoint == "https://maia.search.windows.net"
    # Recorded, not enforced: the index definition owns the vector width, so
    # there is nothing to validate here without a network call.
    assert adapter.dim == 1024


# --------------------------------------------------------------------------- #
# 4. the deferred pipeline_query.py wiring, verified without applying it
# --------------------------------------------------------------------------- #
def test_deferred_pipeline_query_wiring_still_lets_the_protected_tests_patch_the_store(
    clean_stores, monkeypatch):
    """Replays the exact 3-line patch from docs/azure-integration.md against the
    real, untouched `pipeline_query` module: replace the direct QdrantStore(...)
    call with build_vector_store(..., qdrant_store_cls=QdrantStore). If this
    holds, patch("maia.pipeline_query.QdrantStore") and
    monkeypatch.setattr(pq, "QdrantStore", ...) keep intercepting."""
    from maia import pipeline_query as pq

    monkeypatch.setattr(pq, "QdrantStore", _RecordingStore)
    # The three lines, inlined rather than applied, because pipeline_query.py
    # is dirty in this working tree.
    store = build_vector_store(
        dim=384,
        backend="qdrant",
        qdrant_store_cls=pq.QdrantStore,  # <- reads the patched module symbol
    )
    assert len(_RecordingStore.instances) == 1
    assert isinstance(store, QdrantAdapter)
    assert store._store is _RecordingStore.instances[0]
