"""Integration tests for the Kafka streaming pipeline (PROJECT 2).

Runs fully offline against the InMemoryBroker + InMemoryVectorStore + hash
embedder so no Kafka/Qdrant/broker is required.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.chunking import Chunk
from maia.config import settings
from maia.embeddings import Embedder
from maia.stream.events import ChunkEvent, FailedEvent
from maia.stream.metrics import MetricsRegistry
from maia.stream.producer import ChunkProducer
from maia.stream.store import InMemoryVectorStore
from maia.stream.transport import InMemoryBroker, route_partition
from maia.stream.worker import EmbeddingWorker, run_workers


def _chunks(n: int = 8, prefix: str = "d") -> list[Chunk]:
    return [Chunk(text=f"{prefix} sentence {i} alpha beta gamma." * 4,
                  metadata={"chunk_id": f"{prefix}_c{i}", "filename": "t.txt"})
            for i in range(n)]


def _produce(broker, doc_chunks, mode="ordered"):
    producer = ChunkProducer(broker, partitioning=mode)
    produced = []
    for doc_id, chunks in doc_chunks:
        for i, c in enumerate(chunks):
            produced.append(producer.produce_chunk(doc_id, c, i))
    return producer, produced


# ---- §4 partitioning: ordered vs max-throughput ---------------------------
def test_ordered_mode_keeps_document_on_single_partition():
    broker = InMemoryBroker(partitions=8)
    doc_chunks = [("doc_a", _chunks(6, "a")), ("doc_b", _chunks(6, "b"))]
    _, produced = _produce(broker, doc_chunks, mode="ordered")
    partitions_a = {p for p, _ in produced[:6]}
    assert len(partitions_a) == 1, "ordered mode must keep all chunks of a doc on one partition"


# ---- §6/§7 end-to-end: produce -> workers -> idempotent upsert -> lag 0 --
def test_end_to_end_single_worker_drains_to_zero_lag():
    broker = InMemoryBroker(partitions=4)
    store = InMemoryVectorStore()
    embedder = Embedder()
    producer, _ = _produce(broker, [("doc_a", _chunks(10, "a")), ("doc_b", _chunks(10, "b"))])

    worker = EmbeddingWorker("w", broker, store, embedder,
                             member_index=0, worker_count=1)
    stats = worker.run(drain=True)
    assert stats["processed"] == 20
    assert store.count() == 20
    assert broker.lag(worker.group, producer.topic) == 0
    assert stats["failed"] == 0


def test_parallel_workers_process_each_chunk_exactly_once():
    broker = InMemoryBroker(partitions=8)
    store = InMemoryVectorStore()
    embedder = Embedder()
    docs = [(f"doc_{i}", _chunks(20, f"d{i}")) for i in range(10)]
    producer, _ = _produce(broker, docs, mode="max-throughput")

    stats = run_workers(4, broker, store, embedder)
    total = sum(s["processed"] for s in stats)
    assert total == 200                       # all chunks processed
    assert store.count() == 200               # and stored exactly once (no dup vectors)
    assert broker.lag("embedding-workers", producer.topic) == 0


def test_appending_more_workers_rebalances_partitions():
    broker = InMemoryBroker(partitions=8)
    assert sum(len(broker.assign("g", i, 2)) for i in range(2)) == 8
    assert sum(len(broker.assign("g", i, 4)) for i in range(4)) == 8


# ---- §7 idempotency: duplicate chunk_id -> upsert overwrites --------------
def test_redelivered_chunk_does_not_duplicate_vector():
    broker = InMemoryBroker(partitions=2)
    store = InMemoryVectorStore()
    embedder = Embedder()
    producer = ChunkProducer(broker, partitioning="ordered")
    chunks = _chunks(5, "dup")
    for _ in range(2):  # simulate redelivery: same chunk_ids pushed twice
        for i, c in enumerate(chunks):
            producer.produce_chunk("dupdoc", c, i)
    run_workers(1, broker, store, embedder)
    assert store.count() == 5, "upsert by chunk_id must not create duplicates"


# ---- §8 fail handling: retries then DLQ ----------------------------------
class _FlakyStore(InMemoryVectorStore):
    def __init__(self, fail_ids):
        super().__init__()
        self.fail_ids = fail_ids
        self.upsert_calls = 0

    def upsert_one(self, chunk_id, vector, payload):
        self.upsert_calls += 1
        if chunk_id in self.fail_ids:
            raise RuntimeError(f"embedding timeout for {chunk_id}")
        return super().upsert_one(chunk_id, vector, payload)


def test_failed_chunk_goes_to_dlq_after_retries():
    broker = InMemoryBroker(partitions=2)
    store = _FlakyStore(fail_ids={"d_c0"})
    embedder = Embedder()
    producer = ChunkProducer(broker, partitioning="ordered")
    for i, c in enumerate(_chunks(4, "d")):
        producer.produce_chunk("docfail", c, i)

    worker = EmbeddingWorker("w", broker, store, embedder,
                             max_retries=3, retry_backoff_ms=1,
                             member_index=0, worker_count=1)
    stats = worker.run(drain=True)

    assert stats["failed"] == 1                      # one chunk failed
    assert store.count() == 3                        # 3 good chunks stored
    assert broker.lag("embedding-workers", producer.topic) == 0  # handled (DLQ'd)
    assert broker.total_produced(settings.KAFKA_TOPIC_DLQ) >= 1
    assert broker.total_produced(settings.KAFKA_TOPIC_FAILED) >= 1
    dlq_partition = route_partition("d_c0", broker.num_partitions)
    dlq_msg = broker.poll("embedding-workers", settings.KAFKA_TOPIC_DLQ, dlq_partition)
    failed = FailedEvent.model_validate_json(dlq_msg.value)
    assert failed.attempt == 3


# ---- §11 metrics: p95 latency, throughput, counters -----------------------
def test_metrics_rendering_and_latency_p95():
    m = MetricsRegistry()
    m.inc("maia_chunks_created_total", 25)
    m.inc("maia_embeddings_total", 25)
    for sec in [0.01, 0.02, 0.03, 0.05, 0.02]:
        m.observe_latency(sec)
    assert m.latency_p95() <= 0.05
    text = m.render()
    assert "maia_chunks_created_total 25" in text
    assert "maia_embedding_latency_p95" in text
    assert "maia_kafka_consumer_lag" in text


def test_throughput_recorded_by_producer():
    broker = InMemoryBroker(partitions=4)
    producer = ChunkProducer(broker, partitioning="ordered")
    for i, c in enumerate(_chunks(5, "t")):
        producer.produce_chunk("t1", c, i)
    assert broker.total_produced(producer.topic) == 5
    broker = InMemoryBroker(partitions=8)
    doc_chunks = [("doc_a", _chunks(64, "a"))]
    _, produced = _produce(broker, doc_chunks, mode="max-throughput")
    partitions = {p for p, _ in produced}
    assert len(partitions) > 1, "max-throughput must spread a doc's chunks across partitions"


def test_partition_key_routing_is_deterministic():
    assert route_partition("same", 4) == route_partition("same", 4)
    assert route_partition(None, 4) == 0


# ---- §5 consumer group: 1 partition -> 1 consumer --------------------------
def test_consumer_group_assignment_scales():
    broker = InMemoryBroker(partitions=4)
    assert broker.assign("g", 0, 1) == [0, 1, 2, 3]      # 1 worker owns all
    assert broker.assign("g", 0, 2) == [0, 2]            # 2 workers split
    assert broker.assign("g", 1, 2) == [1, 3]
    assert broker.assign("g", 0, 4) == [0]               # 4 workers: 1 partition each
    assert broker.assign("g", 3, 4) == [3]