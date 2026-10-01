# Cost engineering (honest numbers)

Local LAN inference (default `LLM_PROVIDER=local`): ~$0 marginal cost.
Reference commercial price (for the "saved" estimate in `/metrics/ai`):
`pipeline_query._COMMERCIAL_PER_1K_USD` = GPT-4o-mini $0.15/1M input tokens.

Where cost is tracked:

- Per query: `_est_tokens(q + answer + citations)` → `maia_tokens_total`,
  `maia_cost_saved_usd_total` (`pipeline_query.py`).
- Gateway level: `llm-gateway/server.py /admin/stats` (p50/p95, error rate,
  token/cost per project via `X-Project: MAIA` header).

Levers to reduce cost (all implemented or documented):

1. Retrieval tuning: `TOP_K_FINAL=3` default; rerank only top-8 fused.
2. Prompt reduction: concise answer style, citations as IDs not full text.
3. Cache: BM25 index + embedding reuse per process; no per-request model reload
   (`embeddings.get_embedder` singleton — originally the Render OOM fix; the
   Render platform itself is now superseded legacy, see `docs/deployment.md`).
4. Routing: mock/offline mode for tests (`MAIA_EMBED_FORCE_HASH=1`), local LLM
   for dev, commercial only when creds are set.
5. Rate limiting: 60 req/min per user on `/chat` (in-memory; Redis needed for
   multi-replica — documented limitation in `api.py`).
