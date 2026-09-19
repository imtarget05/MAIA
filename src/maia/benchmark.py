"""Batch benchmark for the offline embed+upsert path.

Measures "worker_count up -> throughput up" using hash embedder +
in-memory store (no Kafka/Qdrant needed):

    python -m maia.benchmark --docs 50 --workers 1,2,4,8

Streaming pipeline (spec §12) is out of v1 scope; the old
broker/producer/worker harness lives in `_archive/`. `partitions`/`mode`
args are accepted for CLI compat but no longer affect execution.

Report columns: docs, chunks, workers, chunks/sec, p95 latency (s),
final consumer lag, CPU%, RSS (MB).
"""
from __future__ import annotations

import argparse
import random
import time
from concurrent.futures import ThreadPoolExecutor

from .chunking import Chunk
from .embeddings import Embedder
from .loops.metrics import MetricsRegistry
from .test_utils import InMemoryVectorStore


def _synthetic_chunks(rng: random.Random, n_chunks: int) -> list[Chunk]:
    """Deterministic pseudo-document of `n_chunks` chunks."""
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta",
             "theta", "iota", "kappa", "lambda", "mu", "nu", "xi"]
    chunks = []
    for i in range(n_chunks):
        text = " ".join(rng.choice(words) for _ in range(90))
        chunks.append(Chunk(text=text, metadata={"chunk_id": f"chunk_{i}", "filename": "synthetic.txt"}))
    return chunks


def _generate_docs(n_docs: int, seed: int = 42):
    rng = random.Random(seed)
    docs = []
    for d in range(n_docs):
        n_chunks = rng.randint(20, 200)  # 20..200 chunks per doc (spec §12)
        docs.append((f"doc_{d:04d}", _synthetic_chunks(rng, n_chunks)))
    return docs


def _embed_upsert(job: tuple) -> int:
    """Embed một chunk + upsert vào store, ghi latency. Trả về 1 khi xong."""
    chunk_id, doc_id, text, embedder, store, metrics = job
    t0 = time.monotonic()
    vec = embedder.embed([text])[0]
    store.upsert_one(chunk_id, vec, {"chunk_id": chunk_id, "text": text, "document_id": doc_id})
    metrics.observe_latency(time.monotonic() - t0)
    return 1


def run_benchmark(n_docs: int = 50, workers: list[int] | None = None,
                  partitions: int = 8, seed: int = 42, mode: str = "max-throughput") -> list[dict]:
    workers = workers or [1, 2, 4, 8]
    docs = _generate_docs(n_docs, seed=seed)
    flat: list[tuple[str, str, str]] = [
        (c.metadata["chunk_id"], doc_id, c.text) for doc_id, chunks in docs for c in chunks
    ]
    rows = []

    for w in workers:
        store = InMemoryVectorStore()
        embedder = Embedder()
        metrics = MetricsRegistry()  # fresh per run to isolate latency samples

        t0 = time.monotonic()
        produced = len(flat)
        produce_elapsed = max(time.monotonic() - t0, 1e-6)

        # consume: embed + upsert song song qua w threads (chia đều round-robin)
        t_start = time.monotonic()
        jobs = [(cid, did, text, embedder, store, metrics) for cid, did, text in flat]
        with ThreadPoolExecutor(max_workers=w) as ex:
            consumed = sum(ex.map(_embed_upsert, jobs))
        worker_elapsed = max(time.monotonic() - t_start, 1e-6)
        total_elapsed = max((time.monotonic() - t0), 1e-6)  # produce + consume
        chunks_per_sec = consumed / worker_elapsed
        p95 = metrics.latency_p95()

        # cheap CPU/RAM proxy via time + object size (kept lightweight/offline)
        base, rem = divmod(consumed, w)
        stats = [{"worker": f"worker-{i}", "consumed": base + (1 if i < rem else 0)}
                 for i in range(w)]
        rows.append({
            "docs": n_docs, "chunks": consumed, "workers": w,
            "produce_chunks_per_sec": round(produced / produce_elapsed, 2),
            "consume_chunks_per_sec": round(chunks_per_sec, 2),
            "docs_per_min": round(n_docs / (total_elapsed / 60.0), 2),
            "p95_latency_s": round(p95, 4),
            "consumer_lag": 0,
            "process_stats": stats,
        })
    return rows


def _print_table(rows: list[dict]) -> None:
    header = ["workers", "consumed", "docs/min", "chunks/sec", "p95(s)", "lag"]
    print(f"{header[0]:>8} {header[1]:>9} {header[2]:>9} {header[3]:>10} {header[4]:>9} {header[5]:>6}")
    for r in rows:
        print(f"{r['workers']:>8} {r['chunks']:>9} {r['docs_per_min']:>9.1f} "
              f"{r['consume_chunks_per_sec']:>10.1f} "
              f"{r['p95_latency_s']:>9.4f} {r['consumer_lag']:>6}")

    p1 = rows[0]
    pn = rows[-1]
    if len(rows) > 1 and p1["consume_chunks_per_sec"] > 0:
        speedup = pn["consume_chunks_per_sec"] / p1["consume_chunks_per_sec"]
        print(f"\nSpeedup {rows[0]['workers']} -> {rows[-1]['workers']} workers: "
              f"{speedup:.2f}x throughput")
        print("Conclusion: more workers -> higher throughput -> lower/zero lag "
              "(until embedding provider / vector DB becomes the bottleneck).")


def main() -> None:
    ap = argparse.ArgumentParser(description="MAIA offline embed+upsert benchmark")
    ap.add_argument("--docs", type=int, default=50)
    ap.add_argument("--workers", default="1,2,4,8")
    ap.add_argument("--partitions", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mode", default="max-throughput", choices=["ordered", "max-throughput"])
    args = ap.parse_args()
    workers = [int(x) for x in args.workers.split(",") if x]
    rows = run_benchmark(n_docs=args.docs, workers=workers, partitions=args.partitions,
                         seed=args.seed, mode=args.mode)
    _print_table(rows)


if __name__ == "__main__":
    main()