"""Chunk producer / parser side of the pipeline (spec §2, §3, §4).

reuses MAIA's existing ingestion + chunking, then serializes each chunk into a
`ChunkEvent` and produces it to `topic.doc.chunks` with a mode-dependent key:
  * ordered          -> key = document_id
  * max-throughput   -> key = hash(document_id + chunk_id)
"""
from __future__ import annotations

import time

from ..chunking import Chunk, split_documents
from ..config import settings
from ..ingestion import load_documents
from .events import ChunkEvent
from .metrics import registry
from .transport import InMemoryBroker


class ChunkProducer:
    def __init__(self, transport: InMemoryBroker, topic: str = settings.KAFKA_TOPIC_CHUNKS,
                 partitioning: str = settings.KAFKA_PARTITIONING) -> None:
        self.transport = transport
        self.topic = topic
        self.partitioning = partitioning  # "ordered" | "max-throughput"
        transport.ensure_topic(topic)

    @property
    def mode(self) -> str:
        return self.partitioning

    def produce_chunk(self, document_id: str, chunk: Chunk, chunk_index: int) -> tuple[int, int]:
        # ensure tenant_id in metadata (RBAC)
        if "tenant_id" not in chunk.metadata:
            chunk.metadata["tenant_id"] = settings.TENANT_ID
        event = ChunkEvent(
            document_id=document_id,
            chunk_id=chunk.metadata.get("chunk_id", f"{document_id}_{chunk_index}"),
            chunk_index=chunk_index,
            text=chunk.text,
            metadata=chunk.metadata,
        )
        key = event.partition_key(self.partitioning)
        partition, offset = self.transport.produce(self.topic, key, event.model_dump_json().encode("utf-8"))
        registry.inc("maia_chunks_created_total")
        return partition, offset

    def produce_document(self, document_id: str, chunks: list[Chunk]) -> dict:
        produced: dict[int, int] = {}
        for i, chunk in enumerate(chunks):
            partition, offset = self.produce_chunk(document_id, chunk, i)
            produced[(partition, offset)] = produced.get((partition, offset), 0) + 1
        registry.inc("maia_documents_processed_total")
        return {"document_id": document_id, "chunks": len(chunks)}

    def ingest_dir(self, data_dir: str | None = None) -> dict:
        """Parse + chunk files in a directory and produce all chunk events."""
        data_dir = data_dir or settings.DATA_DIR
        docs = load_documents(data_dir)
        t0 = time.monotonic()
        docs_produced = 0
        chunks_produced = 0
        per_partition: dict[int, int] = {}
        for raw in docs:
            doc_id = raw.metadata.get("doc_id", "doc")
            # inject tenant_id at doc level
            raw.metadata.setdefault("tenant_id", settings.TENANT_ID)
            # chunking mutates index per doc; reset via split_documents per doc
            chunks = split_documents([raw], chunk_size=settings.CHUNK_SIZE, chunk_overlap=settings.CHUNK_OVERLAP)
            for i, chunk in enumerate(chunks):
                partition, offset = self.produce_chunk(doc_id, chunk, i)
                per_partition[partition] = per_partition.get(partition, 0) + 1
                chunks_produced += 1
            docs_produced += 1
        elapsed = max(time.monotonic() - t0, 1e-6)
        registry.set("maia_ingestion_throughput", chunks_produced / elapsed)
        registry.set("maia_docs_per_minute", docs_produced / (elapsed / 60.0))
        return {"docs": docs_produced, "chunks": chunks_produced,
                "mode": self.partitioning, "per_partition": per_partition,
                "throughput_chunks_per_sec": round(chunks_produced / elapsed, 2)}