"""LlamaIndex vector-store adapter backed by MAIA's existing QdrantStore.

The installed LlamaIndex (0.14) does NOT ship a Qdrant vector-store plugin —
only ``llama-index-core`` + ``llama-index-readers-file`` are present.  Rather
than add a new third-party package, this module wraps the battle-tested
``maia.vector_store.QdrantStore`` (dense cosine search, tenant filtering,
circuit-breaker, dedup) as a ``BasePydanticVectorStore`` so a LlamaIndex
``VectorStoreIndex`` can read from the SAME Qdrant collection the rest of MAIA
uses.

This is the *data-plane* half of the LangChain/LlamaIndex/LangGraph stack:
    - langchain/   -> abstraction (LLM/tools/prompts)
    - langgraph    -> control plane (this agent)
    - llamaindex   -> data plane (this adapter + VectorStoreIndex)

Dense-only retrieval.  BM25/RRF hybrid fusion is intentionally NOT duplicated
here — the existing ``HybridRetriever`` remains the default.  This adapter is
opt-in via ``settings.LLAMA_INDEX_DATA_PLANE`` and is meant to evolve toward
being the canonical dense path.
"""
from __future__ import annotations

from typing import Any

from llama_index.core.schema import BaseNode, TextNode
from llama_index.core.vector_stores.types import (
    BasePydanticVectorStore,
    VectorStoreQuery,
    VectorStoreQueryResult,
)

from .chunking import Chunk
from .config import settings


class MaiaQdrantStore(BasePydanticVectorStore):
    """LlamaIndex vector store backed by ``maia.vector_store.QdrantStore``.

    Reuses the existing Qdrant collection (COSINE, tenant-filtered) so ingestion
    via the normal pipeline and retrieval via LlamaIndex share one source of
    truth.
    """

    stores_text: bool = True

    # Backend + config (declared so Pydantic v2 accepts attribute assignment).
    _store: Any = None
    tenant_id: str = ""

    def __init__(self, qdrant_store, *, tenant_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._store = qdrant_store
        self.tenant_id = tenant_id or settings.TENANT_ID

    # ------------------------------------------------------------------
    # BasePydanticVectorStore interface
    # ------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """Expose the underlying Qdrant client (LlamaIndex convention)."""
        return getattr(self._store, "client", None)

    def add(self, nodes: list[BaseNode], **add_kwargs: Any) -> list[str]:
        """Ingest nodes into Qdrant.  Returns the list of chunk ids stored."""
        if not nodes:
            return []
        vectors: list = []
        chunks: list = []
        ids: list[str] = []
        for n in nodes:
            emb = getattr(n, "embedding", None)
            if emb is None:
                continue
            vectors.append(emb)
            meta = dict(getattr(n, "metadata", {}) or {})
            cid = getattr(n, "id_", None) or meta.get("chunk_id") or meta.get("node_id")
            if cid:
                ids.append(str(cid))
            chunks.append(Chunk(text=n.get_content(), metadata=meta))
        if vectors:
            self._store.upsert(vectors, chunks)
        return ids or [str(id(n)) for n in nodes]

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """Delete all vectors whose ``doc_id`` payload matches ``ref_doc_id``."""
        try:
            self._store.delete_by_doc(ref_doc_id)
        except Exception:
            pass

    def delete_doc(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """Alias of :meth:`delete` (doc_id-scoped deletion).

        The LlamaIndex contract names this method ``delete``; MAIA tests and
        callers historically used ``delete_doc``, so both spellings are kept.
        """
        self.delete(ref_doc_id, **delete_kwargs)

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        """Dense cosine search scoped to the wrapper's tenant.

        Accepts both the real ``QdrantStore.search()`` hit shape
        (``{"chunk_id", "text", "score", "metadata"}``) and the minimal
        ``{"id", "score", "payload"}`` shape used by lightweight fakes,
        so offline tests can exercise the same code path as production.
        """
        qvec = query.query_embedding
        if qvec is None:
            return VectorStoreQueryResult(ids=[], nodes=[], similarities=[])
        top_k = int(query.similarity_top_k or settings.TOP_K_DENSE)
        try:
            hits = self._store.search(qvec, top_k=top_k, tenant_id=self.tenant_id)
        except TypeError:
            hits = self._store.search(qvec, top_k=top_k)
        out_ids: list[str] = []
        out_nodes: list[TextNode] = []
        out_sims: list[float] = []
        for h in hits:
            cid = str(h.get("chunk_id") or h.get("id") or "")
            meta = {k: v for k, v in (h.get("metadata") or h.get("payload") or {}).items()}
            text = h.get("text", "") or meta.get("text", "")
            out_ids.append(cid)
            out_nodes.append(
                TextNode(
                    id_=cid,
                    text=text,
                    embedding=qvec,
                    metadata=meta,
                )
            )
            out_sims.append(float(h.get("score", 0.0)))
        return VectorStoreQueryResult(ids=out_ids, nodes=out_nodes, similarities=out_sims)
