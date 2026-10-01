"""Vector-store adapters + the factory that picks one.

This module is the ONLY place that knows which backend MAIA is talking to.
Every consumer depends on `maia.retrieval_port` instead, so swapping Qdrant
for Azure AI Search is a settings change plus a corpus migration, not a
rewrite of the retriever (ADR-0005).

Two adapters, and the second one is not finished
------------------------------------------------
`QdrantAdapter` is a pass-through: `QdrantStore` already implements the port's
signatures, so the adapter exists to (a) statically prove the port at ONE place
in `src/` and (b) forward `.client`, which `llamaindex_store.py` reaches with
`getattr(store, "client")` — returning `None` there silently disables the
LlamaIndex dataplane instead of failing.

It is a pass-through with exactly ONE exception: `delete_by_doc` now takes a
`tenant_id` (see its docstring) and REFUSES a tenant-scoped request when the
wrapped store cannot honour it. Every other method forwards verbatim.

`AzureAISearchAdapter` is a real adapter. It has never run against a live
index: there is no Azure subscription in CI, so the tests inject a stub
search client and a fake credential. It is therefore the honest weak point in
this change, and ADR-0005 makes score-normalised eval parity the gate for
flipping `VECTOR_STORE_BACKEND` away from `qdrant`.

The `pipeline_query.py` seam (3 lines, NOT applied — see
docs/azure-integration.md)
------------------------------------------------------------------------
`pipeline_query.py` is dirty in this working tree, so the factory is designed
around a seam that keeps its existing tests working instead of changing them.
Both protected tests patch the symbol in `pipeline_query`'s OWN namespace:

    tests/test_health_endpoints.py:53   patch("maia.pipeline_query.QdrantStore")
    tests/test_reranker_lifecycle.py:83 monkeypatch.setattr(pq, "QdrantStore", ...)

If `build_stack` merely called `build_vector_store()`, the factory would build
the REAL QdrantStore in a unit test — a network probe plus DDL. So the factory
takes the store CLASS as a parameter and `pipeline_query` passes its own
module-level name. The wiring is:

    from .retrieval_backends import build_vector_store        # add to imports
    ...
    store = build_vector_store(dim=embedder.dim,               # replace 2 lines
                               qdrant_store_cls=QdrantStore)  #   (see the diff)

`qdrant_store_cls=None` falls back to THIS module's `QdrantStore` name, so
`patch("maia.retrieval_backends.QdrantStore")` also works for new tests.

The `except TypeError` probe is a CROSS-TENANT HAZARD
----------------------------------------------------
`retriever.retrieve` calls `store.search(qvec, top_k=..., tenant_id=tid)` and
retries WITHOUT `tenant_id` on `TypeError`. That retry is a full-corpus read,
so the rule the adapters obey here is the opposite of the intuitive one:

  * `TypeError` means "this store does not accept these ARGUMENTS". It may only
    come from argument binding -- never from building a filter, never from a
    network call, never from parsing a response. An adapter that raises
    `TypeError` for "I could not build my filter" hands the caller an
    UNFILTERED read, which is exactly the cross-tenant leak this module is
    reviewed for.
  * Every other failure is a DOMAIN error: `BackendConfigurationError`
    (subclasses `ValueError`) or `AzureSearchUnavailable` (subclasses
    `RuntimeError`). Neither is a `TypeError`, so neither is caught by the
    retriever's probe -- the call fails loudly instead of degrading into a
    wider read.

`tests/test_retrieval_port.py` pins both halves: a cross-tenant probe
regression test, and a check that a non-`str` `extra_filter` surfaces as
`BackendConfigurationError`, never as `TypeError`.

`InMemoryVectorStore` is in `maia.test_utils` and imported LAZILY
-----------------------------------------------------------------
`backend="memory"` is a supported production setting, so `build_vector_store`
below returns that class -- but it is a test/offline store that lives in
`test_utils` because ~13 test files import it from there, and moving it is a far
larger blast radius than this change. The import is therefore done inside the
`memory` branch rather than at module scope, so importing this module on a
deployed container (where the Azure SDK is deliberately absent) no longer
drags a test module into `sys.modules`.
"""
from __future__ import annotations

import inspect
import uuid
from collections.abc import Sequence
from typing import Any, Final

from .chunking import Chunk
from .config import settings
from .retrieval_port import (
    PayloadFilter,
    ScrolledChunk,
    SearchHit,
    VectorStorePort,
)
from .vector_store import QdrantStore, _chunk_hash

BACKEND_QDRANT: Final[str] = "qdrant"
BACKEND_AZURE_AI_SEARCH: Final[str] = "azure_ai_search"
BACKEND_MEMORY: Final[str] = "memory"

#: Every accepted value of `settings.VECTOR_STORE_BACKEND`. Kept as a tuple so
#: the error message for a typo lists the real options instead of a hand-typed
#: copy that drifts.
SUPPORTED_BACKENDS: Final[tuple[str, ...]] = (
    BACKEND_QDRANT,
    BACKEND_AZURE_AI_SEARCH,
    BACKEND_MEMORY,
)


#: `count()` sentinel for "the backend could not answer", as opposed to 0 which
#: is a real answer ("the index is empty"). `api.py` already uses -1 the same
#: way for `kb_points` (api.py:806); see `AzureAISearchAdapter.count`.
COUNT_UNKNOWN: Final[int] = -1


def _accepts_keyword(fn: Any, name: str) -> bool:
    """True when `fn` accepts a keyword argument called `name`.

    Probing the SIGNATURE rather than calling and catching `TypeError` is
    deliberate: at the call site a `TypeError` from argument binding and a
    `TypeError` from inside the callee are indistinguishable, and only the
    first is safe to interpret (see the module docstring).
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # C callables have no introspectable sig
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class BackendConfigurationError(ValueError):
    """Backend không hợp lệ hoặc thiếu cấu hình.

    `ValueError` because it is a configuration value that is wrong, not a
    dependency that is missing -- callers that already catch `ValueError`
    around settings validation keep working.

    It is deliberately NOT a `TypeError`, and that is load-bearing, not
    cosmetic: `retriever.retrieve`/`rebuild` retry a read WITHOUT
    `tenant_id` when a store call raises `TypeError`, so any `TypeError`
    raised from inside an adapter turns a tenant-scoped read into a
    cross-tenant one. `except TypeError` cannot catch a `ValueError`.
    """


class AzureSearchUnavailable(RuntimeError):
    """Thiếu `azure-search-documents`, hoặc thiếu credential.

    Domain error để caller không phải `except` SDK type. Mirror của
    `maia.azure_identity.AzureIdentityUnavailable`.
    """


def _stable_point_id(chunk_id: str) -> str:
    """Point id derivation: the SAME `uuid5(NAMESPACE_URL, key)` scheme
    as `QdrantStore`, over the same key in every case that occurs in
    practice.

    What this gives: `exists()` means the same thing on both backends, and
    re-ingest is idempotent on both (same `chunk_id` -> same id ->
    overwrite instead of a second copy). Changing one derivation without
    the other silently duplicates every chunk.

    What this is NOT: byte-identical in every case. `QdrantStore.upsert`
    keys on `payload.get("chunk_id", payload["chunk_hash"])` (a `.get`
    with a default, so a payload whose `chunk_id` is present but EMPTY
    keys on `""` there), while this function's caller
    `AzureAISearchAdapter.upsert` treats an empty `chunk_id` as absent
    and falls back to the content hash. Identical for every chunk with a
    non-empty `chunk_id`; divergent only for the degenerate empty-string
    case, which no ingest pipeline produces. Do not describe this as
    byte-identical.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


class QdrantAdapter:
    """Pass-through adapter: `QdrantStore` already matches the port.

    Not a subclass and not a re-implementation on purpose: every line of
    read/write behaviour stays in `maia.vector_store`, so the Qdrant path is the
    same code the eval baselines were measured on. The one addition is
    `delete_by_doc`'s `tenant_id` argument, which the wrapped store does not
    have -- see below for what happens when a caller uses it.
    """

    def __init__(self, store: QdrantStore) -> None:
        # Untyped at runtime in practice: the tests inject fakes and a patched
        # `QdrantStore` symbol. Only `.client`/`.collection`/`.dim`/`.url` are
        # ever read off it, all of them forwarded below.
        self._store = store

    # -- attributes the rest of src/ reaches on a store --------------------- #
    # `client` is the important one: llamaindex_store.py:62 does
    # `getattr(self._store, "client", None)`. A missing attribute returns None
    # there, which does not raise -- it just turns off the LlamaIndex
    # dataplane. Forwarding keeps that seam working through the adapter.
    @property
    def client(self) -> Any:
        return getattr(self._store, "client", None)

    @property
    def collection(self) -> str:
        return str(getattr(self._store, "collection", ""))

    @property
    def dim(self) -> int:
        return int(getattr(self._store, "dim", 0))

    @property
    def url(self) -> str:
        return str(getattr(self._store, "url", ""))

    # -- VectorSearchPort ---------------------------------------------------- #
    def search(
        self,
        query_vec: Sequence[float],
        top_k: int = 10,
        score_threshold: float | None = None,
        tenant_id: str | None = None,
        extra_filter: PayloadFilter = None,
    ) -> list[SearchHit]:
        # QdrantStore.search builds exactly these 4 keys at
        # vector_store.py:207-218, but its own annotation is a bare
        # `list[dict]`, so the checker cannot see the shape it really returns.
        # Unifying the two annotations is out of scope for this change
        # (ADR-0005) and would change what every consumer sees.
        store = self._store
        return store.search(  # pyright: ignore[reportReturnType]
            query_vec,
            top_k=top_k,
            score_threshold=score_threshold,
            tenant_id=tenant_id,
            extra_filter=extra_filter,
        )

    def scroll_all(
        self,
        limit: int = 10000,
        tenant_id: str | None = None,
    ) -> list[ScrolledChunk]:
        # Same untyped-source boundary as search(); scroll_all builds the
        # 3-key ScrolledChunk at vector_store.py:240-244.
        store = self._store
        return store.scroll_all(  # pyright: ignore[reportReturnType]
            limit=limit, tenant_id=tenant_id
        )

    # -- VectorWritePort ----------------------------------------------------- #
    def upsert(
        self, vectors: Sequence[Sequence[float]], chunks: Sequence[Chunk]
    ) -> int:
        return self._store.upsert(vectors, chunks)

    def upsert_one(self, chunk_id: str, vector: Sequence[float], payload: dict) -> str:
        return self._store.upsert_one(chunk_id, vector, payload)

    def exists(self, chunk_id: str) -> bool:
        return self._store.exists(chunk_id)

    def delete_by_doc(self, doc_id: str, tenant_id: str | None = None) -> None:
        """Delete a document's chunks, scoped to `tenant_id` when supplied.

        `QdrantStore.delete_by_doc` (vector_store.py:220) builds its filter
        from `doc_id` alone -- it has no `tenant_id` parameter. Doc ids are
        filename-derived and therefore collide across tenants, so a delete
        with no tenant scope removes another tenant's rows too. Rather than
        accepting the parameter and silently ignoring it (which would leave
        the port's tenant-boundary claim false), this forwards it when the
        wrapped store supports it and otherwise REFUSES.

        Callers that own a tenant must pass it. The refusal is deliberate:
        a loud failure is recoverable, a cross-tenant delete is not.
        """
        if tenant_id is None:
            self._store.delete_by_doc(doc_id)
            return
        if not _accepts_keyword(self._store.delete_by_doc, "tenant_id"):
            raise BackendConfigurationError(
                "The wrapped Qdrant store cannot scope delete_by_doc to a "
                "tenant (QdrantStore.delete_by_doc takes no tenant_id), so "
                f"deleting doc_id={doc_id!r} for tenant_id={tenant_id!r} "
                "would delete other tenants' chunks too. Refusing instead "
                "of widening. See maia.retrieval_port.VectorWritePort."
                "delete_by_doc."
            )
        self._store.delete_by_doc(doc_id, tenant_id=tenant_id)  # pyright: ignore[reportCallIssue]

    def count(self) -> int:
        return self._store.count()


class AzureAISearchAdapter:
    """Azure AI Search (HNSW vector profile) behind the MAIA vector-store port.

    Index shape expected on the service side — this class does NOT create the
    index (the Bicep module in `infra/` owns provisioning, so the index is
    reviewable IaC rather than a side effect of starting the app):

        id                  Searchable, key=true, filterable  — the stable point id
        chunk_id            Searchable, filterable
        text                Searchable
        tenant_id           Filterable   (the tenant boundary, see below)
        doc_id              Filterable   (delete_by_doc / re-index)
        <other metadata>    Filterable as needed (filename, session_id, ...)
        embedding_values    SearchField, vector, HNSW, dim = the embedder's dim

    Two properties of the service that shape this code and are easy to get
    wrong:

    * **No scroll.** AI Search pages with `$skip`/`$top`; there is no
      server-side cursor. `scroll_all` therefore pages and stops at `limit`.
      A large corpus costs one query per 1000 chunks — acceptable for the
      BM25 corpus rebuild (which already reads the whole corpus) and NOT
      acceptable on a request path, which is why nothing here calls it there.

    * **`@search.score` is not a cosine score.** For a vector query it is a
      monotonic transform of the index profile's distance function (which is
      not Qdrant's COSINE), and it is a BM25 score the moment the query also
      carries search text. Two consequences, both real:
        - `score_threshold` below is applied AFTER ranking, so the same
          threshold keeps a different number of hits than on Qdrant. The set
          is right, the cutoff point is not identical.
        - `retriever.retrieve` and `pipeline_query` threshold on
          `SIMILARITY_THRESHOLD` (0.3) against `dense_score`. A cross-backend
          eval comparison is therefore meaningless until scores are
          normalised. That normalisation is the eval-parity gate in ADR-0005
          and is deliberately NOT faked here by rescaling `@search.score` into
          a fake cosine: a rescaled BM25 number is still a BM25 number, and a
          wrong one is worse than an obviously-different one.
    """

    VECTOR_FIELD: Final[str] = "embedding_values"
    ID_FIELD: Final[str] = "id"
    CHUNK_ID_FIELD: Final[str] = "chunk_id"
    TEXT_FIELD: Final[str] = "text"
    TENANT_FIELD: Final[str] = "tenant_id"
    DOC_ID_FIELD: Final[str] = "doc_id"

    #: Service paging limits: 1000 per search request, 1000 documents per
    #: delete request. Anything larger is rejected by the service, not clamped.
    PAGE_SIZE: Final[int] = 1000
    DELETE_BATCH_SIZE: Final[int] = 1000

    def __init__(
        self,
        *,
        endpoint: str,
        index_name: str,
        dim: int = 0,
        credential: Any = None,
        api_key: str = "",
        vector_field: str = VECTOR_FIELD,
        select_fields: Sequence[str] | None = None,
        client: Any = None,
    ) -> None:
        if not endpoint.strip():
            raise BackendConfigurationError(
                "AZURE_AI_SEARCH_ENDPOINT is required for the "
                f"{BACKEND_AZURE_AI_SEARCH!r} backend."
            )
        if not index_name.strip():
            raise BackendConfigurationError(
                "AZURE_AI_SEARCH_INDEX is required for the "
                f"{BACKEND_AZURE_AI_SEARCH!r} backend."
            )
        # `Any` below is only about not hard-depending on azure-search-documents
        # (see _make_client). It is NOT permission to pass a key as a
        # principal: `api_key` is the string case, and keeping it separate is
        # what stops the managed-identity path from being taken with a string
        # and failing later as a 401 with no useful message.
        if isinstance(credential, str):
            raise BackendConfigurationError(
                "credential must be a TokenCredential "
                "(azure.core.credentials.TokenCredential), not a str. Pass an "
                "API key via the `api_key` parameter instead."
            )
        if isinstance(client, str):
            raise BackendConfigurationError(
                "client must be a SearchClient instance, not a str."
            )
        self._endpoint = endpoint.strip()
        self._index_name = index_name.strip()
        # `dim` is recorded, not enforced: the index's vector width is part of
        # its definition and changing it means re-creating the index. Reading
        # it back would be a network call on every construction.
        self._dim = dim
        self._credential = credential
        self._api_key = api_key
        self._vector_field = vector_field
        # `select` is left unset by default, which returns every *retrievable*
        # field. That is only cheap because the Bicep index must mark the
        # vector field `retrievable: false` — otherwise every scroll ships the
        # embedding back over the wire. Pass `select_fields` to be explicit.
        self._select_fields = list(select_fields) if select_fields else None
        # An injected client short-circuits construction. Two real uses: a
        # caller that already owns a configured SearchClient (DI container,
        # or one wrapped for audit logging), and the offline tests, which
        # exercise every method against a stub without a subscription.
        self._client = client if client is not None else self._make_client()

    # -- read-only introspection --------------------------------------------- #
    # Exposed so a /ready-style probe and the offline tests can report what
    # was actually built without reaching into the private attributes. Values
    # are returned, never mutated in place.
    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def index_name(self) -> str:
        return self._index_name

    @property
    def dim(self) -> int:
        return self._dim

    # -- construction -------------------------------------------------------- #
    def _make_client(self) -> Any:
        """Build the SearchClient. Lazy import, like `maia.persistence`.

        `api_key` is the string case and `credential` is the TokenCredential
        case; `_make_client` picks whichever is set, and only falls through to
        the managed identity when neither is.
        """
        try:
            from azure.search.documents import (  # pyright: ignore[reportMissingImports] - optional dep; ImportError becomes AzureSearchUnavailable below
                SearchClient,
            )
        except ImportError as exc:  # pragma: no cover - depends on local install
            raise AzureSearchUnavailable(
                "Thiếu azure-search-documents. Cài: "
                "pip install 'azure-search-documents>=12.0'"
            ) from exc

        credential: Any = self._api_key or self._credential
        if not credential:
            # Resolve the managed identity only when there is nothing else,
            # so tests and API-key deployments never touch azure.identity.
            # Imported lazily AND guarded: maia.azure_identity lands in
            # TODO 5, so its absence here is a configuration error, not a
            # ModuleNotFoundError leaking to the caller.
            try:
                from .azure_identity import (  # pyright: ignore[reportMissingImports] - lands in TODO 5; ImportError guarded below
                    azure_credential,
                )
            except ImportError as exc:
                raise AzureSearchUnavailable(
                    "No API key/credential configured and maia.azure_identity "
                    "is unavailable: managed-identity auth is not wired yet "
                    "(TODO 5)."
                ) from exc
            credential = azure_credential()
        return SearchClient(
            endpoint=self._endpoint,
            index_name=self._index_name,
            credential=credential,
        )

    # -- OData ---------------------------------------------------------------- #
    @staticmethod
    def _escape(value: str) -> str:
        """Escape a value for an OData string literal.

        OData escapes a single quote by doubling it. A tenant id containing
        `'` is not hypothetical -- `O'Brien` is exactly the kind of id that
        shows up, and an unescaped one turns a filter into a query error or,
        worse, a filter that matches more than intended.
        """
        return value.replace("'", "''")

    def _build_filter(
        self,
        *,
        tenant_id: str | None = None,
        extra_filter: PayloadFilter = None,
    ) -> str:
        clauses: list[str] = []
        if tenant_id:
            clauses.append(f"{self.TENANT_FIELD} eq '{self._escape(tenant_id)}'")
        if extra_filter is not None:
            # extra_filter is backend-specific by definition (see
            # maia.retrieval_port). The only honest reading here is an OData
            # fragment, so anything else is REJECTED, never ignored: silently
            # dropping a filter is how a tenant-scoped read becomes a full
            # read, and this is the tenant boundary.
            #
            # BackendConfigurationError, NOT TypeError, and that is
            # load-bearing rather than stylistic -- see the module docstring.
            # `retriever.retrieve` retries WITHOUT tenant_id on `TypeError`
            # (retriever.py:113), so a TypeError here turns "unsupported
            # filter type" into an unfiltered cross-tenant read.
            if not isinstance(extra_filter, str):
                raise BackendConfigurationError(
                    f"{type(self).__name__}.extra_filter must be an OData "
                    "string fragment (e.g. \"doc_id eq 'd1'\"), got "
                    f"{type(extra_filter).__name__}. Deliberately NOT a "
                    "TypeError: MAIA probes optional store capabilities with "
                    "`except TypeError` (see maia.retrieval_port), and a "
                    "TypeError here would make `retriever.retrieve` retry this "
                    "read with no tenant filter at all -- a cross-tenant read."
                )
            clauses.append(f"({extra_filter})")
        return " and ".join(clauses)

    def _select(self) -> dict[str, list[str] | None]:
        return {"select": self._select_fields} if self._select_fields else {}

    # -- VectorSearchPort ----------------------------------------------------- #
    def search(
        self,
        query_vec: Sequence[float],
        top_k: int = 10,
        score_threshold: float | None = None,
        tenant_id: str | None = None,
        extra_filter: PayloadFilter = None,
    ) -> list[SearchHit]:
        # _build_filter runs BEFORE the guard and OUTSIDE it, so its
        # BackendConfigurationError propagates unchanged.
        odata = self._build_filter(tenant_id=tenant_id, extra_filter=extra_filter)
        try:
            from azure.search.documents.models import (  # pyright: ignore[reportMissingImports] - optional dep; ImportError becomes AzureSearchUnavailable below
                VectorizedQuery,
            )
        except ImportError as exc:  # pragma: no cover
            raise AzureSearchUnavailable(
                "Thiếu azure-search-documents. Cài: "
                "pip install 'azure-search-documents>=12.0'"
            ) from exc

        try:
            vector_query = VectorizedQuery(
                vector=list(query_vec),
                k_nearest_neighbors=top_k,
                fields=self._vector_field,
            )
            results = self._client.search(
                search_text=None,
                vector_queries=[vector_query],
                filter=odata or None,
                top=top_k,
                **self._select(),
            )
            hits: list[SearchHit] = []
            for item in results:
                score = float(item.get("@search.score") or 0.0)
                # Post-ranking cutoff, NOT the pre-ranking cutoff Qdrant
                # applies. See the class docstring: the kept set is correct,
                # the threshold is not numerically comparable across backends.
                if score_threshold is not None and score < score_threshold:
                    continue
                hits.append(self._to_search_hit(item, score))
            return hits
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            # Fold every internal TypeError/ValueError into a domain error, so
            # the only TypeError a caller can observe is one from binding THIS
            # method's signature. AttributeError/KeyError from mapping a
            # malformed vendor document fold too: neither can come from
            # argument binding, so neither risks the tenant-widening retry. Without this, an SDK signature drift in
            # `self._client.search` -- the same class of problem the
            # qdrant-client <1.10 branch exists for at vector_store.py:186 --
            # or a malformed vector would be read by `retriever.retrieve` as
            # "this store does not support tenant_id" and retried with no
            # tenant filter at all. The Azure SDK has no legacy-signature
            # branch to catch it (and no client-side re-filter either, unlike
            # vector_store.py:203), so the adapter refuses to leak instead.
            raise AzureSearchUnavailable(
                f"Azure AI Search search failed: {type(exc).__name__} "
                f"(index={self._index_name!r}, "
                f"endpoint={self._endpoint!r})."
            ) from exc

    def scroll_all(
        self,
        limit: int = 10000,
        tenant_id: str | None = None,
    ) -> list[ScrolledChunk]:
        odata = self._build_filter(tenant_id=tenant_id)
        out: list[ScrolledChunk] = []
        skip = 0
        try:
            while len(out) < limit:
                page_size = min(self.PAGE_SIZE, limit - len(out))
                page = self._client.search(
                    search_text="*",
                    filter=odata or None,
                    top=page_size,
                    skip=skip,
                    include_total_count=False,
                    **self._select(),
                )
                batch = list(page)
                if not batch:
                    break
                for item in batch:
                    doc = dict(item)
                    # metadata KEEPS text here, matching
                    # QdrantStore.scroll_all.
                    out.append(
                        {
                            "chunk_id": str(doc.get(self.CHUNK_ID_FIELD, "") or ""),
                            "text": str(doc.get(self.TEXT_FIELD, "") or ""),
                            "metadata": doc,
                        }
                    )
                skip += len(batch)
                if len(batch) < page_size:
                    break
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            # Same invariant as search(): a TypeError here would be read by
            # `retriever.rebuild` as "this store takes no tenant_id" and
            # retried with NO tenant filter, putting every tenant's chunks
            # into one BM25 corpus. Domain error instead.
            raise AzureSearchUnavailable(
                f"Azure AI Search scroll_all failed: {type(exc).__name__} "
                f"(index={self._index_name!r}, "
                f"endpoint={self._endpoint!r})."
            ) from exc
        return out[:limit]

    def _to_search_hit(self, item: Any, score: float) -> SearchHit:
        """Map a service hit to a SearchHit: metadata EXCLUDES text.

        Matches QdrantStore._do_search (vector_store.py:207-218) so
        `retriever.retrieve` and `llamaindex_store.query` need no per-backend
        branch. The INPUT field names are configurable (an index is not
        obliged to call them `chunk_id`/`text`), while the OUTPUT keys are
        fixed by the port.
        """
        doc = dict(item)
        doc.pop("@search.score", None)
        text = str(doc.pop(self.TEXT_FIELD, "") or "")
        chunk_id = str(
            doc.get(self.CHUNK_ID_FIELD, "") or doc.get(self.ID_FIELD, "") or ""
        )
        return {"chunk_id": chunk_id, "text": text, "score": score, "metadata": doc}

    # -- writes --------------------------------------------------------------- #
    def _merge_or_upload(self, documents: list[dict]) -> None:
        """`merge_or_upload_documents`, with internal TypeError/ValueError folded
        into a domain error.

        Same invariant as `search` (see the module docstring). A write has no
        retry to protect, but a `TypeError` escaping one still tells any
        `except TypeError` probe upstream that the store rejects `tenant_id` --
        the one conclusion that must never be drawn by accident.
        """
        try:
            self._client.merge_or_upload_documents(documents=documents)
        except (TypeError, ValueError) as exc:
            raise AzureSearchUnavailable(
                "Azure AI Search merge_or_upload_documents failed: "
                f"{type(exc).__name__} (index={self._index_name!r})."
            ) from exc

    def _delete_documents(self, documents: list[dict]) -> None:
        """`delete_documents`, with the same domain-error guard."""
        try:
            self._client.delete_documents(documents=documents)
        except (TypeError, ValueError) as exc:
            raise AzureSearchUnavailable(
                "Azure AI Search delete_documents failed: "
                f"{type(exc).__name__} (index={self._index_name!r})."
            ) from exc

    # -- VectorWritePort ------------------------------------------------------ #
    def _document(
        self,
        chunk_id: str,
        vector: Sequence[float],
        payload: dict,
        text: str | None = None,
    ) -> dict:
        """Build one service document. Never mutates ``payload``.

        ``text`` is passed in rather than read back out of the payload: a
        payload that still carries a STALE `text` (a re-ingest of a
        shortened document, or a payload assembled before the text changed)
        would otherwise win over the chunk's own text. `QdrantStore.upsert`
        assigns `payload["text"] = ch.text` unconditionally
        (vector_store.py:109) and so does this.

        Omitting ``text`` keeps the payload's own value, which is what
        `QdrantStore.upsert_one` does -- it has no Chunk and only hashes
        `payload.get("text", "")` (vector_store.py:130).
        """
        doc = dict(payload)
        doc.setdefault(self.CHUNK_ID_FIELD, chunk_id)
        # Same RBAC default as QdrantStore.upsert: a payload that arrives
        # without a tenant is stamped with the process default rather than
        # stored tenant-less, which would be invisible to every filtered read.
        doc.setdefault(self.TENANT_FIELD, settings.TENANT_ID)
        # `document_id` is a tolerated alias for `doc_id` (see the knowledge
        # loop payloads, and maia.test_utils:31), so BOTH keys are read. The
        # previous `doc.setdefault(DOC_ID_FIELD, str(doc.get("doc_id", "")))`
        # read and wrote the SAME key, so its fallback could only ever be ""
        # -- a payload carrying only `document_id` produced doc_id="".
        doc.setdefault(
            self.DOC_ID_FIELD,
            str(doc.get("doc_id") or doc.get("document_id") or ""),
        )
        doc[self.TEXT_FIELD] = (
            text if text is not None else str(doc.get(self.TEXT_FIELD) or "")
        )
        # Parity with QdrantStore, which stores `chunk_hash` on the bulk and
        # the single upsert alike (vector_store.py:110 and 130). It is the
        # fallback key for point-id derivation when a payload has no chunk_id,
        # so omitting it would make the two backends disagree about which
        # chunk an id belongs to.
        doc.setdefault("chunk_hash", _chunk_hash(str(doc[self.TEXT_FIELD])))
        doc[self.ID_FIELD] = _stable_point_id(
            str(doc.get(self.CHUNK_ID_FIELD) or chunk_id)
        )
        doc[self.VECTOR_FIELD] = list(vector)
        return doc

    def upsert(
        self, vectors: Sequence[Sequence[float]], chunks: Sequence[Chunk]
    ) -> int:
        documents: list[dict] = []
        # strict=True: vectors and chunks of different lengths is an
        # upstream bug, and zip() truncates silently -- the write would
        # cover a subset of the corpus while the caller believes it was
        # complete. Loud beats quiet here. (QdrantStore.upsert has the same
        # silent truncation at vector_store.py:107; that file is off-limits
        # in this change, so it is only noted.)
        for vec, chunk in zip(vectors, chunks, strict=True):
            payload = dict(chunk.metadata)
            # QdrantStore falls back to the content hash when a payload has no
            # chunk_id; mirror that so both backends key the same chunk the
            # same way. See `_stable_point_id` for the one degenerate case where
            # the two derivations still differ.
            chunk_key = str(payload.get(self.CHUNK_ID_FIELD) or _chunk_hash(chunk.text))
            documents.append(self._document(chunk_key, vec, payload, text=chunk.text))
        if not documents:
            return 0
        self._merge_or_upload(documents)
        return len(documents)

    def upsert_one(self, chunk_id: str, vector: Sequence[float], payload: dict) -> str:
        # No `text=` argument: upsert_one has no Chunk, and
        # QdrantStore.upsert_one does not overwrite the payload's text
        # either (vector_store.py:121-138).
        doc = self._document(chunk_id, vector, payload)
        point_id = str(doc[self.ID_FIELD])
        self._merge_or_upload([doc])
        return point_id

    def exists(self, chunk_id: str) -> bool:
        """True if the stable point id is already in the index.

        Returns False on any error, exactly like `QdrantStore.exists`: this
        is an idempotency optimisation (skip re-embedding), never a
        correctness gate, so a transient 429 must not fail a redelivery.
        """
        point_id = _stable_point_id(chunk_id)
        odata = f"{self.ID_FIELD} eq '{self._escape(point_id)}'"
        try:
            results = self._client.search(
                search_text=None,
                filter=odata,
                top=1,
                select=[self.ID_FIELD],
            )
            return any(True for _ in results)
        except Exception:
            return False

    def delete_by_doc(self, doc_id: str, tenant_id: str | None = None) -> None:
        """Delete every chunk of a document, scoped to one tenant when asked.

        Two steps because the service has no "delete by filter": search for
        the keys, then submit them for deletion in batches. A doc with more
        chunks than one delete batch is handled by the loop rather than
        truncated.

        `tenant_id` is part of the SEARCH filter, so the key set is already
        tenant-scoped. Callers that own a tenant MUST pass it: doc ids are
        filename-derived, so two tenants can legitimately share one and an
        unscoped delete removes both. The default keeps the historical
        behaviour for the callers that cannot supply a tenant, which is why
        the port documents this parameter as a boundary and not a
        convenience.
        """
        clauses = [f"{self.DOC_ID_FIELD} eq '{self._escape(doc_id)}'"]
        if tenant_id:
            clauses.append(f"{self.TENANT_FIELD} eq '{self._escape(tenant_id)}'")
        odata = " and ".join(clauses)
        keys: list[str] = []
        skip = 0
        while True:
            try:
                page = self._client.search(
                    search_text="*",
                    filter=odata,
                    top=self.PAGE_SIZE,
                    skip=skip,
                    select=[self.ID_FIELD],
                    include_total_count=False,
                )
                batch = list(page)
            except (TypeError, ValueError) as exc:
                raise AzureSearchUnavailable(
                    f"Azure AI Search delete_by_doc lookup failed: "
                    f"{type(exc).__name__} (index={self._index_name!r})."
                ) from exc
            if not batch:
                break
            keys.extend(str(item.get(self.ID_FIELD, "")) for item in batch)
            if len(batch) < self.PAGE_SIZE:
                break
            skip += len(batch)
        for start in range(0, len(keys), self.DELETE_BATCH_SIZE):
            self._delete_documents(
                [
                    {self.ID_FIELD: key}
                    for key in keys[start:start + self.DELETE_BATCH_SIZE]
                ]
            )

    def count(self) -> int:
        """Approximate document count, or `COUNT_UNKNOWN` (-1) on failure.

        AI Search has no exact count API; `get_count()` is the service's own
        estimate, which is good enough for the `/health` and `/ready` probes
        that consume it.

        `-1`, not `0`, on failure. 0 is a real answer -- "the index is empty"
        -- so returning it for a 403 or a network failure makes a BROKEN
        backend report as a healthy empty one, which is the exact failure a
        count probe exists to catch. `api.py` already uses -1 as its "unknown"
        sentinel for the same field (`kb_points`, api.py:806), so this follows
        the established convention instead of inventing one.

        The port documents the sentinel; a caller must render -1 as unknown,
        not as a count. (QdrantStore.count still returns 0 on failure --
        vector_store.py is out of scope for this change.)
        """
        try:
            results = self._client.search(
                search_text="*", top=1, include_total_count=True
            )
            total = results.get_count()
            return int(total) if total is not None else 0
        except Exception:
            # Resilient on purpose -- a count must not take a health probe down.
            # What it must not do is claim the index is empty.
            return COUNT_UNKNOWN


def build_vector_store(
    *,
    dim: int,
    backend: str | None = None,
    qdrant_store_cls: type[Any] | None = None,
) -> VectorStorePort:
    """Build the vector store named by `VECTOR_STORE_BACKEND`.

    Args:
        dim: embedding width. Used to create the Qdrant collection; for
            Azure AI Search it is recorded only, because the index definition
            owns the vector width (see the adapter docstring).
        backend: overrides `settings.VECTOR_STORE_BACKEND`. `None` reads
            settings; the value is lower-cased and stripped so
            `AZURE_AI_SEARCH` in an env file behaves like `azure_ai_search`.
        qdrant_store_cls: the class used to build the Qdrant store. `None`
            uses this module's `QdrantStore`. `pipeline_query` passes its own
            module-level symbol so `patch("maia.pipeline_query.QdrantStore")`
            keeps intercepting the construction — see the module docstring
            for the exact 3-line wiring.

    Raises:
        BackendConfigurationError: unknown backend name, or the Azure AI
            Search endpoint/index is missing.
        AzureSearchUnavailable: backend is Azure AI Search but the SDK is not
            installed.
    """
    name = (backend if backend is not None else settings.VECTOR_STORE_BACKEND)
    name = str(name).strip().lower()
    if name == BACKEND_MEMORY:
        # The ONE backend that does not structurally satisfy the port:
        # InMemoryVectorStore has no bulk `upsert`, only `upsert_one`. That is
        # the exact reason VectorSearchPort/VectorWritePort are split
        # (maia.retrieval_port) and the exact reason
        # ingestion_pipeline._ingest_rawdocs branches on
        # `hasattr(store, "upsert")`. The branch still runs at runtime; this
        # is the one place the type system is told about it.
        #
        # Imported HERE, not at module scope: `maia.test_utils` is a TEST
        # module, and a module-level import would put it into sys.modules on
        # every production boot -- including the ACA profile, where the Azure
        # SDK is deliberately absent. See the module docstring for why the
        # class was not moved instead.
        from .test_utils import InMemoryVectorStore

        return InMemoryVectorStore()  # pyright: ignore[reportReturnType]
    if name == BACKEND_QDRANT:
        cls = qdrant_store_cls if qdrant_store_cls is not None else QdrantStore
        store = cls(
            url=settings.QDRANT_URL,
            collection=settings.QDRANT_COLLECTION,
            dim=dim,
            api_key=settings.QDRANT_API_KEY,
        )
        return QdrantAdapter(store)
    if name == BACKEND_AZURE_AI_SEARCH:
        adapter = AzureAISearchAdapter(
            endpoint=settings.AZURE_AI_SEARCH_ENDPOINT,
            index_name=settings.AZURE_AI_SEARCH_INDEX,
            dim=dim,
            api_key=settings.AZURE_AI_SEARCH_API_KEY,
        )
        return adapter
    raise BackendConfigurationError(
        f"Unknown VECTOR_STORE_BACKEND {name!r}. "
        f"Supported: {', '.join(SUPPORTED_BACKENDS)}."
    )
