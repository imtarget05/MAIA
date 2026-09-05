# PROJECT 2 — MAIA: Kafka-based Parallel Document Ingestion

> Streaming, parallel embedding ingestion for the MAIA RAG platform.
> Replaces the single-threaded `embed` bottleneck with a Kafka pipeline so the
> embedding step scales horizontally across N workers.

## 1. Pipeline overview

```
                     ┌── Embedding Worker 1 ──┐
                     │                        │
Parser ──> Kafka ────┼── Embedding Worker 2 ──┼──> Vector DB (Qdrant)
                     │                        │
                     └── Embedding Worker N ──┘
```

Each worker performs, per message, the ordering that matters (see §6):

```
consume -> validate -> idempotency check -> embed -> upsert -> commit offset
```

## 2. Chunk event

Produced onto `topic.doc.chunks` by the parser (`ChunkProducer`):

```json
{
  "event_id":     "uuid",
  "document_id":  "uuid",
  "chunk_id":     "uuid",
  "chunk_index":  15,
  "text":         "...",
  "metadata":     { "filename": "report.pdf", "page": 12 },
  "created_at":   "..."
}
```

`ChunkEvent` in `src/maia/stream/events.py`. `chunk_id` is the idempotency key (see §7).

## 3. Kafka topics

| Topic                          | Purpose                                          |
|--------------------------------|--------------------------------------------------|
| `topic.doc.chunks`             | Chunk events to embed (`KAFKA_TOPIC_CHUNKS`)     |
| `topic.doc.chunks.dlq`         | Dead-letter queue (final failures)               |
| `topic.doc.embedding.failed`   | Additional failed-event signal                   |

Consumer group: **`embedding-workers`** (`KAFKA_CONSUMER_GROUP`).

## 4. Partitioning: ordered vs max-throughput

Kafka guarantees **order only within a single partition**, and a partition is
consumed by **exactly one consumer** in a group. How you choose the message key
decides where each chunk lands, and there is a direct trade-off:

| Mode              | `key =`                        | Effect                                              |
|-------------------|--------------------------------|-----------------------------------------------------|
| **Ordered**       | `document_id`                  | all chunks of a doc → 1 partition → in-order        |
| **Max-throughput**| `hash(document_id + chunk_id)` | chunks of a doc spread across partitions → parallel |

### Ordered mode — `KAFKA_PARTITIONING=ordered`
Used when **chunk ordering within a document matters downstream**. Because every
chunk of a document uses the same key, all land in the same partition and are
processed sequentially by a single worker.

**Trade-off — the hot-partition problem.** One document with 1,000 chunks sends
all 1,000 to a single partition. That partition becomes a *hot partition*: its
owning worker does 100% of the work while the others sit idle, so total
throughput does not scale for large single documents.

```
1 document / 1000 chunks  →  (all keyed by document_id)  →  1 partition ⚠️ hot partition
```

### Max-throughput mode — `KAFKA_PARTITIONING=max-throughput`
Uses `key = hash(document_id + chunk_id)`, so chunks of a document spread across
all partitions and embed in parallel. Maximizes aggregate throughput but **does
not guarantee order** within a document.

### How to choose
- Need per-document order + docs are small → **ordered**.
- Docs are large / throughput is the priority and order is not required →
  **max-throughput**. *Production recommendation*: max-throughput for the bulk
  pipeline, with an optional ordered side-topic only for the rare case that
  truly needs order. This is why the two modes are configurable
  (`ChunkEvent.partition_key(mode)`) rather than hard-coded.

## 5. Consumer group & scaling

`embedding-workers` uses standard Kafka consumer-group semantics:
**1 partition → 1 consumer**. Adding workers re-partitions the load:

```
partitions  0 1 2 3
1 worker :  w0 w0 w0 w0
2 workers:  w0 w1 w0 w1
4 workers:  w0 w1 w2 w3   (scale 1 → 2 → 4 → 8)
```

## 6. Offset commit ordering (commit AFTER success)

Kafka marks a message consumed only when the consumer **commits the offset**.
Committing early is dangerous:

```
✗ consume -> commit -> embed -> crash     → the chunk is LOST from the flow
✓ consume -> embed -> upsert -> commit    → crash replays the chunk  (at-least-once)
```

MAIA always **commits after the vector upsert succeeds** (or after the chunk is
handled via DLQ). With an idempotent upsert this becomes *effectively
exactly-once* without losing any chunk. See `worker.step()`.

## 7. Idempotency

- Vector **point id is a stable `uuid5(chunk_id)`** and the store **`upsert`s**
  instead of `insert`s → redelivering or re-ingesting the same `chunk_id`
  overwrites the point instead of creating a duplicate.
- The worker **skips re-embedding** a chunk that already exists in the store
  (`KAFKA_SKIP_EMBEDDED=true`), so a restart after a crash does not re-encode
  already-embedded chunks.

Verified in `test_redelivered_chunk_does_not_duplicate_vector`. Implemented in
`store.upsert_one` (`InMemoryVectorStore`) and `QdrantStore.upsert_one`
(stable `uuid5(chunk_id)`).

## 8. Failed messages & DLQ

- Transient failures are retried inline with increasing backoff up to
  `KAFKA_MAX_RETRIES` (default 3).
- After exhausting retries a `FailedEvent` is produced to
  `topic.doc.chunks.dlq` **and** `topic.doc.embedding.failed`:

```json
{
  "chunk_id":    "uuid",
  "document_id": "uuid",
  "error":       "embedding timeout",
  "attempt":     3,
  "timestamp":   "..."
}
```

The offset is committed because the chunk was *handled* (routed to DLQ) — the
ingestion flow no longer blocks on it. Verified in
`test_failed_chunk_goes_to_dlq_after_retries`.

## 9. Consumer lag — the most important metric

`lag = produced - committed`. If production outpaces consumption, lag grows
continuously and ingestion is under-provisioned.

```
Produced 1000 chunks/min  -  Consumed 600 chunks/min  =  lag grows +400/min ⚠️
```

Exposed as `maia_kafka_consumer_lag` and via `GET /stream/lag`.

## 10. Autoscaling concept (future extension)

No Kubernetes autoscaling is required for this iteration; the design anticipates
it. A controller can map lag thresholds to worker replicas:

| Lag      | Workers |
|----------|---------|
| < 100    | 2       |
| 100–500  | 4       |
| > 500    | 8       |

Because consumer-group rebalancing is automatic, bringing up more replicas
immediately re-partitions the load (§5). Add this when moving to Kubernetes
(HPA on `maia_kafka_consumer_lag` or a custom controller).
## 11. Metrics (Prometheus text format, `GET /metrics`)

| Metric                          | Type      | Meaning                          |
|---------------------------------|-----------|----------------------------------|
| `maia_documents_processed_total`| counter   | documents parsed                 |
| `maia_chunks_created_total`     | counter   | chunk events produced            |
| `maia_docs_per_minute`          | gauge     | documents ingested per minute    |
| `maia_embeddings_total`         | counter   | vectors upserted                 |
| `maia_embedding_latency_seconds`| histogram | embed+upsert latency distribution|
| `maia_embedding_latency_p95`    | gauge     | p95 embed latency                |
| `maia_embedding_failures_total` | counter   | failed embeddings                |
| `maia_kafka_consumer_lag`       | gauge     | consumer lag (the key metric)    |
| `maia_ingestion_throughput`     | gauge     | chunks/sec                       |
| `maia_worker_utilization`       | gauge     | active worker fraction           |

**Focus on throughput, not single-request latency.** MAIA optimizes for
*batch ingestion*; the metrics that prove scaling are **docs/minute**,
**chunks/sec**, **consumer lag**, **embedding p95** and **worker utilization** —
not API round-trip time.

No third-party dependency: implemented in `src/maia/stream/metrics.py`,
scrapeable by Prometheus → Grafana (`grafana/maia-dashboard.json`).

## 12. Benchmark

`python -m maia.benchmark --docs N --workers 1,2,4,8 --partitions P`
(runs offline on the in-memory transport; no Kafka/Qdrant needed).

Sample run (20 docs, 8 partitions, max-throughput, desktop):

```
workers  consumed  docs/min chunks/sec    p95(s)    lag
   1       2059       90.8      156.2    0.1080      0
   2       2059      136.1      234.4    0.1996      0
   4       2059      175.9      303.5    0.2423      0

Speedup 1 -> 4 workers: ~1.9x (docs/min), ~1.9x (chunks/sec)
```

Core thesis of PROJECT 2: **worker_count ↑ ⇒ throughput ↑ ⇒ lag ↓**, until the
bottleneck shifts to the embedding provider or the vector DB.
`tests/test_stream.py` covers partitioning, consumer-group rebalancing,
end-to-end drain-to-zero-lag, idempotency, and DLQ.

## 13. CI/CD — no production test on localhost

MAIA is validated through **GitHub Actions** (`.github/workflows/ci.yml`), not by
manually running a local Qdrant/Kafka stack. The whole pipeline is *air-gapped*:

- `MAIA_EMBED_FORCE_HASH=1` forces the deterministic offline hash embedder
  (`tests/conftest.py`), so **no HF Hub model download and no network**.
- Tests run against the **in-memory** broker + vector store — no `localhost`
  Qdrant, no `topic.doc.chunks` on a local broker, no external services.
- CI jobs: offline test suite (Python 3.11 + 3.12), benchmark smoke
  (`--docs 20 --workers 1,2,4`), byte-compile, and a "dry-run" that verifies the
  stream modules import **without** `confluent-kafka`.

Run the exact CI checks locally offline at any time:

```bash
# interpret this as: stop testing against localhost production services,
# validate through the same gate CI uses.
MAIA_EMBED_FORCE_HASH=1 PYTHONPATH=src python -m pytest tests/ -q
MAIA_EMBED_FORCE_HASH=1 PYTHONPATH=src python -m maia.benchmark --docs 20 --workers 1,2,4
PYTHONPATH=src python -c "from maia.stream import producer, worker, store, transport, events, metrics"
```

## 14. Running

### Offline (no broker) — default `STREAM_TRANSPORT=inmemory`
```bash
python -m pytest tests/ -q                              # offline, no network
PYTHONPATH=src python -m maia.benchmark --docs 50 --workers 1,2,4,8
PYTHONPATH=src python -m maia.cli stream produce      # parse -> topic.doc.chunks
PYTHONPATH=src python -m maia.cli stream worker 4      # embed -> upsert -> commit
PYTHONPATH=src python -m maia.cli stream metrics       # prometheus text
PYTHONPATH=src python -m maia.cli stream lag           # consumer lag
```

> To *fully* forbid model download set `MAIA_EMBED_FORCE_HASH=1` — CI sets it
> automatically via `tests/conftest.py`.

### Docker Compose (real Kafka)
```bash
docker compose up -d kafka kafka-ui qdrant worker
# http://localhost:8080  -> kafka-ui (topics + consumer lag)
docker compose up -d --scale worker=4                   # scale workers up
```
With `STREAM_TRANSPORT=kafka`, the worker uses `requirements-kafka.txt`
(`confluent-kafka`) via `ConfluentKafkaTransport`
(`src/maia/stream/broker_kafka.py`).

## 15. Module map

```
src/maia/stream/
├── __init__.py        # package docstring (architecture)
├── events.py          # ChunkEvent / FailedEvent + partition key logic
├── transport.py       # Transport interface + InMemoryBroker (offline/tests)
├── broker_kafka.py    # ConfluentKafkaTransport (production, optional import)
├── producer.py        # ChunkProducer: parse -> chunk events -> topic.doc.chunks
├── worker.py          # EmbeddingWorker: embed -> upsert -> commit -> DLQ
├── store.py           # InMemoryVectorStore + build_stream_store (Qdrant fallback)
└── metrics.py         # Prometheus registry + /metrics text
src/maia/benchmark.py            # batch benchmark (§12)
src/maia/cli.py                  # stream {produce|worker|lag|metrics}, benchmark
src/maia/api.py                  # GET /metrics, POST /ingest/stream, /stream/*
tests/test_stream.py             # integration tests (offline)
tests/conftest.py                # forces MAIA_EMBED_FORCE_HASH (offline CI)
grafana/maia-dashboard.json      # Grafana dashboard
.github/workflows/ci.yml         # GitHub Actions CI (offline + benchmark)
docker-compose.yml               # Kafka (KRaft) + UI + worker + Qdrant
requirements-kafka.txt           # optional confluent-kafka client
```

## 16. Recommended production topology

MAIA **prioritizes Kafka throughput, not HTTP latency** — users upload whole
document batches and the system must keep the backlog from growing without bound,
not return a single embedding in 100 ms. Run it on managed infrastructure, not a
demo container:

```
User → MAIA API (Railway, Singapore)
         ↓
      Parser
         ↓
  Managed Kafka (event streaming)
         ↓
  Embedding Worker × 2/4/N   (Railway app workers, one consumer group)
         ↓
  Qdrant Cloud (vector database)
         ↓
   Managed PostgreSQL (metadata)
```

Because the `embedding-workers` consumer group rebalances automatically,
scaling is just **bringing up more worker replicas** (Railway `replicas`/HPA)
and watching the four throughput metrics in §11. The constructor code in this
repo ships with the *in-memory* + Docker-Compose flavors; the same
`worker.py`/`transport.py` run unchanged against a managed broker by setting
`STREAM_TRANSPORT=kafka` + `KAFKA_BOOTSTRAP_SERVERS`.

## 17. Definition of Done — status

| # | DoD item                          | Status |
|---|-----------------------------------|--------|
| 1 | Parser tạo chunk events           | ✅ `ChunkProducer.produce_chunk` |
| 2 | Kafka topic.doc.chunks            | ✅ compose + `build_default_transport` |
| 3 | Multiple embedding workers        | ✅ `run_workers` (threads) |
| 4 | Consumer group                    | ✅ `embedding-workers` + assignment |
| 5 | Idempotent vector upsert          | ✅ `upsert_one` by `uuid5(chunk_id)` |
| 6 | Offset commit đúng thứ tự         | ✅ commit after upsert (`worker.step`) |
| 7 | Failed chunk handling             | ✅ retries then DLQ |
| 8 | DLQ                               | ✅ `topic.doc.chunks.dlq` |
| 9 | Consumer lag monitoring           | ✅ `maia_kafka_consumer_lag`, `GET /stream/lag` |
| 10| Throughput metric                 | ✅ `maia_ingestion_throughput` |
| 11| p95 embedding latency             | ✅ histogram + `maia_embedding_latency_p95` |
| 12| Grafana dashboard                 | ✅ `grafana/maia-dashboard.json` |
| 13| Batch benchmark                   | ✅ `maia.benchmark` |
| 14| Docker Compose                    | ✅ Kafka (KRaft) + UI + worker |
| 15| Integration tests                 | ✅ `tests/test_stream.py` (12 tests) |
Confirmed in `test_consumer_group_assignment_scales`.