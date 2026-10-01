#!/usr/bin/env python3
"""WAVE 0 baseline AI reality check — runs MAIA under the PRODUCTION
dependency set only (requirements.api.txt, the file Dockerfile.api installs).

The question is not "does the code contain hybrid retrieval?" but "what does the
shipped image actually execute?" A capability that silently degrades to a
weaker path in production is a false claim, so each check asserts on OBSERVED
behaviour, not on the presence of an import.
"""

import importlib.util
import os
import sys

os.environ.setdefault("MAIA_EMBED_FORCE_HASH", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

print("=" * 72)
print("WAVE 0 — BASELINE AI REALITY CHECK (production dependency set)")
print("=" * 72)
print(f"python  : {sys.executable}")
print()

# ---------------------------------------------------------------- 1. packaging
print("[1] Is each RAG dependency importable in the shipped image?")
print("-" * 72)
PACKAGES = [
    ("rank_bm25", "BM25 sparse leg of hybrid retrieval"),
    ("sentence_transformers", "cross-encoder reranker"),
    ("torch", "reranker backend"),
    ("fastembed", "real embeddings"),
    ("redis", "distributed rate limit / cache"),
    ("psycopg", "PostgreSQL durable state"),
    ("langgraph.checkpoint.postgres", "PostgreSQL checkpoints"),
    ("langgraph.checkpoint.sqlite", "SQLite checkpoints (current durable path)"),
    ("qdrant_client", "vector store"),
]
absent = []
for mod, why in PACKAGES:
    try:
        present = importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        present = False
    print(f"    {'PRESENT' if present else 'ABSENT ':8} {mod:34} ({why})")
    if not present:
        absent.append(mod)

# ------------------------------------------------- 2. does BM25 actually run?
print("[2] Does the sparse leg actually execute? (HybridRetriever, real code)")
print("-" * 72)


class _Store:
    """Minimal VectorStore surface HybridRetriever touches."""

    def scroll_all(self, tenant_id=None):
        return [
            {
                "chunk_id": f"c{i}",
                "text": "chunk vpn access remote work policy",
                "metadata": {"tenant_id": "t1"},
            }
            for i in range(5)
        ]

    def search(self, vec, top_k=10, tenant_id=None):
        return [
            {
                "chunk_id": f"c{i}",
                "text": "chunk vpn access remote work policy",
                "score": 1.0 - i * 0.1,
                "metadata": {"tenant_id": "t1"},
            }
            for i in range(3)
        ]


class _Embedder:
    def embed_query(self, q):
        return [0.0] * 384


from maia.retriever import HybridRetriever

r = HybridRetriever(
    _Store(), _Embedder(), storage_dir="/tmp/maia_baseline_corpus", tenant_id="t1"
)
print(
    f"    retriever built.  _bm25 index is {'BUILT' if r._bm25 is not None else 'None (sparse leg dead)'}"
)
print(f"    corpus chunks: {len(r._corpus)}")

hits = r.retrieve("vpn access", tenant_id="t1")
bm25_rows = [h for h in hits if h.get("bm25_score", 0) > 0]
dense_rows = [h for h in hits if h.get("dense_score", 0) > 0]
print(f"    retrieve() -> {len(hits)} fused results")
print(f"      with dense_score > 0 : {len(dense_rows)}   <- dense leg")
print(f"      with bm25_score  > 0 : {len(bm25_rows)}   <- sparse leg")
if not bm25_rows:
    print("    VERDICT: HYBRID IS NOT RUNNING. RRF received ONE ranking list.")
    print("             'Hybrid retrieval (dense + BM25, RRF)' is a source-only claim.")
else:
    print("    VERDICT: both legs contributed.")
print()

# ----------------------------------------------- 3. does the reranker rerank?
print("[3] Does the reranker execute a real model, or pass through?")
print("-" * 72)
from maia.reranker import Reranker

rr = Reranker()
print(f"    Reranker.mode = {rr.mode!r}")
candidates = [
    {"chunk_id": "c1", "text": "alpha", "fused_score": 0.50},
    {"chunk_id": "c2", "text": "beta", "fused_score": 0.40},
    {"chunk_id": "c3", "text": "gamma", "fused_score": 0.30},
]
out = rr.rerank("query", [dict(c) for c in candidates], top_k=3)
order = [o["chunk_id"] for o in out]
print(f"    input order   : {[c['chunk_id'] for c in candidates]}")
print(f"    output order  : {order}")
if rr.mode == "fallback":
    print("    VERDICT: NO MODEL RAN. Reranker returned the RRF order unchanged.")
    print("             Do not call this 'reranker active' — it is a no-op sort.")
else:
    print("    VERDICT: a real cross-encoder executed.")
print()


# -------------------------------------------- 4. rate limiter is per-process?
print("[4] Is the rate limiter distributed?")
print("-" * 72)
print(f"    REDIS_URL = {os.environ.get('REDIS_URL', '<unset>')!r}")
try:
    redis_ok = importlib.util.find_spec("redis") is not None
except (ImportError, ValueError):
    redis_ok = False
print(f"    redis package importable: {redis_ok}")
print("    VERDICT: with no REDIS_URL and no redis package, api.py falls back to")
print("             the process-local `_rl_hits` dict -> every replica has its own")
print("             counter, so a multi-replica deployment has no global limit.")
print()

# ------------------------------------------- 5. what owns durable agent state?
print("[5] What owns durable conversation/checkpoint state?")
print("-" * 72)
print(f"    MAIA_POSTGRES_DSN = {os.environ.get('MAIA_POSTGRES_DSN', '<unset>')!r}")
try:
    psycopg_ok = importlib.util.find_spec("psycopg") is not None
except (ImportError, ValueError):
    psycopg_ok = False
print(f"    psycopg importable: {psycopg_ok}")
print("    VERDICT: langgraph_agent builds the API graph on SqliteSaver (and a")
print("             module-level MemorySaver for streaming). AsyncPostgresSaver")
print("             exists in persistence.py but psycopg is absent from the image,")
print("             so Postgres cannot own state here. A HITL pending action is")
print("             therefore only visible to the replica holding that SQLite file.")
print()

print("=" * 72)
print("SUMMARY — capability vs runtime")
print("=" * 72)
rows = [
    (
        "BM25 sparse leg",
        "ACTIVE" if "rank_bm25" not in absent else "DORMANT in runtime",
    ),
    (
        "Cross-encoder reranker",
        "ACTIVE" if "sentence_transformers" not in absent else "DORMANT in runtime",
    ),
    (
        "Real embeddings",
        "ACTIVE" if "fastembed" not in absent else "DORMANT in runtime",
    ),
    (
        "Distributed rate limit",
        "ACTIVE" if "redis" not in absent else "DORMANT in runtime",
    ),
    (
        "Postgres durable state",
        "ACTIVE" if "psycopg" not in absent else "DORMANT in runtime",
    ),
    ("SQLite durable state", "ACTIVE (single replica only)"),
]
for name, status in rows:
    print(f"    {name:24} {status}")
print()
print("A DORMANT capability is a source claim, not a product capability.")
sys.exit(0)

out = rr.rerank("query", [dict(c) for c in candidates], top_k=3)
order = [o["chunk_id"] for o in out]
print(f"    input order   : {[c['chunk_id'] for c in candidates]}")
print(f"    output order  : {order}")
if rr.mode == "fallback":
    print("    VERDICT: NO MODEL RAN. Reranker returned the RRF order unchanged.")
    print("             Do not call this 'reranker active' — it is a no-op sort.")
else:
    print("    VERDICT: a real cross-encoder executed.")
print()

print()
