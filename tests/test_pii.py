"""G-04: tests for PII detection, redaction, and pipeline integration.
G-04-FU2: internal role emails (it-help@, hr@, ... @company.com) are public
contact info — they must NOT be flagged/redacted. Personal emails still are.

Uses synthetic fake fixtures only — no real PII.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.loops.guardrails import OutputGuardrail
from maia.loops.pii import PIIScanner, _luhn_ok, is_role_email

# --- G-04-FU2: role-email allowlist ---

ROLE_EMAILS = [
    "hr@company.com",
    "it-help@company.com",
    "security@company.com",
    "benefits@company.com",
    "eap@company.com",
    "finance@company.com",
    "onboarding@company.com",
    "support@company.com",
]


def test_role_emails_are_not_pii():
    for email in ROLE_EMAILS:
        assert not PIIScanner.detect(f"Liên hệ {email} để được hỗ trợ."), email
        assert PIIScanner.redact(f"Liên hệ {email} nhé.") == f"Liên hệ {email} nhé.", email
        assert not PIIScanner.is_dirty(f"Liên hệ {email}"), email


def test_is_role_email_exact_and_prefix():
    assert is_role_email("hr@company.com")
    assert is_role_email("HR@Company.COM")  # case-insensitive
    # prefix rule covers future role inboxes on the company domain
    assert is_role_email("hr-recruit@company.com")
    assert is_role_email("it-support@company.com")
    # ... but not personal mailboxes, even with a role-like local part
    assert not is_role_email("hr@gmail.com")
    assert not is_role_email("nvA@example.com")
    assert not is_role_email("not-an-email")


def test_personal_email_still_redacted_with_role_present():
    text = "Gửi tới it-help@company.com từ nvA@example.com."
    redacted = PIIScanner.redact(text)
    assert "it-help@company.com" in redacted
    assert "[PII-EMAIL]" in redacted
    assert "nvA@example.com" not in redacted


# --- PIIScanner unit tests ---

def test_detects_email():
    text = "Liên hệ test@example.com để được hỗ trợ."
    hits = PIIScanner.detect(text)
    assert any(h.pii_type == "EMAIL" for h in hits)


def test_detects_vn_phone_zero_prefix():
    text = "Số điện thoại 0912345678 của tôi."
    hits = PIIScanner.detect(text)
    assert any(h.pii_type == "PHONE" for h in hits)


def test_detects_vn_phone_plus84_prefix():
    text: str = "Gọi +84912345678 nhé."
    hits = PIIScanner.detect(text)
    assert any(h.pii_type == "PHONE" for h in hits)


def test_detects_cccd_12_digits():
    text = "CCCD 123 456 789 012 của nhân viên."
    hits = PIIScanner.detect(text)
    assert any(h.pii_type == "CCCD" for h in hits)


def test_credit_card_requires_luhn():
    # Random 16 digits — not Luhn-valid → not flagged
    assert not _luhn_ok("1234 5678 9012 3456")
    # Known Luhn-valid test number → flagged
    assert _luhn_ok("4111 1111 1111 1111")
    text_valid = "Thẻ 4111 1111 1111 1111 hết hạn."
    hits = PIIScanner.detect(text_valid)
    assert any(h.pii_type == "CREDIT_CARD" for h in hits)
    text_invalid = "Số 1234 5678 9012 3456 không hợp lệ."
    hits_bad = PIIScanner.detect(text_invalid)
    assert not any(h.pii_type == "CREDIT_CARD" for h in hits_bad)


def test_redact_replaces_with_placeholder():
    text = "Email test@example.com và SĐT 0912345678."
    redacted = PIIScanner.redact(text)
    assert "[PII-EMAIL]" in redacted
    assert "[PII-PHONE]" in redacted
    assert "test@example.com" not in redacted
    assert "0912345678" not in redacted


def test_redact_preserves_non_pii_text():
    text = "Chính sách nghỉ phép 12 ngày mỗi năm."
    assert PIIScanner.redact(text) == text


def test_is_dirty_true_and_false():
    assert PIIScanner.is_dirty("Liên hệ test@example.com")
    assert not PIIScanner.is_dirty("Chính sách nghỉ phép 12 ngày.")


# --- OutputGuardrail PII layer ---

def test_output_guardrail_redacts_pii_in_answer():
    g = OutputGuardrail()
    ok, issues, answer = g.check("Liên hệ test@example.com để được hỗ trợ.")
    assert "pii_redacted_in_output" in issues
    assert "[PII-EMAIL]" in answer
    assert "test@example.com" not in answer


def test_output_guardrail_keeps_role_contact_in_answer():
    g = OutputGuardrail()
    ok, issues, answer = g.check("Liên hệ it-help@company.com máy lẻ 202 để được hỗ trợ.")
    assert "pii_redacted_in_output" not in issues
    assert "it-help@company.com" in answer


def test_output_guardrail_passes_clean_answer():
    g = OutputGuardrail()
    ok, issues, answer = g.check("Chính sách nghỉ phép 12 ngày mỗi năm.")
    assert ok and not issues
    assert answer == "Chính sách nghỉ phép 12 ngày mỗi năm."


def test_output_guardrail_secret_leak_still_detected():
    g = OutputGuardrail()
    ok, issues, answer = g.check("Here is the system secret: password123")
    assert not ok
    assert "potential_secret_leak" in issues


# --- Pipeline integration ---

def test_ingest_pipeline_redacts_pii(tmp_path):
    from maia.chunking import split_documents
    from maia.ingestion import RawDoc
    from maia.ingestion_pipeline import _pii_scan_chunks

    malicious = ("Nhân viên A, email nvA@example.com, SĐT 0912345678, "
                 "CCCD 123 456 789 012. Chính sách nghỉ phép 12 ngày.")
    docs = [RawDoc(text=malicious, metadata={"filename": "pii_test.md", "source": "test"})]
    # call the shared tail directly with a fake stack
    chunks = split_documents(docs, chunk_size=512, chunk_overlap=50)
    chunks, dirty, type_counts = _pii_scan_chunks(chunks)
    assert dirty >= 1
    assert "EMAIL" in type_counts
    full_text = " ".join(c.text for c in chunks)
    assert "nvA@example.com" not in full_text
    assert "0912345678" not in full_text
    assert "123 456 789 012" not in full_text
    assert "[PII-EMAIL]" in full_text
    assert "[PII-PHONE]" in full_text
    assert "[PII-CCCD]" in full_text
    # non-PII content preserved
    assert "Chính sách nghỉ phép 12 ngày" in full_text
