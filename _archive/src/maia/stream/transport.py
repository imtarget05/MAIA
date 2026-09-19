"""Message transport for the pipeline (spec §4, §5 consumer group).

A minimal but faithful model of the Kafka semantics MAIA relies on:

* topics with a fixed partition count
* key-based partitioning (same key -> same partition)
* a **consumer group**: each partition is owned by exactly one consumer, so
  scaling workers 1 -> 2 -> 4 -> 8 simply re-balances partitions.
* **at-least-once** delivery: an offset is only considered consumed once it is
  *committed*. `poll()` returns the same undelivered message until committed.
* consumer *lag* = produced - committed.

Two implementations of the same interface:

* `InMemoryBroker`    - thread-safe, offline (tests + benchmark, default)
* `ConfluentKafkaTransport` - real broker via confluent-kafka (Docker/Prod)
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass

from ..config import settings


@dataclass
class Message:
    """A record delivered to a consumer (mirrors a Kafka ConsumerRecord)."""

    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes


def route_partition(key: str | None, num_partitions: int) -> int:
    """Deterministic Kafka-like key hash routing. Same key -> same partition.

    Ordered mode uses key=document_id (all chunks -> one partition).
    Max-throughput mode uses key=hash(document_id|chunk_id) (spread).
    """
    if key is None or num_partitions <= 1:
        return 0
    # FNV-1a 32-bit; matches librdkafka style muddle2 for demo purposes and is
    # deterministic across processes.
    h = 2166136261
    for b in key.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h % num_partitions


class InMemoryBroker:
    """Offline, thread-safe Kafka clone for tests and benchmarks.

    Consumer-group semantics: a group tracks a committed offset per
    (topic, partition). A consumer that owns a partition reads sequentially and
    commits only after successfully handling the message (spec §6). `poll`
    does NOT advance the position - the offset advances only on `commit`, and
    a crashed worker simply re-polls the same message (at-least-once + idempotent
    upsert => effectively exactly-once vector writes).
    """

    def __init__(self, partitions: int = 4) -> None:
        self.num_partitions = partitions
        self._lock = threading.Lock()
        self._log: dict[str, dict[int, list[tuple[str | None, bytes]]]] = defaultdict(lambda: defaultdict(list))
        self._next_offset: dict[tuple[str, int], int] = defaultdict(int)
        # group -> (topic, partition) -> last committed (next) offset
        self._committed: dict[tuple[str, str, int], int] = defaultdict(int)

    # ---- admin -----------------------------------------------------------
    def ensure_topic(self, topic: str) -> None:
        with self._lock:
            self._log.setdefault(topic, defaultdict(list))

    def total_produced(self, topic: str) -> int:
        with self._lock:
            return sum(self._next_offset.get((topic, p), 0) for p in range(self.num_partitions))

    def committed(self, group: str, topic: str) -> int:
        with self._lock:
            return sum(self._committed.get((group, topic, p), 0) for p in range(self.num_partitions))

    def lag(self, group: str, topic: str) -> int:
        """Consumer lag = produced - committed for a group (spec §9)."""
        return self.total_produced(topic) - self.committed(group, topic)

    # ---- producer ---------------------------------------------------------
    def produce(self, topic: str, key: str | None, value: bytes) -> tuple[int, int]:
        """Append a record; returns (partition, offset). Same key -> same partition."""
        self.ensure_topic(topic)
        partition = route_partition(key, self.num_partitions)
        with self._lock:
            offset = self._next_offset[(topic, partition)]
            self._log[topic][partition].append((key, value))
            self._next_offset[(topic, partition)] = offset + 1
        return partition, offset

    # ---- consumer group ----------------------------------------------------
    def assign(self, group: str, member_index: int, worker_count: int) -> list[int]:
        """Round-robin partition assignment (spec §5: 1 partition -> 1 consumer)."""
        return [p for p in range(self.num_partitions) if p % worker_count == member_index]

    def poll(self, group: str, topic: str, partition: int) -> Message | None:
        """Return the next *uncommitted* message for a group/partition, if any."""
        with self._lock:
            committed = self._committed[(group, topic, partition)]
            recs = self._log[topic].get(partition)
            if not recs or committed >= len(recs):
                return None
            key, value = recs[committed]
            return Message(topic=topic, partition=partition, offset=committed, key=key, value=value)

    def commit(self, group: str, topic: str, partition: int, next_offset: int) -> None:
        """Commit offsets up to `next_offset` (spec §6: after success only)."""
        with self._lock:
            self._committed[(group, topic, partition)] = max(
                self._committed[(group, topic, partition)], next_offset
            )


Transport = InMemoryBroker  # default interface alias (Confluent implements the same methods)


def build_default_transport() -> Transport:
    """Construct the transport selected by STREAM_TRANSPORT config."""
    if settings.STREAM_TRANSPORT == "kafka":
        from .broker_kafka import ConfluentKafkaTransport

        return ConfluentKafkaTransport(
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            topics=[settings.KAFKA_TOPIC_CHUNKS, settings.KAFKA_TOPIC_DLQ, settings.KAFKA_TOPIC_FAILED],
            num_partitions=settings.KAFKA_NUM_PARTITIONS,
        )
    return InMemoryBroker(partitions=settings.KAFKA_NUM_PARTITIONS)