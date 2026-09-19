"""Production transport on a real Kafka broker via `confluent-kafka`.

Implements the same method surface as `InMemoryBroker` (see `transport.py`)
so the workers/producers are transport-agnostic. Not imported by default:
enable with `STREAM_TRANSPORT=kafka` (used in docker-compose) and install
`requirements-kafka.txt`.

Concurrency/ordering notes:
  * Producer: `flush()` after production ensures delivery (sync-ish for demo).
  * Consumer: `assign(...)` + manual `commit()` after successful upsert keeps
    the exact "commit after success" ordering (spec §6) and lets the consumer
    group re-balance partitions as workers scale (spec §5).
"""
from __future__ import annotations

from collections.abc import Iterable

from .transport import Message, route_partition


class ConfluentKafkaTransport:
    def __init__(self, bootstrap_servers: str, topics: Iterable[str],
                 num_partitions: int = 4, group: str = "embedding-workers") -> None:
        try:
            from confluent_kafka import Producer  # noqa: F401
        except ImportError as e:  # pragma: no cover - only run with deps present
            raise RuntimeError(
                "confluent-kafka is required for STREAM_TRANSPORT=kafka; "
                "install requirements-kafka.txt"
            ) from e
        self.bootstrap_servers = bootstrap_servers
        self.topics = {t: num_partitions for t in topics}
        self.num_partitions = num_partitions
        self.group = group
        self._producer = None
        self._consumer = None

    # ---- producer ---------------------------------------------------------
    def produce(self, topic: str, key: str | None, value: bytes, flush: bool = True) -> tuple[int, int]:
        from confluent_kafka import Producer

        if self._producer is None:
            self._producer = Producer({"bootstrap.servers": self.bootstrap_servers})
        partition = route_partition(key, self.num_partitions)  # pre-compute for parity w/ in-memory
        deliver = {"acked": False, "err": None}

        def _cb(err, msg):
            deliver["acked"] = True
            if err:
                deliver["err"] = err

        self._producer.produce(topic, key=key, value=value, partition=partition, callback=_cb)
        if flush:
            self._producer.flush()
        if deliver["err"]:
            raise deliver["err"]
        return partition, 0

    def flush(self) -> None:
        if self._producer is not None:
            self._producer.flush()

    # ---- consumer group ----------------------------------------------------
    def assign(self, group: str, member_index: int, worker_count: int) -> list[int]:
        """Round-robin partition assignment identical to the in-memory broker."""
        return [p for p in range(self.num_partitions) if p % worker_count == member_index]

    def _ensure_consumer(self, group: str) -> object:
        from confluent_kafka import Consumer

        if self._consumer is None:
            self._consumer = Consumer({
                "bootstrap.servers": self.bootstrap_servers,
                "group.id": group,
                "enable.auto.commit": False,  # manual commit after success (spec §6)
                "auto.offset.reset": "earliest",
                "session.timeout.ms": 6000,
            })
        return self._consumer

    def poll(self, group: str, topic: str, partition: int, timeout: float = 0.5) -> Message | None:
        from confluent_kafka import KafkaError, KafkaException

        consumer = self._ensure_consumer(group)
        consumer.assign([(topic, partition)])
        msg = consumer.poll(timeout)
        if msg is None:
            return None
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                return None
            raise KafkaException(msg.error())
        return Message(topic=topic, partition=msg.partition(), offset=msg.offset(),
                       key=msg.key(), value=msg.value() if msg.value() is not None else b"")

    def commit(self, group: str, topic: str, partition: int, next_offset: int) -> None:
        from confluent_kafka import TopicPartition

        consumer = self._ensure_consumer(group)
        # commit the specific partition; next_offset == message.offset + 1
        consumer.commit(offsets=[TopicPartition(topic, partition, next_offset)], asynchronous=False)

    def lag(self, group: str, topic: str) -> int:
        from confluent_kafka import TopicPartition

        consumer = self._ensure_consumer(group)
        total = 0
        for partition in range(self.num_partitions):
            try:
                lo, hi = consumer.get_watermark_offsets(TopicPartition(topic, partition), timeout=5.0)
                committed = consumer.committed([TopicPartition(topic, partition)], timeout=5.0)
                off = committed[0].offset if committed else 0
                total += max(0, hi - lo - max(off - lo, 0))
            except Exception:  # noqa: BLE001 - watermark may not exist yet
                continue
        return total

    def close(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None