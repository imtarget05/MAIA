"""The vector-store port: conformance + the search/scroll shape asymmetry.

Three things are pinned here that are easy to regress silently:

1. `QdrantStore` and `QdrantAdapter` satisfy the port. `QdrantStore` is
   checked at CLASS level so the check needs no network — its `__init__` does
   a connectivity probe plus collection DDL.
2. `InMemoryVectorStore` satisfies `VectorSearchPort` but NOT
   `VectorWritePort`, because it has no bulk `upsert`. That is the reason the
   port is split in two, and the reason `ingestion_pipeline` still needs its
   `hasattr(store, "upsert")` branch.
3. `search()` returns 4 keys with `text` EXCLUDED from metadata, while
   `scroll_all()` returns 3 keys with `text` INCLUDED. Asserted against a
   real implementation (`InMemoryVectorStore`) so the asymmetry cannot be
   "tidied up" without this test failing.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pytest

from maia.chunking import Chunk
from maia.config import settings
from maia.retrieval_backends import (
    AzureAISearchAdapter,
    QdrantAdapter,
    _stable_point_id,
)
from maia.retrieval_port import (
    ScrolledChunk,
    SearchHit,
    VectorSearchPort,
    VectorStorePort,
    VectorWritePort,
)
from maia.test_utils import InMemoryVectorStore
from maia.vector_store import QdrantStore


@pytest.fixture(autouse=True)
def _vectorized_query_without_sdk(monkeypatch):
    """Let adapter.search() run without the optional azure SDK installed.

    The adapter imports VectorizedQuery lazily inside search(); in CI the
    package is deliberately absent (requirements keep the ACA image lean),
    which used to fail every test that actually calls search() with
    ModuleNotFoundError instead of exercising the tenant/filter logic.
    The fake accepts the same constructor kwargs the adapter passes and is
    invisible to the stub clients (they only inspect their own `search`
    kwargs). When the real SDK IS installed this fixture does nothing, so
    the tests still run against the real class there.

    RESTORED 2026-10-01. This fixture existed from 6434c1d and was removed by
    922110a ("feat(azure): retrieval port ... k8s manifest"), which took 34
    lines out of this file. The removal is a regression, not a cleanup: CI runs
    without `azure-search-documents`, so the two C1 tenant-isolation controls
    started failing on `ModuleNotFoundError` instead of testing the behaviour
    they exist to test. A negative control that cannot run is not a control.
    """
    try:
        import azure.search.documents.models  # noqa: F401
        return
    except ImportError:
        pass
    import types

    models = types.ModuleType("azure.search.documents.models")

    class VectorizedQuery:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    models.VectorizedQuery = VectorizedQuery
    for name in ("azure", "azure.search", "azure.search.documents"):
        pkg = types.ModuleType(name)
        pkg.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, pkg)
    monkeypatch.setitem(sys.modules, "azure.search.documents.models", models)


# --------------------------------------------------------------------------- #
# 1. structural conformance
# --------------------------------------------------------------------------- #
def test_qdrant_store_satisfies_the_whole_port():
    """Checked on the CLASS: instantiating QdrantStore would hit the network."""
    assert isinstance(QdrantStore, VectorSearchPort)
    assert isinstance(QdrantStore, VectorWritePort)
    assert isinstance(QdrantStore, VectorStorePort)


def test_qdrant_adapter_satisfies_the_whole_port():
    adapter = QdrantAdapter(InMemoryVectorStore())  # type: ignore[arg-type]
    assert isinstance(adapter, VectorSearchPort)
    assert isinstance(adapter, VectorWritePort)
    assert isinstance(adapter, VectorStorePort)


def test_in_memory_store_satisfies_search_but_not_write():
    """The split is load-bearing, not decoration: no `upsert` means no
    `VectorWritePort`, and no `VectorStorePort` either."""
    mem = InMemoryVectorStore()
    assert isinstance(mem, VectorSearchPort)
    assert not isinstance(mem, VectorWritePort)
    assert not isinstance(mem, VectorStorePort)
    # ...and the rest of the write half is there, which is why the
    # ingestion_pipeline fallback path is usable at all.
    assert not hasattr(mem, "upsert")
    for name in ("upsert_one", "exists", "count", "delete_by_doc"):
        assert hasattr(mem, name), name


def test_azure_ai_search_adapter_satisfies_the_port_with_a_stub_client():
    """Offline conformance: a stub SearchClient stands in for the service, and
    a plain object stands in for the managed identity, so no SDK is needed."""
    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        credential=object(),  # a fake TokenCredential: never used, never called
        client=_StubSearchClient(),
    )
    assert isinstance(adapter, VectorSearchPort)
    assert isinstance(adapter, VectorWritePort)
    assert isinstance(adapter, VectorStorePort)


# --------------------------------------------------------------------------- #
# 2. the `except TypeError:` probe contract
# --------------------------------------------------------------------------- #
def test_port_keeps_optional_kwargs_that_callers_probe_for():
    """Callers call `search(..., tenant_id=...)` inside `try/except TypeError`.
    If the port made that keyword positional-only or required, the probe would
    catch a TypeError for the wrong reason and retry the read UNFILTERED."""
    search_sig = inspect.signature(VectorSearchPort.search)
    for name in ("tenant_id", "extra_filter"):
        assert name in search_sig.parameters, name
        assert search_sig.parameters[name].default is None, name
    scroll_sig = inspect.signature(VectorSearchPort.scroll_all)
    assert "tenant_id" in scroll_sig.parameters
    assert scroll_sig.parameters["tenant_id"].default is None
    # ...and scroll_all must stay callable with the argument absent entirely,
    # which is the fallback branch every caller has.
    assert scroll_sig.parameters["limit"].default == 10000


# --------------------------------------------------------------------------- #
# 3. the search/scroll asymmetry, asserted on a real implementation
# --------------------------------------------------------------------------- #
def _seeded_memory_store() -> InMemoryVectorStore:
    mem = InMemoryVectorStore()
    mem.upsert_one(
        "chunk-1",
        np.array([1.0, 0.0, 0.0], dtype=np.float32),
        {"chunk_id": "chunk-1", "text": "Nghỉ phép 5 ngày.", "doc_id": "d1"},
    )
    return mem


def test_search_hits_carry_score_and_exclude_text_from_metadata():
    hits = _seeded_memory_store().search(np.array([1.0, 0.0, 0.0], dtype=np.float32))
    assert len(hits) == 1
    hit = hits[0]
    assert set(hit) == {"chunk_id", "text", "score", "metadata"}
    assert "score" in hit
    assert hit["text"] == "Nghỉ phép 5 ngày."
    # The text is NOT duplicated inside metadata for a search hit.
    assert "text" not in hit["metadata"]
    assert hit["metadata"]["doc_id"] == "d1"


def test_scrolled_chunks_have_no_score_and_keep_text_in_metadata():
    rows = _seeded_memory_store().scroll_all()
    assert len(rows) == 1
    row = rows[0]
    # The asymmetry: 3 keys, no score. A test that "fixes" scroll_all to match
    # search() must be expected to fail here.
    assert set(row) == {"chunk_id", "text", "metadata"}
    assert "score" not in row
    assert row["chunk_id"] == "chunk-1"
    assert row["text"] == "Nghỉ phép 5 ngày."
    # ...and the whole payload, text included, is also in metadata.
    assert row["metadata"]["text"] == "Nghỉ phép 5 ngày."


def test_typed_dicts_declare_the_two_shapes_explicitly():
    assert set(SearchHit.__required_keys__) == {"chunk_id", "text", "score", "metadata"}
    assert set(ScrolledChunk.__required_keys__) == {"chunk_id", "text", "metadata"}
    assert set(ScrolledChunk.__annotations__) == {"chunk_id", "text", "metadata"}


# --------------------------------------------------------------------------- #
# 4. the tenant filter is a real filter, not a post-hoc no-op
# --------------------------------------------------------------------------- #
def test_search_and_scroll_both_scope_to_the_tenant():
    mem = _seeded_memory_store()
    mem.upsert_one("chunk-2", np.array([0.0, 1.0, 0.0], dtype=np.float32),
                   {"chunk_id": "chunk-2", "text": "other tenant", "tenant_id": "acme"})
    q = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert [h["chunk_id"] for h in mem.search(q, tenant_id="default")] == ["chunk-1"]
    assert [r["chunk_id"] for r in mem.scroll_all(tenant_id="acme")] == ["chunk-2"]


# --------------------------------------------------------------------------- #
# 5. the adapter is a real pass-through, not a re-implementation
# --------------------------------------------------------------------------- #
def test_qdrant_adapter_forwards_search_and_scroll_unchanged():
    mem = _seeded_memory_store()
    adapter = QdrantAdapter(mem)  # type: ignore[arg-type]
    q = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert adapter.search(q, top_k=1)[0]["chunk_id"] == "chunk-1"
    assert adapter.scroll_all()[0]["chunk_id"] == "chunk-1"


def test_qdrant_adapter_forwards_the_raw_client():
    """`client` must be forwarded, not defaulted to None.

    llamaindex_store.py:62 reaches the raw client with
    `getattr(store, "client", None)`, and a MISSING attribute returns None
    there without raising — it just silently disables the LlamaIndex
    dataplane. So this is a forwarding test, not a presence test.
    """
    raw_client = object()
    adapter = QdrantAdapter(SimpleNamespace(client=raw_client, collection="c", dim=3, url="u"))  # type: ignore[arg-type]
    assert adapter.client is raw_client
    assert adapter.collection == "c"
    assert adapter.dim == 3
    assert adapter.url == "u"


# --------------------------------------------------------------------------- #
# stub
# --------------------------------------------------------------------------- #
class _StubSearchClient:
    """Stand-in for azure.search.documents.SearchClient that PINS the contract.

    Two things make this more than a `**kwargs` sponge:

    * `search` declares an explicit allowlist of keyword names and raises
      `TypeError` on anything else. A `**kwargs` stub accepts whatever the
      adapter passes, so every test in this file passed even when the adapter
      called the SDK with arguments the SDK does not have -- the failure only
      shows up against a live index, at 3am. `**kwargs` here means the stub
      cannot fail, and a stub that cannot fail pins nothing.
    * `test_the_adapter_only_uses_keywords_the_real_sdk_accepts` checks the
      allowlist against the real `SearchClient.search` signature, so SDK drift
      breaks CI rather than production. It skips when the SDK is absent.

    No SDK import and no network at runtime; the signature check is the only
    place the real SDK is touched, and it is skipped when unavailable.
    """

    #: Keyword arguments `azure.search.documents.SearchClient.search` accepts.
    SEARCH_KWARGS: ClassVar[frozenset[str]] = frozenset({
        "search_text", "include_total_count", "facets", "filter",
        "highlight_fields", "highlight_pre_tag", "highlight_post_tag",
        "minimum_coverage", "order_by", "query_type", "scoring_parameters",
        "scoring_profile", "semantic_query", "search_fields", "search_mode",
        "query_answer", "query_answer_count", "query_answer_threshold",
        "query_caption", "query_caption_highlight_enabled",
        "semantic_configuration_name", "select", "skip", "top",
        "scoring_statistics", "session_id", "vector_queries",
        "vector_filter_mode", "semantic_error_mode",
        "semantic_max_wait_in_milliseconds", "debug",
    })
    #: The only kwargs the adapter is allowed to pass. A SUBSET of the above,
    #: asserted as such by the SDK-signature test, so widening this class's
    #: allowlist alone cannot make an adapter regression invisible.
    ADAPTER_KWARGS: ClassVar[frozenset[str]] = frozenset({
        "search_text", "vector_queries", "filter", "top", "skip", "select",
        "include_total_count",
    })

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.upserts: list[list[dict]] = []
        self.deletes: list[list[dict]] = []

    def search(self, **kwargs: Any) -> list[dict]:
        unknown = set(kwargs) - self.ADAPTER_KWARGS
        if unknown:
            # A real client would raise TypeError for an unknown keyword. The
            # adapter must not be able to call the SDK wrongly and have every
            # test still pass.
            raise TypeError(
                f"_StubSearchClient.search got unexpected keyword(s): "
                f"{sorted(unknown)}"
            )
        self.calls.append(dict(kwargs))
        return [
            {
                "@search.score": 0.81,
                "id": "stable-1",
                "chunk_id": "chunk-1",
                "text": "Nghỉ phép 5 ngày.",
                "tenant_id": "default",
                "doc_id": "d1",
            }
        ]

    def merge_or_upload_documents(self, documents: list[dict]) -> None:
        self.upserts.append([dict(d) for d in documents])

    def delete_documents(self, documents: list[dict]) -> None:
        self.deletes.append([dict(d) for d in documents])


# --------------------------------------------------------------------------- #
# 6. C1 REGRESSION: the `except TypeError` probe must not widen a tenant read
# --------------------------------------------------------------------------- #
# The reviewer's probe, reproduced exactly. A caller hands a store a
# non-`str` `extra_filter` (a qdrant Filter -- what a shared query builder
# passes a store it believes to be backend-agnostic) while asking for one
# tenant. The OLD adapter raised `TypeError` for that; the probe in
# `retriever.py:111-113` caught it and retried WITHOUT `tenant_id`, and a row
# belonging to tenant "VICTIM" came back to a caller that had asked for
# "acme". These tests fail against the pre-fix code.
def _odata_matches(odata: object, row: dict) -> bool:
    """Honours `field eq 'value'` clauses joined by `and`.

    A fixture that IGNORES its filter would make the C1 test pass for the wrong
    reason, and a fixture that substring-matches would not filter at all -- this
    is the smallest matcher that actually enforces the tenant clause.
    """
    if odata is None:
        return True
    for clause in str(odata).split(" and "):
        clause = clause.strip().strip("()").strip()
        match = _CLAUSE_RE.fullmatch(clause)
        assert match, f"fixture cannot parse the adapter's OData: {odata!r}"
        field, value = match.group(1), match.group(2).replace("''", "'")
        if str(row.get(field)) != value:
            return False
    return True


_CLAUSE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*) eq '([^']*(?:''[^']*)*)'")


class _LeakyClient:
    """An in-memory index that HONOURS the OData filter it is given.

    Two tenants holding the SAME doc_id (filename-derived ids collide across
    tenants -- that is the premise of the H1 fix). A filter is applied, so a
    correctly scoped read cannot return "VICTIM" by accident.
    """

    ROWS: ClassVar[list[dict]] = [
        {"@search.score": 0.9, "id": "id-acme", "chunk_id": "c-acme",
         "text": "acme internal", "tenant_id": "acme", "doc_id": "handbook.pdf"},
        {"@search.score": 0.8, "id": "id-victim", "chunk_id": "c-victim",
         "text": "victim secret", "tenant_id": "VICTIM",
         "doc_id": "handbook.pdf"},
    ]

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.deletes: list[list[dict]] = []

    def search(self, **kwargs: Any) -> list[dict]:
        unknown = set(kwargs) - _StubSearchClient.ADAPTER_KWARGS
        if unknown:
            raise TypeError(f"unexpected kwargs {sorted(unknown)}")
        self.calls.append(dict(kwargs))
        odata = kwargs.get("filter")
        rows = [r for r in self.ROWS if _odata_matches(odata, r)]
        return [dict(r) for r in rows]

    def delete_documents(self, documents: list[dict]) -> None:
        self.deletes.append([dict(d) for d in documents])


class _TwoTenantStore:
    """The seam a caller actually holds: the Azure adapter, wrapped so it
    exposes exactly the keyword arguments a probe passes to a store."""

    def __init__(self, client: Any) -> None:
        self._adapter = AzureAISearchAdapter(
            endpoint="https://maia.search.windows.net",
            index_name="maia_knowledge",
            credential=object(),
            client=client,
        )

    def search(self, query_vec, top_k: int = 10, tenant_id=None, **kw):
        return self._adapter.search(
            query_vec, top_k=top_k, tenant_id=tenant_id, **kw
        )

    def scroll_all(self, limit: int = 10000, tenant_id: str | None = None):
        return self._adapter.scroll_all(limit=limit, tenant_id=tenant_id)


def _search_the_way_the_retriever_probes(store, qvec, top_k, tenant_id,
                                        extra_filter=None) -> list[dict]:
    """The EXACT try/except TypeError shape from `retriever.py:111-113`.

    Verbatim on purpose. Re-implementing it "more correctly" here would test a
    probe the product does not have, and the whole bug lived in the probe.
    """
    try:
        return store.search(qvec, top_k=top_k, tenant_id=tenant_id,
                            extra_filter=extra_filter)
    except TypeError:
        return store.search(qvec, top_k=top_k)


def test_the_probe_shape_does_widen_when_the_store_raises_type_error():
    """CONTROL. Proves the regression test below can actually see a leak.

    If this ever stops leaking, the C1 test is not proving anything -- it could
    be passing because the fixture is broken rather than because the fix works.
    """
    class _TypeErroringStore:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def search(self, query_vec, top_k: int = 10, tenant_id=None,
                   extra_filter=None) -> list[dict]:
            self.calls.append(tenant_id)
            if tenant_id is not None and extra_filter is not None:
                raise TypeError("this store does not support extra_filter")
            return [{"chunk_id": "c", "text": "t", "score": 1.0,
                     "metadata": {"tenant_id": "VICTIM"}}]

    store = _TypeErroringStore()
    out = _search_the_way_the_retriever_probes(
        store, [1.0, 0.0, 0.0], 10, "acme", extra_filter={"must": []}
    )
    # The retry dropped the tenant, and another tenant's row came back.
    assert store.calls == ["acme", None], store.calls
    assert out[0]["metadata"]["tenant_id"] == "VICTIM"


def test_c1_a_non_str_extra_filter_never_becomes_a_type_error():
    """The mechanism, isolated: `_build_filter` rejects with a ValueError.

    `BackendConfigurationError` subclasses `ValueError` and is NOT a
    `TypeError`. If this assertion fails, the probe above catches the
    rejection and retries with no tenant filter -- a cross-tenant read. That
    single subclass choice IS the fix.
    """
    from maia.retrieval_backends import BackendConfigurationError

    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        credential=object(),
        client=_StubSearchClient(),
    )
    with pytest.raises(BackendConfigurationError) as exc:
        adapter.search([1.0, 0.0, 0.0], tenant_id="acme",
                       extra_filter={"must": []})  # a qdrant-shaped filter
    assert not isinstance(exc.value, TypeError), (
        "BackendConfigurationError must not be catchable by `except TypeError`"
    )
    assert issubclass(BackendConfigurationError, ValueError)
    assert not issubclass(BackendConfigurationError, TypeError)


def test_c1_a_non_str_extra_filter_never_widens_the_read_to_another_tenant():
    """C1 REGRESSION. Fails against the pre-fix code; passes now."""
    client = _LeakyClient()
    store = _TwoTenantStore(client)

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        _search_the_way_the_retriever_probes(
            store, [1.0, 0.0, 0.0], 10, "acme", extra_filter={"must": []}
        )

    # The read failed loudly, which is the only acceptable outcome short of a
    # correctly scoped result. What must NOT have happened is an unfiltered
    # retry. Here the rejection happens in `_build_filter`, BEFORE any service
    # call, so the strongest true statement is that nothing was sent at all --
    # and in particular nothing was sent unfiltered.
    filters = [c.get("filter") for c in client.calls]
    assert filters == [], (
        f"a rejected filter must not reach the service, let alone unfiltered: "
        f"{filters!r}"
    )

    # ...and the same read done properly returns only acme's row, which shows
    # the fixture could not have leaked VICTIM by accident.
    hits = store.search([1.0, 0.0, 0.0], top_k=10, tenant_id="acme")
    assert {h["metadata"]["tenant_id"] for h in hits} == {"acme"}
    assert "victim secret" not in {h["text"] for h in hits}


def test_c1_the_scroll_probe_shape_never_widens_either(tmp_path):
    """`retriever.rebuild` has the identical try/except-TypeError shape.

    A corpus rebuild that falls back to an unfiltered scroll puts EVERY
    tenant's chunks into one BM25 index, so the same rejection must not be a
    `TypeError` on this path either. This drives the real `HybridRetriever`.
    """
    from maia.retriever import HybridRetriever

    class _Embedder:
        dim = 3

        def embed_query(self, q: str):
            return [1.0, 0.0, 0.0]

    client = _LeakyClient()
    store = _TwoTenantStore(client)
    retriever = HybridRetriever(store=store, embedder=_Embedder(),
                                storage_dir=str(tmp_path), tenant_id="acme")
    retriever.rebuild(tenant_id="acme")

    filters = [c.get("filter") for c in client.calls]
    assert filters, "no scroll reached the service"
    assert all("acme" in str(f) for f in filters), filters
    corpus_text = " ".join(c["text"] for c in retriever._corpus)
    assert "victim secret" not in corpus_text, corpus_text


def test_a_tenant_scoped_retrieve_does_not_reach_for_an_unfiltered_read(tmp_path):
    """The ordinary path: a scoped read stays scoped, no probe fires."""
    from maia.retriever import HybridRetriever

    class _Embedder:
        dim = 3

        def embed_query(self, q: str):
            return [1.0, 0.0, 0.0]

    client = _LeakyClient()
    retriever = HybridRetriever(store=_TwoTenantStore(client),
                                embedder=_Embedder(),
                                storage_dir=str(tmp_path), tenant_id="acme")
    got = retriever.retrieve("internal", tenant_id="acme")
    assert {c["metadata"]["tenant_id"] for c in got} == {"acme"}
    assert all(c.get("filter") is not None for c in client.calls), client.calls
    assert "victim secret" not in {c["text"] for c in got}


# --------------------------------------------------------------------------- #
# 7. C2: internal TypeError/ValueError must not escape as a TypeError
# --------------------------------------------------------------------------- #
class _DriftingSearchClient(_StubSearchClient):
    """Stands in for an SDK whose `search` signature has drifted.

    `TypeError` is exactly what an argument-mismatch raises, and exactly what
    the `qdrant-client <1.10 legacy path` branch at vector_store.py:186 exists
    to absorb. If it escapes `search`, the probe reads it as "this store does
    not support tenant_id" and retries unfiltered.
    """

    def search(self, **kwargs: Any) -> list[dict]:
        raise TypeError(
            "SearchClient.search() got an unexpected keyword argument 'filter'"
        )


class _ValueErroringSearchClient(_StubSearchClient):
    def search(self, **kwargs: Any) -> list[dict]:
        raise ValueError("'top' must be a positive integer, got -1")


@pytest.mark.parametrize(
    "client_cls", [_DriftingSearchClient, _ValueErroringSearchClient]
)
def test_c2_sdk_type_errors_become_domain_errors_not_type_errors(client_cls):
    from maia.retrieval_backends import AzureSearchUnavailable

    client = client_cls()
    store = _TwoTenantStore(client)

    with pytest.raises(AzureSearchUnavailable) as exc:
        _search_the_way_the_retriever_probes(
            store, [1.0, 0.0, 0.0], 10, "acme"
        )
    # The decisive assertion: the probe's handler is `except TypeError`, so a
    # TypeError here means an unfiltered retry.
    assert not isinstance(exc.value, TypeError)
    assert isinstance(exc.value, RuntimeError)
    assert client.calls == [], "an SDK error must not have produced a retry"


def test_c2_a_malformed_hit_becomes_a_domain_error():
    """A non-mapping item from the service is an adapter-internal failure."""

    class _JunkClient(_StubSearchClient):
        def search(self, **kwargs: Any) -> list[dict]:
            # A score the adapter cannot parse: `float()` raises ValueError
            # inside `search`, which without the guard would surface as
            # ValueError -- and ValueError from `search` is what the retriever
            # would... not catch, but a TypeError sibling would be. The guard
            # exists so a malformed response is a domain error either way.
            return [{"id": "x", "chunk_id": "c", "text": "t",
                     "@search.score": "not-a-number"}]

    store = _TwoTenantStore(_JunkClient())
    with pytest.raises(RuntimeError) as exc:
        _search_the_way_the_retriever_probes(store, [1.0, 0.0, 0.0], 10, "acme")
    assert not isinstance(exc.value, TypeError)


def test_c2_scroll_all_also_folds_sdk_errors():
    from maia.retrieval_backends import AzureSearchUnavailable

    store = _TwoTenantStore(_DriftingSearchClient())
    with pytest.raises(AzureSearchUnavailable) as exc:
        store.scroll_all(limit=10, tenant_id="acme")
    assert not isinstance(exc.value, TypeError)


def test_writes_fold_sdk_type_errors_into_domain_errors_too():
    from maia.retrieval_backends import AzureSearchUnavailable

    class _BadWriteClient(_StubSearchClient):
        def merge_or_upload_documents(self, documents: list[dict]) -> None:
            raise TypeError("unexpected keyword argument 'documents'")

    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        credential=object(),
        client=_BadWriteClient(),
    )
    with pytest.raises(AzureSearchUnavailable) as exc:
        adapter.upsert_one("chunk-1", [1.0], {"chunk_id": "chunk-1"})
    assert not isinstance(exc.value, TypeError)


# --------------------------------------------------------------------------- #
# 8. H1: delete_by_doc takes a tenant, or REFUSES
# --------------------------------------------------------------------------- #
def test_the_port_declares_tenant_id_on_delete_by_doc():
    sig = inspect.signature(VectorWritePort.delete_by_doc)
    assert "tenant_id" in sig.parameters, sig
    assert sig.parameters["tenant_id"].default is None, sig


def test_azure_delete_by_doc_scopes_the_key_lookup_to_the_tenant():
    client = _LeakyClient()
    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        credential=object(),
        client=client,
    )
    adapter.delete_by_doc("handbook.pdf", tenant_id="acme")
    filters = [c.get("filter") for c in client.calls]
    assert filters, "nothing was searched"
    for f in filters:
        assert "doc_id eq 'handbook.pdf'" in str(f), f
        assert "tenant_id eq 'acme'" in str(f), f
    # Only acme's key is submitted; the colliding VICTIM row is never selected.
    assert client.deletes == [[{"id": "id-acme"}]], client.deletes


def test_qdrant_adapter_refuses_a_tenant_scoped_delete_it_cannot_honour():
    """Better a loud refusal than a cross-tenant delete.

    `QdrantStore.delete_by_doc` has no `tenant_id`, so accepting the parameter
    and ignoring it would leave the port's tenant claim false -- exactly the
    confident-but-wrong documentation this change is removing.
    """
    from maia.retrieval_backends import BackendConfigurationError

    calls: list[str] = []
    store = SimpleNamespace(delete_by_doc=lambda doc_id: calls.append(doc_id))
    adapter = QdrantAdapter(store)  # type: ignore[arg-type]
    with pytest.raises(BackendConfigurationError) as exc:
        adapter.delete_by_doc("handbook.pdf", tenant_id="acme")
    assert "handbook.pdf" in str(exc.value)
    assert calls == [], "nothing may be deleted when the scope cannot be honoured"
    # ...and with no tenant it behaves exactly as before: back-compat.
    adapter.delete_by_doc("handbook.pdf")
    assert calls == ["handbook.pdf"]


def test_qdrant_adapter_forwards_tenant_id_when_the_store_accepts_it():
    seen: list[tuple] = []
    store = SimpleNamespace(
        delete_by_doc=lambda doc_id, tenant_id=None: seen.append((doc_id, tenant_id))
    )
    QdrantAdapter(store).delete_by_doc("d1", tenant_id="acme")  # type: ignore[arg-type]
    assert seen == [("d1", "acme")]


# --------------------------------------------------------------------------- #
# 9. H3: count() must not report "broken" as "empty"
# --------------------------------------------------------------------------- #
class _Paged(list):
    """A `SearchItemPaged` stand-in: a list that also answers `get_count()`."""

    def __init__(self, rows: list[dict] | None = None, total: int = 0) -> None:
        super().__init__(rows or [])
        self._total = total

    def get_count(self) -> int:
        return self._total


def test_count_returns_the_unknown_sentinel_when_the_service_errors():
    from maia.retrieval_backends import COUNT_UNKNOWN

    assert COUNT_UNKNOWN == -1, "api.py already uses -1 for this (api.py:806)"

    class _ForbiddenClient(_StubSearchClient):
        def search(self, **kwargs: Any) -> list[dict]:
            raise PermissionError("403 Forbidden")

    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        credential=object(),
        client=_ForbiddenClient(),
    )
    # 0 here would make a broken backend report as a healthy EMPTY one, which
    # is what api.py:1015 then publishes as {"status": "ok", "qdrant_points": 0}.
    assert adapter.count() == COUNT_UNKNOWN
    assert adapter.count() != 0


def test_count_still_returns_zero_for_a_genuinely_empty_index():
    class _EmptyClient(_StubSearchClient):
        def search(self, **kwargs: Any) -> list[dict]:
            return _Paged(total=0)

    adapter = AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        credential=object(),
        client=_EmptyClient(),
    )
    assert adapter.count() == 0


def test_the_port_documents_the_count_sentinel():
    assert "COUNT_UNKNOWN" in (VectorWritePort.count.__doc__ or "")
    assert "-1" in (VectorWritePort.count.__doc__ or "")


# --------------------------------------------------------------------------- #
# 10. H4: the adapter's kwargs must be a subset of the REAL SDK signature
# --------------------------------------------------------------------------- #
def _search_sdk_available() -> bool:
    try:
        from azure.search.documents import SearchClient  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(
    not _search_sdk_available(),
    reason="azure-search-documents is not installed (the ACA profile omits it)",
)
def test_the_adapter_only_uses_keywords_the_real_sdk_accepts():
    """Pins the stub allowlist to the real SDK, so drift breaks CI.

    Without this, `_StubSearchClient.ADAPTER_KWARGS` is just another hand-typed
    list that drifts from the SDK in exactly the way it was written to catch.
    """
    from azure.search.documents import SearchClient

    params = inspect.signature(SearchClient.search).parameters
    # `self` and the SDK's trailing `**kwargs` are not keywords the adapter
    # can meaningfully use, and the SDK's `**kwargs` would otherwise make this
    # check vacuous (it would accept literally any name).
    real = {
        name for name, p in params.items()
        if name != "self"
        and p.kind not in (inspect.Parameter.VAR_KEYWORD,
                           inspect.Parameter.VAR_POSITIONAL)
    }
    unknown = _StubSearchClient.ADAPTER_KWARGS - real
    assert not unknown, (
        f"the adapter passes kwargs the real SearchClient.search does not "
        f"accept: {sorted(unknown)}"
    )
    # The allowlist is a subset, not a copy: the SDK may grow keywords and the
    # adapter is not obliged to use them, so a growing SDK is not a failure --
    # but a SHRINKING one is, and that is what the line above catches.
    stale = real - _StubSearchClient.SEARCH_KWARGS
    assert not stale, (
        f"SEARCH_KWARGS is stale: the SDK now also accepts {sorted(stale)}"
    )


def test_the_stub_rejects_an_unknown_keyword():
    """The stub is only useful if it can fail."""
    with pytest.raises(TypeError):
        _StubSearchClient().search(totally_not_a_real_kwarg=1)


# --------------------------------------------------------------------------- #
# 11. M5: a bare str is not a credential
# --------------------------------------------------------------------------- #
def test_a_string_credential_is_rejected_in_favour_of_api_key():
    from maia.retrieval_backends import BackendConfigurationError

    with pytest.raises(BackendConfigurationError) as exc:
        AzureAISearchAdapter(
            endpoint="https://maia.search.windows.net",
            index_name="maia_knowledge",
            credential="an-api-key-mistaken-for-a-principal",
            client=_StubSearchClient(),
        )
    assert "api_key" in str(exc.value)


def test_a_string_client_is_rejected():
    from maia.retrieval_backends import BackendConfigurationError

    with pytest.raises(BackendConfigurationError):
        AzureAISearchAdapter(
            endpoint="https://maia.search.windows.net",
            index_name="maia_knowledge",
            credential=object(),
            client="https://maia.search.windows.net",
        )


# --------------------------------------------------------------------------- #
# 12. M1-M4: write-path parity, asserted on the documents actually built
# --------------------------------------------------------------------------- #
def _adapter(client: Any) -> AzureAISearchAdapter:
    return AzureAISearchAdapter(
        endpoint="https://maia.search.windows.net",
        index_name="maia_knowledge",
        credential=object(),
        client=client,
    )


def test_m1_a_vectors_chunks_length_mismatch_is_loud():
    client = _StubSearchClient()
    chunks = [Chunk(text="a", metadata={"chunk_id": "c1"})]
    with pytest.raises(ValueError):
        # Two vectors, one chunk. Silent truncation would write half the corpus
        # and report the full count.
        _adapter(client).upsert([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], chunks)
    assert client.upserts == [], "nothing may be written when the input is bad"


def test_m2_document_id_is_read_as_a_doc_id_alias():
    """`doc.setdefault("doc_id", str(doc.get("doc_id", ...)))` read and wrote
    the SAME key, so its fallback was permanently "". A payload carrying only
    the `document_id` alias -- which the knowledge loop emits and which
    maia.test_utils:31 tolerates -- lost its doc id, and doc_id is the filter
    `delete_by_doc` searches on."""
    client = _StubSearchClient()
    adapter = _adapter(client)
    adapter.upsert_one(
        "c1", [1.0, 0.0, 0.0],
        {"chunk_id": "c1", "text": "hello", "document_id": "handbook.pdf"},
    )
    assert client.upserts[0][0]["doc_id"] == "handbook.pdf"
    # ...and an explicit doc_id still wins over the alias.
    adapter.upsert_one(
        "c2", [1.0, 0.0, 0.0],
        {"chunk_id": "c2", "text": "hi", "doc_id": "real.pdf",
         "document_id": "alias.pdf"},
    )
    assert client.upserts[1][0]["doc_id"] == "real.pdf"


def test_m3_a_stale_text_in_the_payload_does_not_override_the_chunk():
    """Qdrant sets `payload["text"] = ch.text` unconditionally
    (vector_store.py:109); the adapter used to keep the stale value, so the two
    backends indexed different text for the same chunk."""
    client = _StubSearchClient()
    _adapter(client).upsert(
        [[1.0, 0.0, 0.0]],
        [Chunk(text="the new text", metadata={"chunk_id": "c1",
                                              "text": "the OLD text"})],
    )
    assert client.upserts[0][0]["text"] == "the new text"


def test_m4_point_id_matches_qdrant_and_chunk_hash_is_written():
    """The parity claim, ASSERTED instead of asserted in prose.

    `_stable_point_id` is `uuid5(NAMESPACE_URL, key)` -- the same scheme and
    the same key as `QdrantStore.upsert` (vector_store.py:114) for any chunk
    with a non-empty `chunk_id`. `chunk_hash` is written because Qdrant writes
    it on both upsert paths and it is the fallback id key.
    """
    import uuid as _uuid

    from maia.vector_store import _chunk_hash

    client = _StubSearchClient()
    _adapter(client).upsert([[1.0, 0.0, 0.0]],
                            [Chunk(text="hello", metadata={"chunk_id": "c1"})])
    doc = client.upserts[0][0]

    assert doc["id"] == str(_uuid.uuid5(_uuid.NAMESPACE_URL, "c1"))
    assert doc["id"] == _stable_point_id("c1")
    assert doc["chunk_hash"] == _chunk_hash("hello")
    assert doc["text"] == "hello"
    # And the tenant default matches Qdrant's: stored tenant-less rows would be
    # invisible to every filtered read.
    assert doc["tenant_id"] == settings.TENANT_ID
