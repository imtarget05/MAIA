"""Context assembly (§5, failure mode §8.2): cap tokens, dedup, cite markers."""
MAX_CONTEXT_CHARS = 3000


def assemble(candidates: list[dict], max_chars: int = MAX_CONTEXT_CHARS) -> tuple[str, list[dict]]:
    seen = set()
    parts: list[str] = []
    used: list[dict] = []
    total = 0
    for i, c in enumerate(candidates):
        key = c["chunk_id"] or c["text"][:80]
        if key in seen:
            continue
        seen.add(key)
        tag = f"[S{i+1}]"
        chunk_txt = c["text"].strip()
        block = f"{tag} (file={c['metadata'].get('filename','?')}, page={c['metadata'].get('page','?')}, score={c.get('rerank_score', c.get('fused_score', 0.0)):.3f})\n{chunk_txt}"
        if total + len(block) > max_chars and used:
            break
        parts.append(block)
        used.append({**c, "cite_tag": tag})
        total += len(block)
    return "\n\n---\n\n".join(parts), used


SYSTEM_PROMPT = (
    "You are MAIA, a grounded RAG assistant. Answer ONLY from the provided context.\n"
    "Rules: 1) Every factual claim must cite its source like [S1], [S2]. "
    "2) If the context lacks the answer, say so explicitly and do not invent facts. "
    "3) Be concise, then list 'Sources:' with the tags you used."
)


def build_messages(question: str, context: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}\nAnswer with citations [S1], [S2]..."},
    ]
