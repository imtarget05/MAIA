"""Evaluation must measure CANONICAL retrieved evidence, not the 600-char
presentation excerpt in citations[].text.

Bug: eval._eval_row joined ``c["text"]`` from citations, which pipeline_query
truncates to 600 chars for display. A gold string past char 600 (EAP contact at
offset 919) was scored as missing, driving contact_context_rate 1.000 -> 0.833
and silently understating context_precision / faithfulness.

These tests pin both halves of the contract: the metric reads the full chunk,
and the 600-char presentation truncation stays exactly as it was.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import eval as eval_mod
from maia import pipeline_query
from maia.prompt import assemble

CITATION_TEXT_LIMIT = 600


def _chunk(chunk_id="c1", text=""):
    return {"chunk_id": chunk_id, "text": text, "metadata": {"filename": "hr.md"},
            "dense_score": 0.9, "fused_score": 0.9}


def _presented(chunk, limit=CITATION_TEXT_LIMIT):
    """Mirror pipeline_query's response shape: excerpt citation + full candidates."""
    return {"answer": "x", "has_evidence": True, "candidates": [chunk],
            "citations": [{"chunk_id": chunk["chunk_id"],
                           "text": chunk["text"][:limit]}]}


def test_contact_beyond_char_600_scores_in_ctx_while_citation_stays_truncated():
    """Regression: contact after char 600 -> contact_in_ctx == 1, citation still 600."""
    contact = "eap@company.com"
    body = "Nội quy nhân sự rất dài. " + ("A" * 900) + " " + contact + " — Liên hệ EAP."
    assert body.index(contact) > CITATION_TEXT_LIMIT  # the pre-fix trigger condition
    chunk = _chunk(text=body)

    res = _presented(chunk)
    # presentation side is unchanged
    assert len(res["citations"][0]["text"]) == CITATION_TEXT_LIMIT
    assert contact not in res["citations"][0]["text"]
    # candidates stay canonical (full text) for the evaluator
    assert res["candidates"][0]["text"] == body

    row = {"question": "Who do I contact about EAP?", "gold_chunk_ids": ["c1"],
           "gold_keywords": [], "expect_contact": contact}
    m = eval_mod._eval_row(row, res, top_k=3)
    assert m["hit"] == 1
    assert m["contact_in_ctx"] == 1

    # the same chunk measured the old way (presentation text only) would have failed
    legacy = {"answer": "x", "has_evidence": True,
              "citations": res["citations"], "candidates": []}
    assert eval_mod._eval_row(row, legacy, top_k=3)["contact_in_ctx"] == 0


def test_keyword_precision_independent_of_keyword_position_in_chunk():
    """gold_keywords past char 600 count as present; position must not matter."""
    kw = ["EAP", "bảo mật"]
    early = _chunk("c1", "EAP và bảo mật thông tin. " + ("B" * 900))
    late = _chunk("c1", "Nội quy dài. " + ("B" * 900) + " Cuối tài liệu: EAP và bảo mật.")
    assert late["text"].index("EAP") > CITATION_TEXT_LIMIT

    row = {"question": "q?", "gold_chunk_ids": ["c1"], "gold_keywords": kw}
    assert eval_mod._eval_row(row, _presented(early), top_k=3)["prec"] == 1.0
    assert eval_mod._eval_row(row, _presented(late), top_k=3)["prec"] == 1.0

    # a genuinely absent keyword still scores 0.5 (metric meaning unchanged)
    absent = _chunk("c1", "Nội quy dài. " + ("B" * 900) + " Cuối tài liệu: EAP.")
    assert eval_mod._eval_row(row, _presented(absent), top_k=3)["prec"] == 0.5


def test_canonical_texts_falls_back_to_citation_text_and_preserves_order():
    chunk_a, chunk_b = _chunk("a", "chunk A full text"), _chunk("b", "chunk B full text")
    res = {"citations": [{"chunk_id": "b", "text": "B"}, {"chunk_id": "a", "text": "A"}],
           "candidates": [chunk_a, chunk_b]}
    # order follows citations, text comes from candidates
    assert eval_mod._canonical_texts(res) == ["chunk B full text", "chunk A full text"]
    # no candidates -> citation text
    assert eval_mod._canonical_texts({"citations": res["citations"]}) == ["B", "A"]
    # unknown chunk_id -> citation text
    assert eval_mod._canonical_texts(
        {"citations": [{"chunk_id": "zz", "text": "fallback"}], "candidates": [chunk_a]}) == ["fallback"]
    # missing / malformed candidates never raise
    assert eval_mod._canonical_texts({}) == []
    assert eval_mod._canonical_texts({"citations": [{"chunk_id": "a"}], "candidates": [None, 3]}) == [""]


def test_pipeline_assemble_and_projection_keep_600_truncation_and_full_candidates():
    """On the real assemble() output: presentation truncated, evidence canonical."""
    long_text = "X" * 1200 + " hr@company.com"
    context, used = assemble([_chunk("c1", long_text)])
    assert context
    citations = [{"chunk_id": c["chunk_id"], "text": c["text"][:CITATION_TEXT_LIMIT]}
                 for c in used]
    assert len(citations[0]["text"]) == CITATION_TEXT_LIMIT
    assert "hr@company.com" not in citations[0]["text"]
    assert used[0]["text"] == long_text
    row = {"question": "q?", "gold_chunk_ids": ["c1"], "gold_keywords": [],
           "expect_contact": "hr@company.com"}
    m = eval_mod._eval_row(row, {"answer": "a", "has_evidence": True,
                                 "citations": citations, "candidates": used}, top_k=3)
    assert m["contact_in_ctx"] == 1


def test_pipeline_query_citation_projection_is_still_600_chars():
    """Pin the presentation contract in pipeline_query itself."""
    src = Path(pipeline_query.__file__).read_text()
    assert '"text": c["text"][:600]' in src
