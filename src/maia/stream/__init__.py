"""Kafka streaming ingestion for MAIA (PROJECT 2).

Streaming parallel document ingestion:
    Parser -> Kafka topic.doc.chunks -> [worker x N] -> Vector Store / DLQ

Partitioning modes (documented in docs/PROJECT_2_STREAMING.md):
  * ordered          -> key = document_id (order preserved per doc, hot-partition risk)
  * max-throughput   -> key = hash(document_id + chunk_id) (spread, no order guarantee)
"""