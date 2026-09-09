"""Loop 1 - Context / Knowledge Loop.

Production RAG must handle more than "PDF -> embedding -> Qdrant". This manager
tracks a *knowledge index* (document -> content hash, embedding version,
chunking strategy, metadata) and orchestrates:

  * ingest(path)                             -> parse, chunk, embed, store
  * refresh(doc_id, path)                    -> document changed -> re-index
  * delete(doc_id)                           -> remove all vectors + index entry
  * reindex_all()                            -> refresh every document
  * update_embedding_version()               -> embedding model/version changed
  * change_metadata(doc_id, metadata)        -> update metadata WITHOUT re-embed
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..chunking import split_documents
from ..ingestion import RawDoc


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _sanitize_and_redact(chunks):
    """Mirror the canonical ingest tail (ingestion_pipeline._ingest_rawdocs).

    Spec requires PII 2-layer: ingest-time scan + output guardrail. This loop
    previously embedded raw chunks, diverging from the canonical pipeline.
    Returns (chunks, sanitized_count, pii_redacted_count, pii_type_counts).
    """
    from ..pipeline_wiring import _doc_sanitizer, _pii_scanner

    dirty = 0
    for c in chunks:
        cleaned, was_dirty = _doc_sanitizer.sanitize(c.text)
        if was_dirty:
            c.text = cleaned
            c.metadata["injection_sanitized"] = True
            dirty += 1
    pii_dirty = 0
    type_counts: dict[str, int] = {}
    for c in chunks:
        if _pii_scanner.is_dirty(c.text):
            hits = _pii_scanner.detect(c.text)
            for h in hits:
                type_counts[h.pii_type] = type_counts.get(h.pii_type, 0) + 1
            c.text = _pii_scanner.redact(c.text)
            c.metadata["pii_redacted"] = True
            c.metadata["pii_types"] = sorted({h.pii_type for h in hits})
            pii_dirty += 1
    return chunks, dirty, pii_dirty, type_counts


class KnowledgeManager:
    def __init__(self, store, embedder, index_path: str = "./storage/knowledge_index.json",
                 chunk_size: int = 512, chunk_overlap: int = 50):
        self.store = store
        self.embedder = embedder
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.index_path = Path(index_path)
        self.embedding_version = "bge-small-1"  # bump via update_embedding_version()
        self.chunking_strategy = f"split-{chunk_size}-{chunk_overlap}"
        self._index: dict[str, dict] = {}
        self._load_index()

    # ---- index persistence -------------------------------------------------
    def _load_index(self) -> None:
        self._index = {}
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text())
            except Exception:
                self._index = {}

    def _save_index(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(self._index, ensure_ascii=False, indent=2))

    # ---- core lifecycle -----------------------------------------------------
    def ingest(self, doc_id: str, text: str, path: str = ""):
        """Chunk -> sanitize -> PII-scan -> embed -> store (same tail as canonical pipeline)."""
        raw = RawDoc(text=text, metadata={"doc_id": doc_id, "filename": str(path)})
        chunks = split_documents([raw], chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        chunks, dirty, pii_dirty, pii_type_counts = _sanitize_and_redact(chunks)
        self._embed_and_store(doc_id, chunks, path)
        return {"doc_id": doc_id, "chunks": len(chunks), "points": self.store.count(),
                "sanitized_chunks": dirty, "pii_redacted_chunks": pii_dirty,
                "pii_type_counts": pii_type_counts}

    def _embed_and_store(self, doc_id: str, chunks, path: str) -> int:
        count = 0
        for i, ch in enumerate(chunks):
            chunk_id = ch.metadata.get("chunk_id", f"{doc_id}_{i}")
            vector = self.embedder.embed([ch.text])[0]
            payload = dict(ch.metadata)
            payload.update({"text": ch.text, "chunk_id": chunk_id,
                            "document_id": doc_id, "embedding_version": self.embedding_version,
                            "chunking_strategy": self.chunking_strategy})
            self.store.upsert_one(chunk_id, vector, payload)
            count += 1
        return count

    def refresh(self, doc_id: str, text: str, path: str = "") -> dict:
        """Remove existing vectors for a doc, then re-ingest (Loop 1: changed doc)."""
        self.store.delete_by_doc(doc_id)
        result = self.ingest(doc_id, text, path)
        result["action"] = "refresh"
        return result

    def delete(self, doc_id: str) -> dict:
        """Delete document from the vector store and the knowledge index."""
        self.store.delete_by_doc(doc_id)
        self._index.pop(doc_id, None)
        self._save_index()
        return {"doc_id": doc_id, "action": "delete", "points": self.store.count()}

    def reindex_all(self) -> dict:
        """Re-embed every chunk in the store (Loop 1: embedding/strategy change)."""
        corpus = self.store.scroll_all()
        for item in corpus:
            meta = item.get("metadata", {})
            doc_id = meta.get("document_id", meta.get("doc_id", "?"))
            self.store.upsert_one(
                item["chunk_id"], self.embedder.embed([item["text"]])[0],
                {**meta, "text": item["text"], "chunk_id": item["chunk_id"],
                 "embedding_version": self.embedding_version,
                 "chunking_strategy": self.chunking_strategy})
        return {"action": "reindex_all", "chunks": len(corpus),
                "embedding_version": self.embedding_version}

    # ---- version / metadata changes (Loop 1) --------------------------------
    def update_embedding_version(self, version: str) -> dict:
        """Simulate embedding model change: bump version + mark for re-embed."""
        self.embedding_version = version
        return {"action": "embedding_version_changed", "version": version,
                "reindex_required": self.store.count()}

    def update_chunking_strategy(self, strategy: str) -> dict:
        self.chunking_strategy = strategy
        return {"action": "chunking_strategy_changed", "strategy": strategy,
                "reindex_required": self.store.count()}

    def change_metadata(self, doc_id: str, metadata: dict) -> dict:
        """Update metadata on the payload WITHOUT re-embedding (fast path)."""
        corpus = self.store.scroll_all()
        updated = 0
        for item in corpus:
            m = item["metadata"]
            if m.get("document_id", m.get("doc_id")) == doc_id:
                self.store.upsert_one(
                    item["chunk_id"], self.embedder.embed([item["text"]])[0],
                    {**m, **metadata, "text": item["text"], "chunk_id": item["chunk_id"]})
                updated += 1
        return {"action": "metadata_changed", "doc_id": doc_id, "chunks_updated": updated}

    # ---- change detection ----------------------------------------------------
    def document_changed(self, doc_id: str, text: str) -> bool:
        """True if the current content hash differs from the last indexed one."""
        return self._index.get(doc_id, {}).get("content_hash") != _content_hash(text)

    def record(self, doc_id: str, text: str, path: str = "") -> None:
        """Record an indexed doc's fingerprint into the knowledge index."""
        self._index[doc_id] = {
            "content_hash": _content_hash(text),
            "path": str(path),
            "embedding_version": self.embedding_version,
            "chunking_strategy": self.chunking_strategy,
            "chunk_size": self.chunk_size,
            "timestamp": str(int(time.time())),
        }
        self._save_index()

    def list_stale(self) -> list[str]:
        """doc_ids recorded in the index but with no vectors in the store."""
        corp = self.store.scroll_all()
        present = {c["metadata"].get("document_id", c["metadata"].get("doc_id")) for c in corp}
        return [d for d in self._index if d not in present]