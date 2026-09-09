"""G-04-FU2 CI gate: contact_context_rate threshold logic (offline, no Qdrant)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import eval as eval_mod


def _row(q="Cần hỗ trợ VPN thì liên hệ ai?", contact="it-help@company.com"):
    return {"question": q, "gold_chunk_ids": [], "gold_keywords": [],
            "expect_contact": contact}


def _res(answer, texts):
    return {"answer": answer, "has_evidence": True,
            "citations": [{"chunk_id": f"c{i}", "text": t} for i, t in enumerate(texts)]}


def test_eval_row_contact_in_ctx_flag():
    hit = eval_mod._eval_row(
        _row(), _res("Liên hệ it-help@company.com.", ["Hỗ trợ VPN: it-help@company.com"]), top_k=3)
    assert hit["contact_in_ctx"] == 1
    assert hit["contact_usable"] == 1
    # redacted in context AND answer -> both 0
    lost = eval_mod._eval_row(
        _row(), _res("Liên hệ [PII-EMAIL].", ["Hỗ trợ VPN: [PII-EMAIL]"]), top_k=3)
    assert lost["contact_in_ctx"] == 0
    assert lost["contact_usable"] == 0
    # no expect_contact -> no signal (None, not 0)
    plain = eval_mod._eval_row(
        {"question": "Nghỉ phép?", "gold_chunk_ids": [], "gold_keywords": []},
        _res("12 ngày.", ["12 ngày."]), top_k=3)
    assert plain["contact_in_ctx"] is None


def test_eval_row_contact_in_ctx_conditional_on_hit():
    # retrieval miss (gold ids, no overlap) -> no redact signal (None),
    # but end-to-end usable stays 0 (honest).
    miss_row = {"question": "Who do I contact about benefits?",
                "gold_chunk_ids": ["gold1"], "gold_keywords": [],
                "expect_contact": "benefits@company.com"}
    miss = eval_mod._eval_row(
        miss_row, _res("Liên hệ [PII-EMAIL].", ["HR policy chung chung."]), top_k=3)
    assert miss["hit"] == 0
    assert miss["contact_in_ctx"] is None
    assert miss["contact_usable"] == 0


def test_contact_context_rate_decoupled_from_recall(monkeypatch, tmp_path):
    """Conditional aggregate: 1 retrieved+survived, 1 retrieved+redacted,
    1 missed -> contact_context_rate 1/2, contact_usability_rate 1/3."""
    import json

    rows = [
        {"question": "q1", "gold_chunk_ids": ["g1"], "gold_keywords": [],
         "expect_contact": "it-help@company.com"},
        {"question": "q2", "gold_chunk_ids": ["g2"], "gold_keywords": [],
         "expect_contact": "hr@company.com"},
        {"question": "q3", "gold_chunk_ids": ["g3"], "gold_keywords": [],
         "expect_contact": "benefits@company.com"},
    ]
    ds = tmp_path / "cond.jsonl"
    ds.write_text("\n".join(json.dumps(r) for r in rows))

    def fake_query(question, top_k_final=None, tenant_id=None, session_id=None):
        if question == "q1":
            return {"answer": "Liên hệ it-help@company.com.",
                    "citations": [{"chunk_id": "g1", "text": "VPN: it-help@company.com"}],
                    "has_evidence": True}
        if question == "q2":
            return {"answer": "Liên hệ [PII-EMAIL].",
                    "citations": [{"chunk_id": "g2", "text": "HR: [PII-EMAIL]"}],
                    "has_evidence": True}
        return {"answer": "Không tìm thấy.", "citations": [],
                "has_evidence": False}

    monkeypatch.setattr(eval_mod, "query", fake_query)
    rep = eval_mod.evaluate_group(str(ds), top_k=3)
    assert rep["contact_context_rate"] == 0.5  # 1 survived / 2 retrieved
    assert rep["contact_usability_rate"] == round(1 / 3, 3)  # end-to-end stays honest
    ok, _ = eval_mod.contact_gate(rep, 1.0)
    assert not ok
    ok, _ = eval_mod.contact_gate(rep, 0.5)
    assert ok


def test_contact_gate_pass_fail_no_signal():
    ok, msg = eval_mod.contact_gate({"contact_context_rate": 1.0}, 0.8)
    assert ok and "1.0 >= 0.8" in msg
    ok, msg = eval_mod.contact_gate({"contact_context_rate": 0.5}, 0.8)
    assert not ok and "0.5 < 0.8" in msg
    ok, msg = eval_mod.contact_gate({"contact_context_rate": None}, 0.8)
    assert not ok and "no contact signal" in msg
    # boundary passes
    ok, _ = eval_mod.contact_gate({"contact_context_rate": 0.8}, 0.8)
    assert ok
