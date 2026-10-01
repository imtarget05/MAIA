# Azure integration

What MAIA does with Azure, what it does without it, and the one patch that is
deferred until `pipeline_query.py` is clean again.

Read order: `docs/adr/0005-vector-store-port.md` (why a port, why Qdrant is
still the default), then this file (how it behaves in practice).

## 1. Nothing here changes a local run

Every Azure setting defaults to `""`, and every Azure import is lazy. The
proof is mechanical — with `azure-*` blocked at the meta-path level, the three
new modules still import and the failure surfaces as a domain error at the
point of use:

```python
import maia.retrieval_port, maia.retrieval_backends, maia.azure_identity
# OK with azure.* unavailable; AzureAISearchAdapter(...) then raises
# AzureSearchUnavailable("Thiếu azure-search-documents. Cài: ...")
```

That test runs as `test_the_three_new_modules_import_with_the_azure_sdk_unavailable`
in `tests/test_azure_identity.py`. It is a subprocess so an already-imported
module cannot mask the result.

Two consequences worth knowing:

* **No Azure package is in `requirements.api.txt`.** The ACA image is a
  deliberate 14-package import closure and a 1 GiB profile. `azure-identity`,
  `azure-keyvault-secrets` and `azure-search-documents` live in
  `requirements-optional.txt`, so the container starts with the default
  `VECTOR_STORE_BACKEND=qdrant` and no Azure code path loaded.
* **No network call at import time, anywhere.** `azure_credential()` builds a
  `DefaultAzureCredential` when called, never at module scope. A module-level
  credential probes IMDS, which in a container without an identity blocks
  before `/health` can answer.

## 2. Dev vs cloud: one code path, two environments

| | dev container | Azure Container Apps |
| --- | --- | --- |
| credential | ambient chain (`az login`) | user-assigned managed identity |
| which identity | none — `AZURE_CLIENT_ID` ignored | `AZURE_CLIENT_ID` pins it |
| secrets | process env / `.env` | Key Vault, read with that identity |
| vector store | Qdrant (docker) | Qdrant, or Azure AI Search |

`maia.azure_identity` is the only module that reads Azure configuration, and it
branches on exactly one thing — whether `AZURE_KEY_VAULT_URI` is set:

```python
resolve_kv_secret("JWT_SECRET_KEY")
#   AZURE_KEY_VAULT_URI set   -> Key Vault, via the managed identity
#   AZURE_KEY_VAULT_URI empty -> os.environ["JWT_SECRET_KEY"]
```

Key Vault wins whenever the URI is set, **even if the environment variable is
also set**. That is deliberate: if the environment won, a secret rotated in
the vault would keep being served from a stale container config value and
rotation would have no effect. The deployment therefore does not also use ACA
key-vault references — those push secrets into the environment, which is
precisely what the ordering above avoids.

`running_on_azure()` keys off `ENVIRONMENT`, the same signal `config.py`
already uses to fail fast on a SQLite auth DB, plus the vault URI as a
fallback for a deployment that forgot `ENVIRONMENT`. One signal, two modules,
no chance of disagreement.

## 3. The Key Vault secret-name contract

**A Key Vault secret name IS a MAIA env var name, verbatim.** There is no
mapping table, on purpose: a second list of names is the thing that drifts, and
drift between a Bicep `keyVaultSecrets` array and the code that reads it is
silent until a deploy fails at startup. With this convention
`resolve_kv_secret(name)` is correct in both environments with no lookup.

`KV_SECRET_NAMES` in `src/maia/azure_identity.py` is the authoritative list, and
`infra/` must match it:

> HONEST STATUS: two secret lists legitimately coexist today, so "must match"
> is the migration target, not the current state. Bicep's `keyVaultSecretNames`
> carries the LIVE ACA secret-refs; `KV_SECRET_NAMES` is the FUTURE resolver
> contract. The Bicep-sync test documents this skip and is re-armed at
> migration. Forcing equality now would break ACA startup.

| secret | why it is a secret |
| --- | --- |
| `JWT_SECRET_KEY` | token signing key; rotating it invalidates sessions |
| `AUTH_DB_URL` | contains the Postgres password |
| `QDRANT_API_KEY` | Qdrant Cloud write key |
| `MAIA_POSTGRES_DSN` | full DSN, credentials included (`persistence.ENV_DSN`) |
| `AZURE_AI_SEARCH_API_KEY` | local-dev fallback only; stays empty on ACA |
| `SMTP_PASSWORD` | mail relay credential |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |

**Not secrets** — plain app settings, never `keyVaultSecrets`:
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_KEY_VAULT_URI`,
`AZURE_AI_SEARCH_ENDPOINT`, `AZURE_AI_SEARCH_INDEX`, `QDRANT_URL`,
`QDRANT_COLLECTION`, `VECTOR_STORE_BACKEND`, `MAIA_CHECKPOINT_TABLE`.

Key Vault data-plane access is RBAC on the managed identity (Key Vault
Secrets User), not an API key, so the vault itself needs no provisioned
credential. The same is true of the AI Search data plane: the cloud path
authenticates with the identity, and `AZURE_AI_SEARCH_API_KEY` exists only
because a dev container has no identity to assume.

`tests/test_azure_identity.py` asserts the list is non-empty, unique, valid
Key Vault naming, and contains `persistence.ENV_DSN` — so the two spellings of
the Postgres DSN cannot drift apart.

## 4. The Azure AI Search index (if you cut over)

The index is provisioned by `infra/`; the adapter never creates it, so the
index shape is reviewable IaC rather than a side effect of starting the app.

| field | type | notes |
| --- | --- | --- |
| `id` | string | key, `filterable`; `uuid5(NAMESPACE_URL, chunk_id)` |
| `chunk_id` | string | `searchable`, `filterable` |
| `text` | string | `searchable` |
| `tenant_id` | string | **`filterable`** — this is the RBAC boundary |
| `doc_id` | string | `filterable` — `delete_by_doc` / re-index |
| `embedding_values` | `SearchField` | vector, HNSW, dim = the embedder's dim |

`embedding_values` **must** be `retrievable: false`. Without that, every
`scroll_all` page ships the embedding back over the wire.

Two service behaviours the adapter works around, both documented at the class
in `retrieval_backends.py`:

* **No scroll.** AI Search pages with `$skip`/`$top`. `scroll_all` pages and
  stops at `limit` — fine for a BM25 corpus rebuild, not for a request path.
* **`@search.score` is not a cosine score.** It is `1 / (1 + distance)` on a
  different distance function, and it becomes a BM25 score the moment a query
  has search text. `retriever.retrieve` and the evidence gate threshold
  `SIMILARITY_THRESHOLD` (0.3) against `dense_score`, so cross-backend numbers
  are not comparable until normalised. The adapter does **not** rescale
  `@search.score` into a fake cosine: a rescaled BM25 number is still a BM25
  number, and a wrong one is worse than an obviously-different one.

### The eval-parity gate for cutting over

`VECTOR_STORE_BACKEND` stays `qdrant` until all three hold on a real index:

1. Re-ingest the same corpus into Azure AI Search and diff `chunk_id` sets —
   the point ids are derived identically on both backends, so a count
   difference is a real data loss, not an id-scheme difference.
2. Re-run the golden eval with **score normalisation applied to both sides**,
   and require hit@k / recall@k / MRR within the same tolerance the LlamaIndex
   data-plane flip was held to (documented at `config.py:LLAMA_INDEX_DATA_PLANE`).
   Absolute `dense_score` and `SIMILARITY_THRESHOLD` comparisons across
   backends are not evidence of anything.
3. Re-run the evidence-gate and abstention thresholds
   (`gate8b_rag_quality.py`, `gate8b_abstention.py`) against the new backend.
   A backend that retrieves the right chunks but scores them on a different
   scale will silently change how often MAIA refuses to answer, which is the
   behaviour the abstention gate exists to protect.

Until then, `azure_ai_search` is a supported setting and an unproven one.

## 5. The deferred `pipeline_query.py` patch (3 lines, NOT applied)

`pipeline_query.py` is dirty in this working tree, so the factory is designed
around a seam that keeps the two protected tests working rather than changing
them. Both patch the symbol in `pipeline_query`'s **own** namespace:

* `tests/test_health_endpoints.py:53` — `patch("maia.pipeline_query.QdrantStore")`
* `tests/test_reranker_lifecycle.py:83` — `monkeypatch.setattr(pq, "QdrantStore", _FakeStore)`

So `build_stack` must not merely call `build_vector_store()`: the factory would
then build the **real** `QdrantStore` inside a unit test — a connectivity probe
plus collection DDL. The factory therefore takes the store *class* as a
parameter, and `pipeline_query` passes its own module-level symbol.

```diff
--- a/src/maia/pipeline_query.py
+++ b/src/maia/pipeline_query.py
@@ -17,6 +17,7 @@
 from .pipeline_wiring import PipelineTracer
 from .prompt import assemble, build_messages
 from .reranker import get_reranker
+from .retrieval_backends import build_vector_store
 from .retriever import HybridRetriever
 from .vector_store import QdrantStore

@@ -36,8 +37,8 @@
     # Initialise the embedder FIRST so its real dim (1024 for Cloudflare BGE-m3)
     # is known before QdrantStore creates the collection.
     embedder = get_embedder(model=settings.EMBED_MODEL, dim=settings.EMBED_DIM)
-    store = QdrantStore(url=settings.QDRANT_URL, collection=settings.QDRANT_COLLECTION,
-                        dim=embedder.dim, api_key=settings.QDRANT_API_KEY)
+    store = build_vector_store(dim=embedder.dim,
+                               qdrant_store_cls=QdrantStore)
     retriever = HybridRetriever(
         store, embedder, storage_dir=settings.STORAGE_DIR,
```

Why this is the right seam:

* `qdrant_store_cls=QdrantStore` reads `pipeline_query`'s module symbol **at
  call time**, so both protected patches keep intercepting construction. A
  test that forgets the parameter still gets a real store, which is the failure
  mode this avoids.
* `qdrant_store_cls=None` falls back to `retrieval_backends.QdrantStore`, so
  `patch("maia.retrieval_backends.QdrantStore")` works for new tests.
* `QdrantAdapter` forwards `.client`, so `llamaindex_store.py:62`'s
  `getattr(store, "client", None)` keeps returning a real client. A missing
  attribute there returns `None` without raising — it would silently disable
  the LlamaIndex dataplane.

The patch is exercised, not assumed:
`test_deferred_pipeline_query_wiring_still_lets_the_protected_tests_patch_the_store`
replays it against the real, unmodified module.

### Why the `retriever.py` annotation is NOT applied yet

`HybridRetriever.__init__`'s `store` parameter is still **unannotated**, and
`pyrightconfig.json` carries **no** exemption for it. This is deliberate, not an
oversight.

The annotation itself is correct and cheap — `store: VectorSearchPort` is the
honest type. It is held back for one reason: applying it today produces a
`reportArgumentType` error at `pipeline_query.py:42`, where
`HybridRetriever(store, ...)` is constructed from a bare `QdrantStore`, whose
`search` is annotated `-> list[dict]` rather than the port's `list[SearchHit]`.
That is a weaker *annotation*, not a real incompatibility —
`tests/test_retrieval_port.py` asserts the runtime shape really is the 4-key
`SearchHit`, and `isinstance(store, VectorSearchPort)` holds today.

Two ways out were weighed:

| option | cost |
| --- | --- |
| add an `executionEnvironments` entry scoped to `pipeline_query.py` with `reportArgumentType: none` | a repo-wide config relaxation, carried indefinitely, to work around **one dirty uncommitted file** |
| leave `store` unannotated | `retriever.py` keeps one untyped parameter until the tree is clean |

**The second was chosen.** A type-checker relaxation outlives the workaround that
justified it: `pyrightconfig.json` is tracked, config is read by everyone, and a
suppressed diagnostic is invisible to the next reader. The annotation is worth
more than a temporary lie about the type checker's verdict.

### The single future change: annotate **and** rewire, together

Both halves land in one patch, when `pipeline_query.py` is clean:

1. annotate `store: VectorSearchPort` in `HybridRetriever.__init__`, and import
   `VectorSearchPort` in `retriever.py`;
2. apply the 3-line `build_stack` diff above, so `pipeline_query` passes a
   `QdrantAdapter` — which *provably* satisfies the port — instead of a bare
   `QdrantStore`.

Together they close the annotation gap at its source and make the error
impossible rather than suppressed. Applied separately, either one leaves a
`reportArgumentType` error; there is no `pyrightconfig.json` change in this
plan at all, because none is needed once (1) and (2) are both in.

Verification for that patch: `pyright src/` stays at exactly the 3
`reportMissingImports` errors for optional deps (`docx`,
`langgraph.checkpoint.postgres.aio`, `psycopg_pool`), and
`tests/test_health_endpoints.py` + `tests/test_reranker_lifecycle.py` still pass
— both protected tests patch `maia.pipeline_query.QdrantStore`, which is why the
factory takes `qdrant_store_cls` rather than building a store internally.

## 6. Open item: `AUTH_DB_URL` / `connect_args`

`config.py` has no `AUTH_DB_URL`-connection-mode field and this change adds
none. `api.py` builds its SQLAlchemy engine from the DSN only, so a PostgreSQL
`AUTH_DB_URL` on Azure depends on that engine's own defaults (notably SSL)
being right. Fixing it properly means editing `api.py`, which is dirty in this
working tree, so the option is recorded here rather than guessed at: when that
file is next touched, confirm whether the Azure Postgres needs
`sslmode=require` in the DSN or a `connect_args` branch driven by a new flat
setting. No guard is added in `config.py` because that file has no validators
by design — a validator there would be a style break, and a half-correct one
would fail at an import-time assertion rather than at the connection.

## 7. Live Azure Container App Deployment (Verified)

MAIA API is deployed and verified live on Azure Container Apps:

- **Resource Group:** `rg-portfolio-evidence` (Region: East Asia)
- **Container App:** `ca-maia-api`
- **FQDN:** `https://ca-maia-api.wittysand-b748274c.eastasia.azurecontainerapps.io`
- **Active Revision:** `ca-maia-api--0000012` (Image: `ghcr.io/imtarget05/maia-maia-api:b53aca4cad4a4f32498100236f6ad3a1aa31322b@sha256:9eaa013420b6caa1282f8365bc5617d456a8d2ca9f00f84f8eb26e1bf791fcd5`)
- **Backend Infrastructure:**
  - **Vector Store:** Live Qdrant Cloud Cluster (`https://81d6d1d0-0963-465a-a4eb-69aa82d5986a.sa-east-1-0.aws.cloud.qdrant.io`, collection: `maia_knowledge`, dim: 1024)
  - **Embeddings:** Cloudflare Workers AI (`@cf/baai/bge-m3`, 1024-dim dense vectors)
  - **LLM Inference:** Cloudflare Workers AI (`@cf/meta/llama-3.1-8b-instruct`)
  - **Auth & Session:** SQLite `/tmp/maia_auth.db` with 256-bit random production `JWT_SECRET_KEY`
- **Scale:** `minReplicas=1`, `maxReplicas=1` (always warm)
- **Live Verification Endpoints:**
  - `GET /health` → `200 OK` (`status: "ok"`, `version: "0.4.0"`)
  - `GET /metrics` → `200 OK` (Prometheus metrics: `maia_ingestion_throughput`, `maia_docs_per_minute`, etc.)
  - `POST /auth/register` → `201 Created` (returns user profile with tenant and employee ID)
  - `POST /auth/login` → `200 OK` (returns JWT `access_token` and `refresh_token`)
  - `GET /auth/me` → `200 OK` (authenticated user session details)
  - `POST /query` → `200 OK` (Full real RAG pipeline: retrieves from Qdrant Cloud, computes citations `[S1]`, `[S2]`, generates natural language response via Cloudflare Llama-3.1-8B)


