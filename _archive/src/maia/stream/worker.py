"""Embedding worker (spec §5, §6, §7, §8).

Per-message pipeline (spec §6): consume -> validate -> idempotency check ->
embed -> upsert -> ONLY THEN commit offset.

* Idempotency (§7): upsert keyed by `chunk_id` + skip-re-embed if already stored,
  so restarts/redeliveries never duplicate vectors.
* Failures (§8): inline retries with backoff; after `max_retries` the chunk is
  routed to `topic.doc.chunks.dlq` and a `topic.doc.embedding.failed` event is
  published. The offset is committed only once the chunk is *handled*.
* Consumer group (§5): each worker owns a disjoint set of partitions, so adding
  workers re-balances load (1 -> 2 -> 4 -> 8).
"""
from __future__ import annotations

import time

from ..config import settings
from .dlq_handler import DLQHandler
from .events import ChunkEvent
from .metrics import MetricsRegistry, registry
from .transport import InMemoryBroker


class MessageValidator:
    """Validates ChunkEvent messages before processing."""

    def validate(self, event: ChunkEvent) -> bool:
        return bool(event.chunk_id and event.text and event.text.strip())


class RetryManager:
    """Handles retry logic with exponential backoff for transient failures."""

    def __init__(self, max_retries: int, retry_backoff_ms: int,
                 metrics: MetricsRegistry) -> None:
        self.max_retries = max_retries
        self.retry_backoff_ms = retry_backoff_ms
        self.metrics = metrics

    def run(self, operation) -> Exception | None:
        """Execute ``operation`` with retries.

        Returns ``None`` on success, or the last ``Exception`` after
        exhausting all retries.
        """
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                operation(attempt)
                return None
            except Exception as e:  # noqa: BLE001 - transient embed/upsert errors
                last_err = e
                self.metrics.inc("maia_embedding_failures_total")
                if attempt < self.max_retries:
                    time.sleep((self.retry_backoff_ms * attempt) / 1000.0)
        return last_err


class EmbeddingWorker:
    def __init__(self, name: str, transport: InMemoryBroker, store, embedder,
                 *, group: str = settings.KAFKA_CONSUMER_GROUP,
                 topic: str = settings.KAFKA_TOPIC_CHUNKS,
                 dlq_topic: str = settings.KAFKA_TOPIC_DLQ,
                 failed_topic: str = settings.KAFKA_TOPIC_FAILED,
                 max_retries: int = settings.KAFKA_MAX_RETRIES,
                 retry_backoff_ms: int = settings.KAFKA_RETRY_BACKOFF_MS,
                 skip_embedded: bool = settings.KAFKA_SKIP_EMBEDDED,
                 metrics: MetricsRegistry = registry,
                 member_index: int = 0, worker_count: int = 1) -> None:
        self.name = name
        self.transport = transport
        self.store = store
        self.embedder = embedder
        self.group = group
        self.topic = topic
        self.dlq_topic = dlq_topic
        self.failed_topic = failed_topic
        self.max_retries = max_retries
        self.retry_backoff_ms = retry_backoff_ms
        self.skip_embedded = skip_embedded
        self.metrics = metrics
        self.member_index = member_index
        self.worker_count = worker_count
        self.partitions: list[int] = transport.assign(group, member_index, worker_count)
        self._dlq_handler = DLQHandler(transport, dlq_topic, failed_topic, metrics)
        self.validator = MessageValidator()
        self.retry_manager = RetryManager(max_retries, retry_backoff_ms, metrics)
        # activity-facing counters for utilization metric
        self.polls = 0
        self.processed = 0
        self.skipped = 0
        self.failed = 0

    # ---- single-message handling ------------------------------------------
    def process_message(self, msg) -> bool:
        """Handle one message. Returns True when handled (offset can be committed)."""
        t0 = time.monotonic()
        event = ChunkEvent.model_validate_json(msg.value)
        if not self.validator.validate(event):
            # keep event.attempt semantics; DLQ immediately (not retryable)
            self._to_dlq(event, "validation error: empty text", attempts=event.attempt + 1)
            self.processed += 1
            return True
        # idempotency short-circuit (already embedded -> commit, no re-embed)
        if self.skip_embedded and hasattr(self.store, "exists"):
            try:
                if self.store.exists(event.chunk_id):
                    self.skipped += 1
                    self.processed += 1
                    self.metrics.inc("maia_embeddings_skipped_total")
                    return True
            except Exception:  # noqa: BLE001 - a failed existence probe is non-fatal
                pass

        err = self.retry_manager.run(lambda attempt: self._embed_and_upsert(event, t0))
        if err is not None:
            # exhausted retries -> DLQ + failed event (spec §8)
            error = str(err) if err else "unknown error"
            self._to_dlq(event, error, attempts=self.max_retries)
            self.failed += 1
            self.processed += 1
        return True

    def _embed_and_upsert(self, event: ChunkEvent, t0: float) -> None:
        vector = self.embedder.embed([event.text])[0]
        payload = dict(event.metadata)
        payload.update({"text": event.text, "chunk_id": event.chunk_id,
                        "document_id": event.document_id})
        self.store.upsert_one(event.chunk_id, vector, payload)
        latency = time.monotonic() - t0
        self.metrics.observe_latency(latency)
        self.metrics.inc("maia_embeddings_total")
        self.processed += 1

    def _to_dlq(self, event: ChunkEvent, error: str, attempts: int | None = None) -> None:
        attempts = attempts if attempts is not None else event.attempt + 1
        self._dlq_handler.route_to_dlq(event, error, attempts)

    # ---- consume loop --------------------------------------------------------
    def step(self) -> int:
        """Poll one message and handle it; return number handled (0 if drained)."""
        for partition in self.partitions:
            msg = self.transport.poll(self.group, self.topic, partition)
            if msg is None:
                continue
            self.polls += 1
            handled = self.process_message(msg)
            if handled:
                # commit offset only AFTER successful handling (spec §6)
                self.transport.commit(self.group, self.topic, partition, msg.offset + 1)
            return 1
        self.metrics.set("maia_worker_utilization", float(self.polls > 0))
        return 0

    def run(self, drain: bool = True, max_iterations: int = -1) -> dict:
        """Consume assigned partitions until drained or max_iterations reached."""
        iterations = 0
        while True:
            handled = self.step()
            iterations += 1
            if max_iterations > 0 and iterations >= max_iterations:
                break
            if drain and handled == 0:
                break
        return {"name": self.name, "partitions": self.partitions,
                "processed": self.processed, "skipped": self.skipped,
                "failed": self.failed, "iterations": iterations}


def run_workers(worker_count: int, transport: InMemoryBroker, store, embedder,
                drain: bool = True, metrics: MetricsRegistry = registry,
                **kw) -> list[dict]:
    """Run `worker_count` embedding workers in threads (parallel consumers)."""
    import threading

    results: dict[int, dict] = {}
    threads = []

    def _target(idx: int):
        worker = EmbeddingWorker(name=f"worker-{idx}", transport=transport, store=store,
                                  embedder=embedder, metrics=metrics,
                                  member_index=idx, worker_count=worker_count, **kw)
        results[idx] = worker.run(drain=drain)

    for idx in range(worker_count):
        t = threading.Thread(target=_target, args=(idx,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return [results[i] for i in sorted(results)]
