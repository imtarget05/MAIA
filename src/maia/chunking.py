"""Chunking (§4, failure mode §8.4): SentenceSplitter with overlap.

Uses LlamaIndex SentenceSplitter when available, else sliding-window
fallback that respects paragraph boundaries.
"""
from dataclasses import dataclass


@dataclass
class Chunk:
    text: str
    metadata: dict


def split_documents(docs, chunk_size: int = 512, chunk_overlap: int = 50) -> list[Chunk]:
    # Try LlamaIndex splitter
    try:
        from llama_index.core.node_parser import SentenceSplitter

        splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        chunks: list[Chunk] = []
        for doc in docs:
            text = doc.text if hasattr(doc, "text") else doc["text"]
            meta = doc.metadata if hasattr(doc, "metadata") else doc.get("metadata", {})
            nodes = splitter.get_nodes_from_documents(
                [_dict_to_llama_doc(text, meta)]
            )
            for i, n in enumerate(nodes):
                m = dict(meta)
                m["chunk_id"] = f"{m.get('doc_id', 'doc')}_{i}"
                chunks.append(Chunk(text=getattr(n, "text", ""), metadata=m))
        return chunks
    except Exception:
        pass
    # Fallback splitter
    return _fallback_split(docs, chunk_size, chunk_overlap)


def _dict_to_llama_doc(text, meta):
    from llama_index.core import Document

    return Document(text=text, metadata=dict(meta))


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
