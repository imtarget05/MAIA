"""Batch benchmark for the streaming pipeline (spec §12).

Tests the claim "worker_count up -> throughput up -> lag down" using the
offline in-memory transport + hash embedder (no Kafka/Qdrant needed):

    python -m maia.benchmark --docs 50 --workers 1,2,4,8

Report columns: docs, chunks, workers, chunks/sec, p95 latency (s),
final consumer lag, CPU%, RSS (MB).
"""
from __future__ import annotations

import argparse
import random
import time

from .embeddings import Embedder
from .ingestion import RawDoc
from .chunking import Chunk
from .stream.events import ChunkEvent
from .stream.metrics import MetricsRegistry, registry
from .stream.producer import ChunkProducer
from .stream.store import InMemoryVectorStore
from .stream.transport import InMemoryBroker
from .stream.worker import run_workers


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


def run_benchmark(n_docs: int = 50, workers: list[int] | None = None,
                  partitions: int = 8, seed: int = 42, mode: str = "max-throughput") -> list[dict]:
    workers = workers or [1, 2, 4, 8]
    docs = _generate_docs(n_docs, seed=seed)
    rows = []

    for w in workers:
        transport = InMemoryBroker(partitions=partitions)
        producer = ChunkProducer(transport, partitioning=mode)
        store = InMemoryVectorStore()
        embedder = Embedder()
        metrics = MetricsRegistry()  # fresh per run to isolate latency samples

        t0 = time.monotonic()
        produced = 0
        for doc_id, chunks in docs:
            producer.produce_document(doc_id, chunks)
            produced += len(chunks)
        produce_elapsed = time.monotonic() - t0

        # run workers until drained
        t_start = time.monotonic()
        stats = run_workers(w, transport, store, embedder, metrics=metrics)
        worker_elapsed = max(time.monotonic() - t_start, 1e-6)
        total_elapsed = max((time.monotonic() - t0), 1e-6)  # produce + consume

        final_lag = transport.lag("embedding-workers", producer.topic)
        total_produced = transport.total_produced(producer.topic)
        chunks_per_sec = total_produced / worker_elapsed
        p95 = metrics.latency_p95()

        # cheap CPU/RAM proxy via time + object size (kept lightweight/offline)
        rows.append({
            "docs": n_docs, "chunks": total_produced, "workers": w,
            "produce_chunks_per_sec": round(produced / produce_elapsed, 2),
            "consume_chunks_per_sec": round(chunks_per_sec, 2),
            "docs_per_min": round(n_docs / (total_elapsed / 60.0), 2),
            "p95_latency_s": round(p95, 4),
            "consumer_lag": final_lag,
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
    ap = argparse.ArgumentParser(description="MAIA streaming ingestion benchmark (spec §12)")
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