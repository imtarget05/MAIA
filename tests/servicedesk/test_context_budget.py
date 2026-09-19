"""T3 context-budget tests (adapted donor tests + the plan's hard boundaries).

Adapted from the project-1 donor at the pinned SHA
(agent/tests/test_context_trim.py), with the project-1-only cases (memory
summarizer, qdrant tool) removed and the two donor bugs re-asserted as fixed.
"""
from __future__ import annotations

from maia.servicedesk.knowledge.context_budget import (
    MAX_CHARS,
    TRUNCATE_SUFFIX,
    build_evidence_context,
    cap_chunks,
    trim_messages,
    truncate,
)


def _msgs(n: int, pad: int = 500) -> list[dict]:
    msgs = [{"role": "system", "content": "S" * 200}]
    for i in range(n):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "c" * pad})
    return msgs


def test_trim_messages_keeps_12_and_char_budget():
    original = _msgs(40)
    out = trim_messages(original)

    assert len(out) <= 12
    assert out[0]["role"] == "system", "the leading system message must survive"
    assert sum(len(m["content"]) for m in out) <= MAX_CHARS
    assert out[-1]["content"] == "c" * 500
    # donor contract: the input is never mutated
    assert len(original) == 41
    assert original[-1]["content"] == "c" * 500


def test_trim_messages_giant_single_message_stays_in_budget():
    out = trim_messages([{"role": "user", "content": "x" * 20000}])
    total = sum(len(m["content"]) for m in out)
    # Fix 1: suffix counts inside the budget (donor exceeded it by 14 chars).
    assert total <= MAX_CHARS
    assert out[0]["content"].endswith(TRUNCATE_SUFFIX)


def test_trim_messages_caps_giant_system_message():
    """Fix 2: the donor never capped the system message, so a huge system
    prompt left the budget permanently exceeded."""
    out = trim_messages(
        [{"role": "system", "content": "S" * 20000}, {"role": "user", "content": "hi"}]
    )
    total = sum(len(m["content"]) for m in out)
    assert total <= MAX_CHARS, f"budget exceeded: {total} > {MAX_CHARS}"
    assert out[0]["role"] == "system"


def test_truncate_result_never_exceeds_max_chars():
    for limit in (14, 15, 50, 1000):
        result = truncate("q" * 5000, limit)
        assert len(result) <= limit, f"limit={limit} produced {len(result)} chars"


def test_truncate_leaves_short_text_untouched():
    assert truncate("short", 100) == "short"
    assert truncate("", 10) == ""


def test_cap_chunks_top5_and_1200_chars():
    chunks = [
        {"text": "t" * 2000, "document_name": f"d{i}", "score": 0.9} for i in range(8)
    ]
    out = cap_chunks(chunks)
    assert len(out) == 5
    for chunk in out:
        assert len(chunk["text"]) <= 1200
    assert len(chunks[0]["text"]) == 2000, "input chunks must not be mutated"


def test_build_evidence_context_empty_returns_no_citations():
    """No usable evidence must yield ('', []) so the caller reports
    insufficient_evidence instead of inventing a runbook (S6)."""
    assert build_evidence_context([]) == ("", [])
    assert build_evidence_context([{"text": "   "}]) == ("", [])


def test_build_evidence_context_builds_citations_with_identity():
    from uuid import uuid4

    doc_id = uuid4()
    chunks = [
        {
            "chunk_id": "vpn_vpn-vpn_1_v1_0",
            "document_id": doc_id,
            "document_version": 3,
            "title": "VPN runbook",
            "section": "Error 809",
            "page": 2,
            "text": "Check the firewall profile and re-import the certificate.",
        }
    ]
    block, citations = build_evidence_context(chunks)

    assert block.startswith("[1] VPN runbook — Error 809 (p.2)")
    assert "firewall profile" in block
    assert len(citations) == 1
    citation = citations[0]
    assert citation.document_id == doc_id
    assert citation.document_version == 3
    assert citation.page == 2
    assert citation.excerpt in block


def test_build_evidence_context_respects_budget():
    from uuid import uuid4

    chunks = [
        {
            "chunk_id": f"c{i}",
            "document_id": uuid4(),
            "document_version": 1,
            "title": f"doc {i}",
            "text": "z" * 5000,
        }
        for i in range(10)
    ]
    block, citations = build_evidence_context(chunks, max_chars=3000)
    assert len(block) <= 3000
    assert len(citations) <= 5