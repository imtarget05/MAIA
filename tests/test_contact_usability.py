"""G-04-FU2: end-to-end contact usability — redact -> answer keeps contact.

Pipeline: ingest PII scan -> output guardrail -> eval metric. Uses synthetic
fake fixtures only — no real PII.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import eval as eval_mod
from maia.chunking import split_documents
from maia.ingestion import RawDoc
from maia.ingestion_pipeline import _pii_scan_chunks
from maia.loops.guardrails import OutputGuardrail


def test_ingest_keeps_role_contact_redacts_personal():
    docs = [
        RawDoc(text="Hỗ trợ VPN liên hệ it-help@company.com máy lẻ 202.",
               metadata={"filename": "vpn.md", "source": "test"}),
        RawDoc(text="Nhân viên B email nvB@example.com SĐT 0912345678.",
               metadata={"filename": "hr.md", "source": "test"}),
    ]
    chunks = split_documents(docs, chunk_size=512, chunk_overlap=50)
    chunks, dirty, type_counts = _pii_scan_chunks(chunks)
    full = " ".join(c.text for c in chunks)
    # role contact survives ingestion
    assert "it-help@company.com" in full
    # personal PII still redacted
    assert "nvB@example.com" not in full and "[PII-EMAIL]" in full
    assert "0912345678" not in full and "[PII-PHONE]" in full
    assert dirty == 1 and type_counts.get("EMAIL") == 1


def test_answer_keeps_role_contact_redacts_personal():
    g = OutputGuardrail()
    _, issues, answer = g.check(
        "Bạn liên hệ it-help@company.com (nhân sự nvB@example.com đã nghỉ)."
    )
    assert "it-help@company.com" in answer
    assert "nvB@example.com" not in answer


def test_eval_row_contact_usable_flag():
    row = {"question": "Cần hỗ trợ VPN thì liên hệ ai?",
           "gold_chunk_ids": [], "gold_keywords": [],
           "expect_contact": "it-help@company.com"}
    usable = eval_mod._eval_row(
        row, {"answer": "Liên hệ it-help@company.com máy lẻ 202.",
              "citations": [], "has_evidence": True}, top_k=3)
    assert usable["contact_usable"] == 1
    redacted = eval_mod._eval_row(
        row, {"answer": "Liên hệ [PII-EMAIL] máy lẻ 202.",
              "citations": [], "has_evidence": True}, top_k=3)
    assert redacted["contact_usable"] == 0
    # rows without expect_contact carry no signal (None, not 0)
    plain = eval_mod._eval_row(
        {"question": "Nghỉ phép?", "gold_chunk_ids": [], "gold_keywords": []},
        {"answer": "12 ngày.", "citations": [], "has_evidence": True}, top_k=3)
    assert plain["contact_usable"] is None
