"""Ingestion pipeline: chunk -> sanitize -> pii-scan -> enrich -> embed -> upsert.

Separated from pipeline_query.py to isolate ingestion concerns from retrieval concerns.
"""
from .chunking import split_documents
from .config import settings
from .ingestion import fetch_url, load_documents
from .pipeline_wiring import _doc_sanitizer, _pii_scanner


def _sanitize_chunks(chunks):
    dirty = 0
    for c in chunks:
        cleaned, was_dirty = _doc_sanitizer.sanitize(c.text)
        if was_dirty:
            c.text = cleaned
            c.metadata["injection_sanitized"] = True
            dirty += 1
    return chunks, dirty


def _pii_scan_chunks(chunks):
    dirty = 0
    type_counts: dict[str, int] = {}
    for c in chunks:
        if _pii_scanner.is_dirty(c.text):
            hits = _pii_scanner.detect(c.text)
            for h in hits:
                type_counts[h.pii_type] = type_counts.get(h.pii_type, 0) + 1
            c.text = _pii_scanner.redact(c.text)
            c.metadata["pii_redacted"] = True
            c.metadata["pii_types"] = sorted({h.pii_type for h in hits})
            dirty += 1
    return chunks, dirty, type_counts


def _enrich_chunks_with_tenant(chunks, tenant_id: str | None):
    tid = tenant_id or settings.TENANT_ID
    for c in chunks:
        c.metadata.setdefault("tenant_id", tid)
    return chunks


def _ingest_rawdocs(docs, tenant_id: str | None, session_id: str = "",
                    stack: tuple | None = None) -> dict:
    """Shared tail of every ingest path: chunk -> sanitize -> pii-scan -> enrich -> embed -> upsert."""
    tid = tenant_id or settings.TENANT_ID
    if stack is None:
        from .pipeline_query import build_stack
        stack = build_stack(tenant_id=tenant_id)
    parts = stack
    embedder, store, retriever = parts[0], parts[1], parts[2]
    chunks = split_documents(docs, chunk_size=settings.CHUNK_SIZE, chunk_overlap=settings.CHUNK_OVERLAP)
    chunks, dirty = _sanitize_chunks(chunks)
    chunks, pii_dirty, pii_type_counts = _pii_scan_chunks(chunks)
    chunks = _enrich_chunks_with_tenant(chunks, tid)
    if session_id:
        for c in chunks:
            c.metadata["session_id"] = session_id
    vecs = embedder.embed([c.text for c in chunks])
    if hasattr(store, "upsert"):
        n = store.upsert(vecs, chunks)
    else:
        n = 0
        for c, v in zip(chunks, vecs):
            payload = dict(c.metadata)
            payload["text"] = c.text
            store.upsert_one(c.metadata.get("chunk_id", ""), v, payload)
            n += 1
    retriever.rebuild(tenant_id=tid)
    return {"docs": len(docs), "chunks": n, "collection": settings.QDRANT_COLLECTION,
            "embed_mode": embedder.mode, "total_points": store.count(),
            "tenant_id": retriever.tenant_id, "sanitized_chunks": dirty,
            "pii_redacted_chunks": pii_dirty, "pii_type_counts": pii_type_counts}


def ingest_data_dir(data_dir: str | None = None, tenant_id: str | None = None) -> dict:
    data_dir = data_dir or settings.DATA_DIR
    docs = load_documents(data_dir)
    if not docs:
        return {"docs": 0, "chunks": 0, "collection": settings.QDRANT_COLLECTION}
    return _ingest_rawdocs(docs, tenant_id)


def ingest_url(url: str, tenant_id: str | None = None, session_id: str = "",
               stack: tuple | None = None) -> dict:
    tid = tenant_id or settings.TENANT_ID
    sid = session_id or ""
    if sid:
        existing = [s for s in list_sources(sid, tid, stack=stack)]
        if len(existing) >= settings.URL_MAX_LINKS_PER_SESSION:
            raise ValueError(f"session already has {len(existing)} links "
                             f"(max {settings.URL_MAX_LINKS_PER_SESSION})")
    doc = fetch_url(url, session_id=sid)
    res = _ingest_rawdocs([doc], tid, session_id=sid, stack=stack)
    res.update({"url": doc.metadata["source"], "title": doc.metadata["filename"],
                "doc_id": doc.metadata["doc_id"], "session_id": sid})
    return res


def list_sources(session_id: str, tenant_id: str | None = None,
                 stack: tuple | None = None) -> list[dict]:
    tid = tenant_id or settings.TENANT_ID
    if stack is None:
        from .pipeline_query import build_stack
        stack = build_stack(tenant_id=tenant_id)
    parts = stack
    store = parts[1]
    try:
        corpus = store.scroll_all(tenant_id=tid)
    except TypeError:
        corpus = store.scroll_all()
    except Exception:
        return []
    docs: dict[str, dict] = {}
    for c in corpus:
        meta = c.get("metadata", {})
        if meta.get("origin") != "url" or (meta.get("session_id") or "") != session_id:
            continue
        d = docs.setdefault(meta.get("doc_id", ""), {
            "doc_id": meta.get("doc_id", ""), "url": meta.get("source", ""),
            "title": meta.get("filename", ""), "chunks": 0,
            "added_ts": meta.get("timestamp", "0")})
        d["chunks"] += 1
        if str(meta.get("timestamp", "0")) > str(d["added_ts"]):
            d["added_ts"] = meta.get("timestamp", "0")
    return sorted(docs.values(), key=lambda d: str(d["added_ts"]), reverse=True)


def delete_source(doc_id: str, session_id: str, tenant_id: str | None = None,
                  stack: tuple | None = None) -> dict:
    owned = [s["doc_id"] for s in list_sources(session_id, tenant_id, stack=stack)]
    if doc_id not in owned:
        return {"ok": False, "error": "source not found in this session"}
    if stack is None:
        from .pipeline_query import build_stack
        stack = build_stack(tenant_id=tenant_id)
    parts = stack
    store, retriever = parts[1], parts[2]
    store.delete_by_doc(doc_id)
    tid = tenant_id or settings.TENANT_ID
    retriever.rebuild(tenant_id=tid)
    return {"ok": True, "doc_id": doc_id}
