"""Vector-store port: the contract MAIA actually consumes, backend-agnostic.

Why this file exists
--------------------
Until now the vector store was reached by duck typing. ``HybridRetriever``
accepted an unannotated ``store`` and called ``scroll_all``/``search`` inside
``try/except TypeError`` probes, because a store *might* not accept
``tenant_id``. The probes work, but nothing checked the return shape and no
second backend could be substituted without a rewrite of the retriever.

Two protocols, not one
----------------------
``VectorSearchPort`` and ``VectorWritePort`` are deliberately separate.
``ingestion_pipeline._ingest_rawdocs`` branches on ``hasattr(store, "upsert")``
because ``InMemoryVectorStore`` has no bulk ``upsert`` (it only has
``upsert_one``). If the port declared ``upsert`` as required, that store would
stop type-checking at the branch and the branch itself would have to be
rewritten -- so the split keeps the capability probe honest instead of papering
over it. ``VectorStorePort`` is the union, for callers that genuinely need both
(ingestion, the document-lifecycle loops).

``search`` and ``scroll_all`` return DIFFERENT shapes, and this is not fixed
here
---------------------------------------------------------------------------
``QdrantStore.search`` returns 4-key dicts whose ``metadata`` is the payload
*minus* ``text``; ``QdrantStore.scroll_all`` returns 3-key dicts with NO
``score`` and whose ``metadata`` is the payload *including* ``text``. Callers
depend on both quirks: ``retriever.rebuild`` reads ``c["text"]`` straight off
the scroll result and reads ``c["metadata"]`` for tenant checks, while
``llamaindex_store.query`` expects the search shape. Unifying them would change
consumer behaviour and invalidate eval baselines, so the asymmetry is modelled
honestly in two ``TypedDict``s and pinned by a test
(``tests/test_retrieval_port.py``) instead. See ADR-0005.

``TypeError`` means ONE thing at this boundary: "this store does not accept
these ARGUMENTS"
---------------------------------------------------------------------------------
``retriever.retrieve`` and ``retriever.rebuild`` call a store inside
``try/except TypeError`` and retry the call WITHOUT ``tenant_id``. That retry
is a full-corpus read, so a ``TypeError`` raised for any other reason silently
converts a tenant-scoped read into a cross-tenant one. This was not
hypothetical: ``AzureAISearchAdapter._build_filter`` used to raise
``TypeError`` for a non-``str`` ``extra_filter`` with a comment claiming that
degraded safely, and the reviewer reproduced another tenant's row coming back
to a caller that had asked for ``tenant_id="acme"``.

So the contract is asymmetric on purpose, and both halves are pinned in
``tests/test_retrieval_port.py``:

* Signatures keep ``tenant_id``/``extra_filter`` as optional keywords with
  defaults, and ``scroll_all`` stays callable with and without ``tenant_id``.
  Making one positional-only or required would hand the caller a ``TypeError``
  for the wrong reason.
* An implementation raises ``TypeError`` ONLY from argument binding -- never
  from building a filter, calling a dependency, or parsing a response. A store
  that cannot honour a filter rejects the FILTER (a domain error such as
  ``BackendConfigurationError``, which subclasses ``ValueError`` and therefore
  cannot be caught by ``except TypeError``); it does not claim to lack a
  parameter. A store that genuinely cannot support ``tenant_id`` at all keeps
  the parameter and ignores it, which is a known and accepted gap -- it widens
  nothing, because ``tenant_id=None`` was already the caller's choice.
* Internal ``TypeError``/``ValueError`` from a dependency must be translated to
  a domain error by the adapter, never surfaced raw. ``QdrantStore`` re-filters
  client-side on its legacy path (vector_store.py:203); an adapter without that
  fallback has no safe degraded read, so it must fail loudly instead.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, TypedDict, runtime_checkable

from .chunking import Chunk

# Backend-specific payload filter, passed straight through to the backend.
#
# Typed as Any on purpose, with a comment, because there is no honest concrete
# type: it is a ``qdrant_client.models.Filter`` for the Qdrant adapter, an
# OData ``str`` fragment for Azure AI Search, and ignored entirely by the
# in-memory store. Naming ``Filter`` would couple this port to qdrant_client --
# the exact dependency the port exists to abstract -- and would reject values
# that are correct for the other two backends.
PayloadFilter = Any


class SearchHit(TypedDict):
    """One dense hit. ``metadata`` is the payload WITHOUT ``text``.

    ``text`` is a separate key because BM25/RRF fusion and the citation
    builder read it directly, and because duplicating it inside ``metadata``
    would double the size of every fused candidate dict.
    """

    chunk_id: str
    text: str
    score: float
    metadata: dict


class ScrolledChunk(TypedDict):
    """One scrolled corpus row. No ``score``: a scroll is not a ranking.

    ``metadata`` is the payload INCLUDING ``text`` (Qdrant keeps the whole
    payload), so corpus rebuilds can read either ``row["text"]`` or
    ``row["metadata"]["text"]`` and get the same string. See the module
    docstring for why this is not unified with :class:`SearchHit`.
    """

    chunk_id: str
    text: str
    metadata: dict


@runtime_checkable
class VectorSearchPort(Protocol):
    """Read half of the store: dense search + corpus scroll.

    Runtime-checkable so tests can assert a candidate store satisfies the port
    with ``isinstance(store, VectorSearchPort)``. That check verifies method
    PRESENCE only, never signatures -- a static assignment to the Protocol is
    what proves the signature, and ``src/`` is type-checked.
    """

    def search(
        self,
        query_vec: Sequence[float],
        top_k: int = 10,
        score_threshold: float | None = None,
        tenant_id: str | None = None,
        extra_filter: PayloadFilter = None,
    ) -> list[SearchHit]:
        """Top-``top_k`` dense hits for ``query_vec``, tenant-scoped.

        ``tenant_id`` and ``extra_filter`` stay optional keywords: callers
        probe for them with ``except TypeError`` (see module docstring).
        """
        ...

    def scroll_all(
        self,
        limit: int = 10000,
        tenant_id: str | None = None,
    ) -> list[ScrolledChunk]:
        """Corpus rows for BM25 rebuild + eval. Callable without ``tenant_id``."""
        ...


@runtime_checkable
class VectorWritePort(Protocol):
    """Write half of the store: ingest, idempotency probe, re-index, delete.

    Kept separate from :class:`VectorSearchPort` because
    ``InMemoryVectorStore`` implements this half minus ``upsert``: bulk ingest
    against the offline store goes through ``upsert_one`` in a loop. Declaring
    ``upsert`` required on a single combined protocol would make
    ``hasattr(store, "upsert")`` in ``ingestion_pipeline`` un-typeable.
    """

    def upsert(self, vectors: Sequence[Sequence[float]],
               chunks: Sequence[Chunk]) -> int:
        """Bulk upsert. Returns the number of points written (0 when empty)."""
        ...

    def upsert_one(self, chunk_id: str, vector: Sequence[float],
                   payload: dict) -> str:
        """Idempotent single-chunk upsert. Returns the stable point id."""
        ...

    def exists(self, chunk_id: str) -> bool:
        """True if ``chunk_id`` is already stored (skip re-embedding)."""
        ...

    def delete_by_doc(self, doc_id: str, tenant_id: str | None = None) -> None:
        """Delete every chunk belonging to ``doc_id`` (document re-index).

        ``tenant_id`` is a security parameter, not a convenience. Doc ids are
        filename-derived, so two tenants can legitimately share one, and an
        implementation that deletes by ``doc_id`` alone removes the other
        tenant's chunks too. It is optional only for back-compat with callers
        that own no tenant.

        The caller contract, stated because it cannot be enforced here:

        * A caller that KNOWS its tenant MUST pass it -- e.g.
          ``store.delete_by_doc(doc_id, tenant_id=current_user.tenant_id)``,
          never the bare positional form.
        * An implementation that accepts ``tenant_id`` but cannot honour it
          MUST raise (``BackendConfigurationError``), not ignore it.
          ``QdrantAdapter`` does this: ``QdrantStore.delete_by_doc`` has no
          tenant filter to forward to, so a tenant-scoped request is refused
          rather than silently widened. ``AzureAISearchAdapter`` scopes its
          key lookup instead.
        * Only a caller with no tenant at all (a whole-corpus maintenance job)
          may omit it, and only knowing it deletes across tenants.
        """
        ...

    def count(self) -> int:
        """Point count for the whole collection/tenant.

        SENTINEL: a negative value (conventionally ``-1``, exported as
        ``COUNT_UNKNOWN`` from ``maia.retrieval_backends``) means "the backend
        could not answer", which is NOT the same as ``0``. ``0`` is a real
        answer -- an empty index -- so a 403 or a network failure must not
        report as one. Callers must render a negative value as unknown;
        ``api.py`` already uses ``-1`` for the same field in ``/admin/stats``.

        Implementations that have no way to distinguish the two may return
        ``0`` (``QdrantStore.count`` does, and is out of scope here); new
        implementations should return the sentinel instead.
        """
        ...


@runtime_checkable
class VectorStorePort(VectorSearchPort, VectorWritePort, Protocol):
    """The full store. Ingestion, lifecycle loops and the API admin routes.

    ``HybridRetriever`` needs only :class:`VectorSearchPort` -- it must never
    grow a write dependency, or the read path starts requiring a write-capable
    store. Its ``store`` parameter is still unannotated while
    ``pipeline_query.py`` passes a bare ``QdrantStore`` (whose ``search`` is
    ``-> list[dict]`` rather than ``-> list[SearchHit]``); annotating it lands
    with that wiring, deliberately not via a type-checker exemption. See
    ``docs/azure-integration.md`` section 5.
    """
