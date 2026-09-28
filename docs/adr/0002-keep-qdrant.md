# ADR-0002 — Keep Qdrant (dense) + BM25 → RRF, no pgvector migration

Status: accepted (2026-09-28).

Context: portfolio pressure to show PostgreSQL/pgvector. MAIA already runs
Qdrant (payload tenant filter, cosine) with a working BM25 sidecar and RRF
fusion (`retriever.py`), plus a LlamaIndex dataplane option.

Decision: keep Qdrant. A pgvector migration adds ops cost with zero retrieval
gain on this corpus size, and would invalidate the existing eval baselines.

Tradeoffs: Qdrant needs its own container; pgvector would co-locate with the
auth DB. Revisit if deployment must be single-database.
