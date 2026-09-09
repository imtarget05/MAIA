"""Document Lifecycle — production versioning for RAG.

Beyond "PDF -> embed -> Qdrant", production RAG must track:

  document_id, document_version, filename, content_hash,
  embedding_model, embedding_version, chunking_version,
  chunk_ids[], status, created_at, updated_at, deleted_at

Example: employee_policy.pdf is embedded with bge-m3:v1 + recursive:v2.
When the model changes bge-m3:v1 -> bge-m3:v2, we know exactly which vectors
belong to which version and can re-index cleanly.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..chunking import split_documents
from ..ingestion import RawDoc


def _now() -> str:
    return str(int(time.time()))


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class DocumentManifest:
    document_id: str
    document_version: int = 1
    filename: str = ""
    content_hash: str = ""
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_version: str = "v1"
    chunking_version: str = "recursive:v2"
    chunk_size: int = 512
    chunk_overlap: int = 50
    chunk_ids: list[str] = field(default_factory=list)
    status: str = "active"          # active | deprecated | deleted
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str | None = None


class DocumentLifecycleManager:
    def __init__(self, store, embedder, index_path: str = "./storage/document_lifecycle.json",
                 chunk_size: int = 512, chunk_overlap: int = 50,
                 embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                 embedding_version: str = "v1",
                 chunking_version: str = "recursive:v2"):
        self.store = store
        self.embedder = embedder
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self.chunking_version = chunking_version
        self.index_path = Path(index_path)
        self._manifests: dict[str, DocumentManifest] = {}
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        self._manifests = {}
        if self.index_path.exists():
            try:
                raw = json.loads(self.index_path.read_text())
                for doc_id, m in raw.items():
                    self._manifests[doc_id] = DocumentManifest(**m)
            except Exception:
                self._manifests = {}

    def _save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps({d: m.__dict__ for d, m in self._manifests.items()},
                       ensure_ascii=False, indent=2))

    def _embed_and_store(self, doc_id: str, chunks, version: int | None = None) -> list[str]:
        chunk_ids: list[str] = []
        v = version if version is not None else 1
        for i, ch in enumerate(chunks):
            base = ch.metadata.get("chunk_id", f"{doc_id}_{i}")
            # versioned chunk_id to avoid overwriting previous version vectors
            chunk_id = base if v == 1 else f"{base}_v{v}"
            vector = self.embedder.embed([ch.text])[0]
            payload = dict(ch.metadata)
            payload.update({"text": ch.text, "chunk_id": chunk_id, "document_id": doc_id,
                            "document_version": v,
                            "embedding_model": self.embedding_model,
                            "embedding_version": self.embedding_version,
                            "chunking_version": self.chunking_version})
            self.store.upsert_one(chunk_id, vector, payload)
            chunk_ids.append(chunk_id)
        return chunk_ids

    # ---- core lifecycle ----------------------------------------------------
    def upload(self, doc_id: str, text: str, filename: str = "") -> DocumentManifest:
        """Upload a new document: v1, parse, chunk, embed, index."""
        raw = RawDoc(text=text, metadata={"doc_id": doc_id, "filename": filename or doc_id})
        chunks = split_documents([raw], chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        chunk_ids = self._embed_and_store(doc_id, chunks, version=1)
        manifest = DocumentManifest(
            document_id=doc_id, document_version=1, filename=filename or doc_id,
            content_hash=_content_hash(text), embedding_model=self.embedding_model,
            embedding_version=self.embedding_version, chunking_version=self.chunking_version,
            chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap,
            chunk_ids=chunk_ids, status="active", created_at=_now(), updated_at=_now())
        self._manifests[doc_id] = manifest
        self._save()
        return manifest

    def update_document(self, doc_id: str, text: str, filename: str = "") -> DocumentManifest:
        """Update an existing document: bump version, deprecate old, re-index."""
        old = self._manifests.get(doc_id)
        new_version = (old.document_version + 1) if old else 1
        if old:
            old.status = "deprecated"
            old.deleted_at = _now()
        raw = RawDoc(text=text, metadata={"doc_id": doc_id, "filename": filename or (old.filename if old else doc_id)})
        chunks = split_documents([raw], chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        chunk_ids = self._embed_and_store(doc_id, chunks, version=new_version)
        manifest = DocumentManifest(
            document_id=doc_id, document_version=new_version,
            filename=filename or (old.filename if old else doc_id),
            content_hash=_content_hash(text), embedding_model=self.embedding_model,
            embedding_version=self.embedding_version, chunking_version=self.chunking_version,
            chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap,
            chunk_ids=chunk_ids, status="active", created_at=old.created_at if old else _now(),
            updated_at=_now())
        self._manifests[doc_id] = manifest
        self._save()
        return manifest

    def delete_document(self, doc_id: str) -> dict:
        """Soft delete: set status=deleted, deleted_at, remove vectors."""
        m = self._manifests.get(doc_id)
        if m:
            m.status = "deleted"
            m.deleted_at = _now()
            m.updated_at = _now()
            self.store.delete_by_doc(doc_id)
            self._save()
        return {"document_id": doc_id, "action": "delete", "status": m.status if m else "not_found"}

    def change_embedding_model(self, model: str, version: str) -> dict:
        """Change embedding model/version; old vectors keep their version tag."""
        self.embedding_model = model
        self.embedding_version = version
        self._save()
        return {"action": "embedding_model_changed", "model": model, "version": version,
                "note": "existing vectors keep old version; re-index to upgrade"}

    # ---- queries -----------------------------------------------------------
    def get_manifest(self, doc_id: str) -> DocumentManifest | None:
        return self._manifests.get(doc_id)

    def list_documents(self, include_deleted: bool = False) -> list[DocumentManifest]:
        return [m for m in self._manifests.values()
                if include_deleted or m.status != "deleted"]

    def get_chunks_by_version(self, doc_id: str, version: int) -> list[dict]:
        """Retrieve chunk payloads for a specific document version."""
        return [p for p in self.store.scroll_all()
                if p.get("metadata", {}).get("document_id") == doc_id
                and p.get("metadata", {}).get("document_version") == version]
        return chunk_ids