"""Dead-letter queue handler for failed streaming events.

Extracted from EmbeddingWorker._to_dlq (stream/worker.py)
to separate DLQ + failed event publishing from consumption logic.
"""
from .events import FailedEvent


class DLQHandler:
    """Handles routing failed events to DLQ and failed topics."""

    def __init__(self, transport, dlq_topic: str, failed_topic: str,
                 metrics=None):
        self.transport = transport
        self.dlq_topic = dlq_topic
        self.failed_topic = failed_topic
        self.metrics = metrics

    def route_to_dlq(self, event, error: str, attempts: int) -> None:
        """Publish a failed event to DLQ and failed topic.

        Also increments the DLQ metrics counter.
        """
        dlq = FailedEvent(
            chunk_id=event.chunk_id,
            document_id=event.document_id,
            error=error,
            attempt=attempts,
        ).model_dump_json().encode("utf-8")
        # DLQ keyed by chunk_id for stable ordering/grouping in dead-letter inspection
        self.transport.produce(self.dlq_topic, event.chunk_id, dlq)
        self.transport.produce(self.failed_topic, event.chunk_id, dlq)
        if self.metrics:
            self.metrics.inc("maia_dlq_total")
