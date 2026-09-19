"""Context budget helpers for MAIA Service Desk (T3).

PROVENANCE — copied from the project-1 donor (MIT, see THIRD_PARTY_NOTICES.md):
  repo:   https://github.com/imtarget05/Smart-Document-Chatbot
  commit: babe1c5badcf61f2a3059a5293cb059bdd2c49fd
  source: agent/memory/context_trim.py
  donor sha256 (at copy time): b1baf5466d7244ab6b0d58df27937acbe929c9b795c7e61f999e3a82845c78c4
Documented modifications (plan T3):
  1. ``truncate`` now counts ``TRUNCATE_SUFFIX`` INSIDE the budget, so a
     capped string never exceeds ``max_chars`` (donor returned
     ``max_chars + len(suffix)``).
  2. ``trim_messages`` caps the leading system message too; in the donor an
     over-long system message passed through uncapped and blew the budget.
  3. ``build_evidence_context`` added: builds the prompt block + Citation
     list from authorized chunks (T4/T5 consumers).
  4. Inputs are never mutated (defensive copies).

Note: character caps are NOT a tokenizer guarantee — the LLM provider must
still reserve output tokens (plan T3).
"""
from __future__ import annotations

from typing import Any, Dict, List

MAX_MSGS = 12
MAX_CHARS = 8000
TOP_K = 5
CHUNK_CHARS = 1200
SHORT_TERM_ENTRY_CHARS = 1000
TRUNCATE_SUFFIX = "...[truncated]"


def truncate(text: str, max_chars: int) -> str:
    """Truncate so the RESULT length is <= max_chars, suffix included (fix 1)."""
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATE_SUFFIX):
        return text[:max_chars]
    return text[: max_chars - len(TRUNCATE_SUFFIX)] + TRUNCATE_SUFFIX


def _as_message_dict(m: Any) -> Dict[str, Any]:
    """Normalize BaseMessage / dict -> {role, content} (không mutate gốc)."""
    if isinstance(m, dict):
        role = m.get("role", m.get("type", "user"))
        content = m.get("content", "")
        return {"role": str(role), "content": content if isinstance(content, str) else str(content)}
    # langchain BaseMessage-like
    role = getattr(m, "type", None) or getattr(m, "role", "user")
    content = getattr(m, "content", "")
    return {"role": str(role), "content": content if isinstance(content, str) else str(content)}


def trim_messages(
    messages: List[Any],
    max_msgs: int = MAX_MSGS,
    max_chars: int = MAX_CHARS,
) -> List[Dict[str, Any]]:
    """Giữ `max_msgs` messages gần nhất + system đầu nếu có; enforce char budget.

    Vượt budget → bỏ message cũ nhất trước; message đơn lẻ quá dài → truncate.
    """
    if not messages:
        return []

    norm = [_as_message_dict(m) for m in messages]

    head = None
    if str(norm[0].get("role", "")).lower() == "system":
        head = norm[0]
        pool = norm[1:]
    else:
        pool = norm

    keep_n = max(1, max_msgs - (1 if head is not None else 0))
    pool = pool[-keep_n:]

    # Fix 2: cap the system message as well. The donor counted head content
    # in _total but only truncated pool[-1], so a huge system message left the
    # budget permanently exceeded. Reserve at most half the budget for the
    # system block when there is conversation to keep.
    if head is not None:
        head_budget = max(1, max_chars // 2) if pool else max_chars
        if len(head["content"]) > head_budget:
            head = {**head, "content": truncate(head["content"], head_budget)}

    # Enforce char budget: cắt từ đầu (giữ cuối).
    def _total(ps: List[Dict[str, Any]]) -> int:
        return sum(len(p["content"]) for p in ps) + (len(head["content"]) if head else 0)

    while len(pool) > 1 and _total(pool) > max_chars:
        pool.pop(0)

    # Message đơn lẻ vẫn vượt budget → truncate content của nó (defensive copy).
    if _total(pool) > max_chars and pool:
        remaining = max(1, max_chars - (len(head["content"]) if head else 0))
        pool[-1] = {**pool[-1], "content": truncate(pool[-1]["content"], remaining)}

    return ([head] if head is not None else []) + pool


def cap_chunks(
    chunks: List[Dict[str, Any]],
    top_k: int = TOP_K,
    max_chars: int = CHUNK_CHARS,
) -> List[Dict[str, Any]]:
    """Chỉ lấy top `top_k` chunks; truncate `text` ≤ max_chars (không mutate gốc)."""
    if not chunks:
        return []
    out = []
    for c in chunks[:top_k]:
        c2 = dict(c)
        if "text" in c2 and isinstance(c2["text"], str):
            c2["text"] = truncate(c2["text"], max_chars)
        out.append(c2)
    return out


def build_evidence_context(
    chunks: List[Dict[str, Any]],
    max_chars: int = MAX_CHARS,
    top_k: int = TOP_K,
) -> tuple[str, List["Citation"]]:
    """Build the prompt evidence block + Citation list from ALLOWED chunks.

    Callers MUST pass only chunks that already passed ACL/version checks
    (plan S6: filtering happens before text reaches the prompt). Each returned
    citation carries the immutable source identity (document id + version +
    chunk id) so the source endpoint can re-authorize on open.

    Returns ``("", [])`` when there is no usable evidence — the caller then
    reports ``insufficient_evidence`` rather than inventing a runbook.
    """
    from maia.servicedesk.schemas import Citation

    usable = [
        c for c in chunks
        if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()
    ][:top_k]
    if not usable:
        return "", []

    citations: List[Citation] = []
    lines: List[str] = []
    remaining = max(0, max_chars)
    for index, chunk in enumerate(usable, start=1):
        label = f"[{index}] {chunk.get('title') or 'Untitled'}"
        section = chunk.get("section")
        if section:
            label += f" — {section}"
        page = chunk.get("page")
        if page is not None:
            label += f" (p.{page})"

        header = f"{label}\n"
        # Keep the header even in a tight budget; the body absorbs the cut.
        body_budget = max(0, remaining - len(header))
        if body_budget <= 0:
            break
        body = truncate(chunk["text"].strip(), body_budget)
        block = header + body
        lines.append(block)
        remaining -= len(block) + 1  # +1 for the joining newline

        citations.append(
            Citation(
                chunk_id=str(chunk.get("chunk_id") or chunk.get("id") or index),
                document_id=chunk["document_id"],
                document_version=int(chunk.get("document_version", 1)),
                title=str(chunk.get("title") or "Untitled"),
                section=section if isinstance(section, str) else None,
                page=int(page) if isinstance(page, int) else None,
                excerpt=body,
            )
        )
        if remaining <= 0:
            break

    return "\n".join(lines), citations
