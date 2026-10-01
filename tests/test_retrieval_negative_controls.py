"""Negative controls for the RetrievalPort boundary (TODO 4, NC2/NC4/NC5 + P10).

NC1 (unknown backend) and NC3 (azure missing endpoint/index/SDK) already live
in tests/test_retrieval_factory.py; C1 tenant-widening lives in
tests/test_retrieval_port.py. This file pins what those leave open:

- NC2: a dependency failure inside the Qdrant path propagates UNCHANGED
  (never translated, never retried against another backend).
- NC4: a malformed vendor document fails controlled (domain error), never a
  raw KeyError/TypeError from mapping code.
- NC5: an adapter missing a port method fails the isinstance gate.
- P10: the factory constructs exactly one store per build (no duplication).
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.retrieval_backends import (
    AzureAISearchAdapter,
    AzureSearchUnavailable,
    QdrantAdapter,
    build_vector_store,
)
from maia.retrieval_port import VectorSearchPort, VectorStorePort


class _FailingStore:
    """Store whose dependency is down: every operation raises ConnectionError."""

    def __init__(self, *a, **k):
        pass

    def search(self, *a, **k):
        raise ConnectionError("qdrant unreachable")

    def scroll_all(self, *a, **k):
        raise ConnectionError("qdrant unreachable")


def test_nc2_qdrant_dependency_failure_propagates_unchanged():
    """No translation, no fallback: the caller sees the real failure."""
    adapter = QdrantAdapter(_FailingStore())
    with pytest.raises(ConnectionError):
        adapter.search([0.1, 0.2], top_k=3, tenant_id="acme")
    with pytest.raises(ConnectionError):
        adapter.scroll_all(tenant_id="acme")


def test_nc2_qdrant_failure_is_never_a_type_error():
    """The tenant-widening retry in retriever.py keys on TypeError. A
    dependency failure must not present as one."""
    adapter = QdrantAdapter(_FailingStore())
    try:
        adapter.search([0.1], tenant_id="acme")
    except TypeError:
        pytest.fail("dependency failure must not surface as TypeError")
    except ConnectionError:
        pass


class _MalformedDocClient:
    """Stub SearchClient returning rows no mapping code should trust."""

    def search(self, **kwargs):
        return [None, "just-a-string", {"@search.score": 0.9}]


def test_nc4_malformed_vendor_documents_fail_controlled():
    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        dim=8,
        credential=object(),
        client=_MalformedDocClient(),
    )
    with pytest.raises(AzureSearchUnavailable):
        adapter.search([0.1] * 8, top_k=3, tenant_id="acme")


class _MissingScroll:
    """Fake adapter implementing search but forgetting scroll_all."""

    def search(self, query_vec, top_k=10, score_threshold=None,
               tenant_id=None, extra_filter=None):
        return []


def test_nc5_adapter_missing_a_port_method_fails_the_gate():
    assert not isinstance(_MissingScroll(), VectorSearchPort)
    assert not isinstance(_MissingScroll(), VectorStorePort)


class _CountingStore:
    instances: ClassVar[list] = []

    def __init__(self, *a, **k):
        _CountingStore.instances.append(self)


def test_p10_factory_builds_exactly_one_store():
    _CountingStore.instances.clear()
    store = build_vector_store(dim=8, backend="qdrant",
                               qdrant_store_cls=_CountingStore)
    assert isinstance(store, QdrantAdapter)
    assert len(_CountingStore.instances) == 1
    assert store._store is _CountingStore.instances[0]


def test_p10_qdrant_adapter_adds_no_client_of_its_own():
    """The adapter wraps; it must not open connections (client comes from
    the wrapped store or is absent)."""
    adapter = QdrantAdapter(SimpleNamespace(client=None, collection="c",
                                            dim=3, url="u"))
    assert adapter.client is None


class _TenantRecordingStore:
    """Records the tenant_id every read call receives."""

    def __init__(self):
        self.search_tenants: list = []
        self.scroll_tenants: list = []

    def search(self, query_vec, top_k=10, score_threshold=None,
               tenant_id=None, extra_filter=None):
        self.search_tenants.append(tenant_id)
        return []

    def scroll_all(self, limit=10000, tenant_id=None):
        self.scroll_tenants.append(tenant_id)
        return []


def test_nc6_qdrant_adapter_forwards_tenant_id_to_the_wrapped_store():
    """NC6: dropping tenant_id here widens reads cross-tenant. A scratch
    mutation (tenant_id=None) must turn this test red — verified during
    TODO 4 against a disposable copy."""
    inner = _TenantRecordingStore()
    adapter = QdrantAdapter(inner)
    adapter.search([0.1, 0.2], top_k=2, tenant_id="acme")
    adapter.scroll_all(limit=5, tenant_id="acme")
    assert inner.search_tenants == ["acme"]
    assert inner.scroll_tenants == ["acme"]
