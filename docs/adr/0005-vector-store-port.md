# ADR-0005 — A vector-store port, with Qdrant still the default backend

Status: accepted (2026-10-01).

Context: ADR-0002 kept Qdrant and closed with "Revisit if deployment must be
single-database". That condition is now met: the Azure foundation
(Container Apps, managed identity, Key Vault, a Bicep `infra/`) makes a second
managed data service a per-service cost, role assignment and failure mode,
and a customer asking for one database is a realistic conversation rather
than a hypothetical one. The retrieval code itself is not the problem — it is
already well separated (`retriever.py` does hybrid dense+BM25, `vector_store.py`
does Qdrant). The problem is that the boundary is duck typing: `HybridRetriever`
took an unannotated `store`, and its `scroll_all`/`search` calls sit in
`try/except TypeError` probes with no checked return shape. Swapping backends
today means a rewrite of the retriever and no compiler help, which is exactly
the kind of migration the single-database pressure would force at the worst
possible moment.

Decision: introduce `maia.retrieval_port`, a pair of `Protocol`s
(`VectorSearchPort`, `VectorWritePort`, and their union `VectorStorePort`) that
describe the surface `src/maia` actually consumes, and `maia.retrieval_backends`
with a `build_vector_store` factory that dispatches on
`settings.VECTOR_STORE_BACKEND`. **Qdrant stays the default and stays the
measured baseline**; `azure_ai_search` and `memory` are reachable but not
promoted. ADR-0002's Qdrant decision is not reversed — it is given a boundary
it did not have.

The port is split in two because `ingestion_pipeline._ingest_rawdocs` branches
on `hasattr(store, "upsert")`: `InMemoryVectorStore` has `upsert_one` but no
bulk `upsert`. A single combined protocol would make that branch un-typeable,
and the honest capability probe is worth more than a tidier signature list.

The `search` and `scroll_all` return shapes are deliberately NOT unified here.
`search` returns 4 keys with `text` excluded from `metadata`; `scroll_all`
returns 3 keys, has no `score`, and keeps `text` inside `metadata`. Callers
depend on both (`retriever.rebuild` reads `c["text"]` off a scroll row,
`llamaindex_store.query` expects the search shape), and unifying them would
change consumer behaviour and invalidate every eval baseline. They are modelled
as two `TypedDict`s and pinned by `tests/test_retrieval_port.py` so a future
unification is a deliberate, reviewable change.

Tradeoffs: the port does not remove `QdrantStore`; the default path is
`QdrantStore` behind a thin `QdrantAdapter` pass-through, so there is one more
indirection on the read path and one more file to read. `AzureAISearchAdapter`
is unproven against a live service — no Azure subscription in CI, so it is
tested against a stub client and a fake credential. Its `@search.score` is not
cosine-comparable to Qdrant's, and its `scroll_all` is paging rather than a
scroll. Revisit the cutover only when the eval-parity gate below passes on a
real index; until then `VECTOR_STORE_BACKEND` stays `qdrant`.
