"""P1-6: OutputValidator — semantic authorization-claim detection + PII redaction.

The chat path must never let the LLM *claim* that a tool was executed/approved.
Action responses (confirm_action) are exempt via is_action_response=True.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.loops.guardrails import OutputValidator

# ---- claim scan --------------------------------------------------------

def test_clean_answer_passes():
    v = OutputValidator()
    ok, issues, ans = v.check("Chính sách nghỉ phép là 12 ngày mỗi năm.")
    assert ok and not issues
    assert ans == "Chính sách nghỉ phép là 12 ngày mỗi năm."


def test_unauthorized_claim_flagged_in_chat_answer():
    v = OutputValidator()
    ok, issues, ans = v.check("Tôi đã tạo yêu cầu nghỉ phép của bạn rồi, được phê duyệt bởi quản lý.")
    assert not ok
    assert "unauthorized_action_claim" in issues


@pytest.mark.parametrize("claim", [
    "approved by manager",
    "authorized by admin",
    "executed create_it_ticket",
    "created ticket IT-42",
    "Đã tạo ticket IT-42",
    "Đã thực hiện create_leave_request",
    "request approved",
])
def test_each_claim_pattern_flagged(claim):
    v = OutputValidator()
    ok, issues, _ = v.check(claim)
    assert not ok
    assert "unauthorized_action_claim" in issues


def test_action_response_exempt():
    """confirm_action output legitimately states what it did — must pass."""
    v = OutputValidator()
    ok, issues, ans = v.check("Đã tạo ticket IT-42 (VPN). verified: open.",
                              is_action_response=True)
    assert ok
    assert "unauthorized_action_claim" not in issues


def test_action_response_still_redacts_pii():
    v = OutputValidator()
    ok, issues, ans = v.check("Đã tạo ticket. Liên hệ test@example.com",
                              is_action_response=True)
    # PII gets redacted; secret-leak / claim scan skipped
    assert "pii_redacted_in_output" in issues
    assert "[PII-EMAIL]" in ans


# ---- PII + secret layers inherited from OutputGuardrail ----------------

def test_output_validator_redacts_pii():
    v = OutputValidator()
    ok, issues, ans = v.check("Liên hệ test@example.com để được hỗ trợ.")
    assert "pii_redacted_in_output" in issues
    assert "[PII-EMAIL]" in ans


def test_output_validator_keeps_role_contact():
    v = OutputValidator()
    ok, issues, ans = v.check("Liên hệ it-help@company.com máy lẻ 202.")
    assert "pii_redacted_in_output" not in issues
    assert "it-help@company.com" in ans


def test_output_validator_secret_leak():
    v = OutputValidator()
    ok, issues, ans = v.check("Here is the system secret: password123")
    assert not ok
    assert "potential_secret_leak" in issues


# ---- schema validation -------------------------------------------------

def test_validate_schema_empty_answer():
    ok, reasons = OutputValidator.validate_schema("", [])
    assert not ok
    assert "empty_answer" in reasons


def test_validate_schema_hallucinated_citation():
    citations = [{"tag": "[S1]"}, {"tag": "[S2]"}]  # only 2 returned
    ok, reasons = OutputValidator.validate_schema("Answer [S1] and [S5].", citations)
    assert not ok
    assert any("3" in r or "5" in r for r in reasons)


def test_validate_schema_ok():
    ok, reasons = OutputValidator.validate_schema("Answer [S1].", [{"tag": "[S1]"}])
    assert ok and not reasons
