"""Source reader: show exactly which passage the AI cited + full document.

A Citation only carries a <=600-char excerpt. To let users (1) see the exact
passage the AI read (highlighted zone) and (2) read the whole source document
with that zone still highlighted, we reassemble the full document from the
vector store: all sibling chunks sharing the same doc_id, ordered by chunk
index, with the cited chunk(s) marked.

All grouping/ordering/highlight logic in build_source_view() is pure (offline
testable). Only fetch_source_view() touches Qdrant.
"""
from __future__ import annotations

import html
import re

MAX_CHUNKS = 200
MAX_CHARS = 60000

_CHUNK_SUFFIX_RE = re.compile(r"_(\d+)$")


def _chunk_index(chunk_id: str) -> int:
    m = _CHUNK_SUFFIX_RE.search(chunk_id or "")
    return int(m.group(1)) if m else 0


def _doc_key(meta: dict, chunk_id: str, filename: str) -> str:
    """Group sibling chunks: prefer stable doc ids, fall back to filename."""
    for k in ("doc_id", "document_id"):
        v = (meta or {}).get(k)
        if v:
            return f"doc:{v}"
    base = (chunk_id or "").rsplit("_", 1)[0] if "_" in (chunk_id or "") else ""
    if base:
        return f"base:{base}"
    return f"file:{filename or '?'}"


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def locate_excerpt(full_chunk_text: str, excerpt: str, window: int = 40) -> tuple[int, int]:
    """Find excerpt inside its full chunk text → (start, end) offsets.

    The citation excerpt is truncated (<=600 chars); match on its head so the
    highlight anchors even when the stored chunk is longer. Falls back to the
    whole chunk when no anchor is found.
    """
    full = full_chunk_text or ""
    head = _normalize(excerpt)[:120]
    if head:
        # try progressively shorter heads (punctuation/whitespace drift)
        for ln in (120, 80, 40):
            key = head[:ln].strip()
            if len(key) >= 20:
                norm_full = _normalize(full)
                pos = norm_full.find(key)
                if pos >= 0:
                    # map back approximately: highlight the matched span
                    # expanded by `window` chars of context on each side
                    start = max(0, pos - window)
                    end = min(len(norm_full), pos + len(key) + window)
                    return start, end
    return 0, len(full)


def build_source_view(cited: dict, corpus: list[dict]) -> dict:
    """Assemble the full source document around a cited chunk (pure).

    cited:  {chunk_id, filename, text} (citation excerpt)
    corpus: [{chunk_id, text, metadata}] (e.g. store.scroll_all())
    Returns {filename, doc_id, chunks:[{chunk_id, text, cited}],
             cited_text, full_text, total_chunks, truncated}.
    """
    chunk_id = cited.get("chunk_id", "")
    filename = cited.get("filename", "")
    excerpt = cited.get("text", "")

    ref = next((c for c in corpus if c.get("chunk_id") == chunk_id), None)
    if ref is None and filename:
        ref = next(
            (c for c in corpus if (c.get("metadata") or {}).get("filename") == filename),
            None,
        )
    if ref is None:
        # Store unreachable / web chunk: single-passage view.
        return {
            "filename": filename or "?",
            "doc_id": "",
            "chunks": [{"chunk_id": chunk_id, "text": excerpt, "cited": True}],
            "cited_text": excerpt,
            "full_text": excerpt,
            "total_chunks": 1,
            "truncated": False,
        }

    ref_meta = ref.get("metadata") or {}
    key = _doc_key(ref_meta, ref.get("chunk_id", ""), filename)
    doc_id = str(ref_meta.get("doc_id", ref_meta.get("document_id", "")))
    siblings = [
        c for c in corpus
        if _doc_key(c.get("metadata") or {}, c.get("chunk_id", ""),
                    (c.get("metadata") or {}).get("filename", "")) == key
    ]
    siblings.sort(key=lambda c: (_chunk_index(c.get("chunk_id", "")), c.get("chunk_id", "")))
    siblings = siblings[:MAX_CHUNKS]

    chunks = [
        {"chunk_id": c.get("chunk_id", ""),
         "text": c.get("text", ""),
         "cited": c.get("chunk_id") == chunk_id}
        for c in siblings
    ]
    if not any(c["cited"] for c in chunks):
        chunks.append({"chunk_id": chunk_id, "text": excerpt, "cited": True})

    full_text = "\n\n".join(c["text"] for c in chunks)
    truncated = len(full_text) > MAX_CHARS
    if truncated:
        full_text = full_text[:MAX_CHARS] + "\n\n… (đã rút gọn, tài liệu còn tiếp)"
    cited_full = next((c["text"] for c in chunks if c["cited"]), excerpt)
    return {
        "filename": (ref_meta.get("filename") or filename or "?"),
        "doc_id": doc_id,
        "chunks": chunks,
        "cited_text": excerpt or cited_full,
        "full_text": full_text,
        "total_chunks": len(chunks),
        "truncated": truncated,
    }


def fetch_source_view(chunk_id: str, filename: str, excerpt: str = "",
                      tenant_id: str | None = None, stack=None) -> dict:
    """Load corpus from Qdrant once, then build the view (cached by caller)."""
    try:
        if stack is None:
            from .pipeline_query import build_stack
            stack = build_stack(tenant_id=tenant_id)
        store = stack[1]
        try:
            corpus = store.scroll_all(tenant_id=tenant_id)
        except TypeError:
            corpus = store.scroll_all()
    except Exception:
        corpus = []
    return build_source_view(
        {"chunk_id": chunk_id, "filename": filename, "text": excerpt},
        corpus,
    )


def render_source_html(view: dict) -> str:
    """Full-document HTML with the cited chunk(s) wrapped in <mark>.

    The cited zone keeps its highlight while the user reads the whole source.
    """
    parts: list[str] = []
    for c in view.get("chunks", []):
        safe = html.escape(c.get("text", "") or "")
        if c.get("cited"):
            parts.append(
                "<mark class='maia-cited-mark' id='maia-cited'>"
                f"{safe}</mark>"
            )
        else:
            parts.append(safe.replace("\n", "<br>"))
    body = "<br><br>".join(parts)
    return f"<div class='maia-source-full'>{body}</div>"


def render_excerpt_html(excerpt: str, tag: str = "", filename: str = "") -> str:
    """Highlighted 'zone' card: exactly the passage the AI read."""
    safe = html.escape((excerpt or "").strip() or "(trống)").replace("\n", "<br>")
    label = f"{tag} · {filename}".strip(" ·") if (tag or filename) else "Đoạn AI đã đọc"
    return (
        "<div class='maia-cited'>"
        f"<div class='maia-cited-label'>◉ Đoạn AI đã đọc — {html.escape(label)}</div>"
        f"<div class='maia-cited-text'>{safe}</div>"
        "</div>"
    )
