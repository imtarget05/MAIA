"""Chunking (§4, failure mode §8.4): SentenceSplitter with overlap.

Uses LlamaIndex SentenceSplitter when available, else sliding-window
fallback that respects paragraph boundaries.
"""
from dataclasses import dataclass


@dataclass
class Chunk:
    text: str
    metadata: dict


def split_documents(docs, chunk_size: int = 512, chunk_overlap: int = 50,
                    strategy: str = "sentence") -> list[Chunk]:
    """Split docs into chunks.

    strategy="sentence" (default): LlamaIndex SentenceSplitter, else word-window
    fallback. strategy="paragraph": paragraph-aware grouping (blank-line
    boundaries, merged to ~chunk_size with overlap) — pure stdlib, useful when
    LlamaIndex is absent or headings must stay intact. See docs/chunking-benchmark.md.
    """
    if strategy == "paragraph":
        return _paragraph_split(docs, chunk_size, chunk_overlap)
    # Try LlamaIndex splitter
    try:
        from llama_index.core.node_parser import SentenceSplitter

        splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        chunks: list[Chunk] = []
        for doc in docs:
            text = doc.text if hasattr(doc, "text") else doc["text"]
            if not text or not text.strip():
                continue
            meta = doc.metadata if hasattr(doc, "metadata") else doc.get("metadata", {})
            nodes = splitter.get_nodes_from_documents(
                [_dict_to_llama_doc(text, meta)]
            )
            for i, n in enumerate(nodes):
                txt = getattr(n, "text", "")
                if not txt or not txt.strip():
                    continue
                m = dict(meta)
                m["chunk_id"] = f"{m.get('doc_id', 'doc')}_{i}"
                chunks.append(Chunk(text=txt, metadata=m))
        return chunks
    except Exception:
        pass
    # Fallback splitter
    return _fallback_split(docs, chunk_size, chunk_overlap)


def _dict_to_llama_doc(text, meta):
    from llama_index.core import Document

    return Document(text=text, metadata=dict(meta))


def _paragraph_split(docs, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    """Group blank-line-separated paragraphs into ~chunk_size-char chunks.

    Paragraph boundaries are never cut; overlap is done at paragraph granularity
    (last ~overlap chars worth of paragraphs are prepended to the next chunk).
    """
    import re

    chunks: list[Chunk] = []
    for doc in docs:
        text = doc.text if hasattr(doc, "text") else doc["text"]
        meta = doc.metadata if hasattr(doc, "metadata") else doc.get("metadata", {})
        paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
        if not paras:
            continue
        buf: list[str] = []
        buf_len = 0
        idx = 0

        def _flush() -> None:
            nonlocal buf, buf_len, idx
            if not buf:
                return
            m = dict(meta)
            m["chunk_id"] = f"{m.get('doc_id', 'doc')}_{idx}"
            m["chunk_strategy"] = "paragraph"
            chunks.append(Chunk(text="\n\n".join(buf), metadata=m))
            idx += 1

        for p in paras:
            if buf and buf_len + len(p) + 2 > chunk_size:
                _flush()
                # overlap: keep trailing paragraphs worth ~chunk_overlap chars
                if chunk_overlap > 0:
                    keep: list[str] = []
                    keep_len = 0
                    for q in reversed(buf):
                        keep.append(q)
                        keep_len += len(q) + 2
                        if keep_len >= chunk_overlap:
                            break
                    buf = list(reversed(keep))
                    buf_len = sum(len(q) + 2 for q in buf)
                else:
                    buf, buf_len = [], 0
            buf.append(p)
            buf_len += len(p) + 2
        _flush()
    return chunks


def _fallback_split(docs, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    """char-based sliding window on words, tries to keep paragraphs together."""
    chunks: list[Chunk] = []
    for doc in docs:
        text = doc.text if hasattr(doc, "text") else doc["text"]
        meta = doc.metadata if hasattr(doc, "metadata") else doc.get("metadata", {})
        words = text.split()
        if not words:
            continue
        # approx: chunk_size chars -> words (~5 chars/word)
        win = max(50, chunk_size // 5)
        ov = max(0, chunk_overlap // 5)
        step = max(1, win - ov)
        idx = 0
        for start in range(0, len(words), step):
            piece = " ".join(words[start : start + win])
            if not piece.strip():
                continue
            m = dict(meta)
            m["chunk_id"] = f"{m.get('doc_id', 'doc')}_{idx}"
            chunks.append(Chunk(text=piece, metadata=m))
            idx += 1
            if start + win >= len(words):
                break
    return chunks
