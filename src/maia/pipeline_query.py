"""Query pipeline (§5): question -> embed -> hybrid retrieve -> rerank
-> context assembly -> LLM -> grounded answer + citations (§10 outputs 4,5,6)."""
from .config import settings
from .embeddings import Embedder
from .ingestion import load_documents
from .chunking import split_documents
from .llm import CloudflareLLM
from .prompt import assemble, build_messages
from .reranker import Reranker
from .retriever import HybridRetriever
from .vector_store import QdrantStore


def build_stack(tenant_id: str | None = None):
    embedder = Embedder(model=settings.EMBED_MODEL, dim=settings.EMBED_DIM)
    store = QdrantStore(url=settings.QDRANT_URL, collection=settings.QDRANT_COLLECTION,
                        dim=embedder.dim, api_key=settings.QDRANT_API_KEY)
    retriever = HybridRetriever(
        store, embedder, storage_dir=settings.STORAGE_DIR,
        top_k_dense=settings.TOP_K_DENSE, top_k_bm25=settings.TOP_K_BM25,
        top_k_fused=settings.TOP_K_FUSED, rrf_k=settings.RRF_K,
        tenant_id=tenant_id or settings.TENANT_ID,
    )
    reranker = Reranker()
    llm = CloudflareLLM(settings.CLOUDFLARE_ACCOUNT_ID, settings.CLOUDFLARE_API_TOKEN, settings.CLOUDFLARE_MODEL)
    return embedder, store, retriever, reranker, llm


def _enrich_chunks_with_tenant(chunks, tenant_id: str | None):
    tid = tenant_id or settings.TENANT_ID
    for c in chunks:
        c.metadata.setdefault("tenant_id", tid)
    return chunks


def ingest_data_dir(data_dir: str | None = None, tenant_id: str | None = None) -> dict:
    data_dir = data_dir or settings.DATA_DIR
    embedder, store, retriever, _, _ = build_stack(tenant_id=tenant_id)
    docs = load_documents(data_dir)
    if not docs:
        return {"docs": 0, "chunks": 0, "collection": settings.QDRANT_COLLECTION}
    chunks = split_documents(docs, chunk_size=settings.CHUNK_SIZE, chunk_overlap=settings.CHUNK_OVERLAP)
    chunks = _enrich_chunks_with_tenant(chunks, tenant_id)
    vecs = embedder.embed([c.text for c in chunks])
    n = store.upsert(vecs, chunks)
    retriever.rebuild()
    return {"docs": len(docs), "chunks": n, "collection": settings.QDRANT_COLLECTION,
            "embed_mode": embedder.mode, "total_points": store.count(), "tenant_id": retriever.tenant_id}


def query(question: str, top_k_final: int | None = None, tenant_id: str | None = None) -> dict:
    _, store, retriever, reranker, llm = build_stack(tenant_id=tenant_id)
    top_k_final = top_k_final or settings.TOP_K_FINAL
    candidates = retriever.retrieve(question, tenant_id=retriever.tenant_id)
    reranked = reranker.rerank(question, candidates, top_k=top_k_final)
    context, used = assemble(reranked)
    if not used:
        return {"answer": "Không tìm thấy bằng chứng liên quan trong knowledge base. Tôi không thể trả lời chắc chắn.",
                "citations": [], "has_evidence": False, "candidates": [],
                "llm_mode": llm.mode, "rerank_mode": reranker.mode}
    # §8.3 missing evidence: threshold on dense score of top-1
    top_dense = max([c.get("dense_score", 0) for c in used], default=0)
    has_evidence = top_dense >= settings.SIMILARITY_THRESHOLD or llm.mode == "mock" and len(used) > 0
    # In mock mode keep evidence True if we retrieved anything (demo-friendly)
    if llm.mode == "mock" and used:
        has_evidence = True
    messages = build_messages(question, context)
    answer = llm.chat(messages)
    citations = [
        {"tag": c.get("cite_tag", f"[S{i+1}]"), "chunk_id": c["chunk_id"],
         "filename": c["metadata"].get("filename", ""), "page": c["metadata"].get("page", ""),
         "section": c["metadata"].get("section", ""),
         "dense_score": c.get("dense_score", 0.0), "fused_score": c.get("fused_score", 0.0),
         "rerank_score": c.get("rerank_score", 0.0),
         "text": c["text"][:600]}
        for i, c in enumerate(used)
    ]
    return {"answer": answer, "citations": citations, "has_evidence": has_evidence,
            "candidates": used, "llm_mode": llm.mode, "rerank_mode": reranker.mode}
