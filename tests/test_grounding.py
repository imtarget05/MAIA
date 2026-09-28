"""P1-3/P1-4: grounding + citation integrity — reason codes, support heuristic,
cross-tenant citation defense, offline LLM grounding fallback.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.loops.answer_loop import (
    GROUNDED,
    NOT_GROUNDED,
    TOKENS_LOW,
    UNVERIFIABLE,
    CitationChecker,
    GroundingChecker,
    LLMGroundingChecker,
    extract_cites,
)

# ---- extract_cites ----------------------------------------------------

def test_extract_cites_dedupes_and_sorts():
    assert extract_cites("See [S3] and [S1]. Also [S3].") == [1, 3]
    assert extract_cites("no cites here") == []


# ---- CitationChecker reason codes + support heuristic -----------------

def _candidates():
    return [
        {"chunk_id": "k1", "text": "Leave balance is credited 12 days per year.",
         "metadata": {"filename": "Leave.md", "tenant_id": "tenantA"}},
        {"chunk_id": "k2", "text": "VPN requires manager approval form.",
         "metadata": {"filename": "VPN.md", "tenant_id": "tenantB"}, "text": "VPN requires manager approval."},
    ]


def test_check_reasons_flags_missing_and_no_text():
    cc = CitationChecker(
        [{"chunk_id": "k1", "text": "leave days policy", "metadata": {}}])
    # S2 doesn't exist -> missing; S1 exists with text -> ok
    valid, reasons = cc.check_reasons("Answer [S1]. Bad cite [S2].")
    assert not valid
    assert reasons[2] == "missing"


def test_check_reasons_no_text():
    cc = CitationChecker([{"chunk_id": "k1", "text": "", "metadata": {}}])
    valid, reasons = cc.check_reasons("Answer here [S1].")
    assert not valid
    assert reasons[1] == "no_text"


def test_verify_support_true_when_overlap():
    cc = CitationChecker(
        [{"chunk_id": "k1", "text": "leave balance 12 days each year", "metadata": {}}])
    # sentence shares tokens with the chunk
    assert cc.verify_support("You get 12 leave days [S1].", cite_tag=1) is True


def test_verify_support_false_when_no_overlap():
    cc = CitationChecker(
        [{"chunk_id": "k1", "text": "kafka partitions consumer group", "metadata": {}}])
    # answer sentence is in Vietnamese about leave, chunk is about kafka
    assert cc.verify_support("Bạn có được bao nhiêu ngày nghỉ [S1].", cite_tag=1) is False


# ---- unauthorized source detection ------------------------------------

def test_check_unauthorized_sources_flags_cross_tenant_cite():
    candidates = [
        {"chunk_id": "k1", "text": "tenantA doc",
         "metadata": {"tenant_id": "tenantA"}},
        {"chunk_id": "k2", "text": "tenantB doc",
         "metadata": {"tenant_id": "tenantB"}},
    ]
    cc = CitationChecker(candidates)
    # answer cites S2 which belongs to tenantB (not in allowed set)
    bad = cc.check_unauthorized_sources("See [S2] for that.", {"tenantA"})
    assert bad == [2]
    # citing same-tenant chunk is fine
    assert cc.check_unauthorized_sources("See [S1] for that.", {"tenantA"}) == []


def test_check_unauthorized_sources_empty_allowed():
    cc = CitationChecker([{"chunk_id": "k1", "text": "x", "metadata": {"tenant_id": "tA"}}])
    assert cc.check_unauthorized_sources("See [S1].", set()) == []


# ---- GroundingChecker reason codes ------------------------------------

def test_grounding_check_detailed_supported():
    gc = GroundingChecker(threshold=0.15)
    context = "leave balance is 12 days per year credited annually"
    supported, score, reason = gc.check_detailed("12 leave days per year", context)
    assert supported is True
    assert reason == GROUNDED


def test_grounding_check_detailed_not_supported():
    gc = GroundingChecker(threshold=0.5)
    supported, _, reason = gc.check_detailed("quantum physics explained", "cats and dogs")
    assert supported is False
    assert reason in (NOT_GROUNDED, TOKENS_LOW, UNVERIFIABLE)


def test_grounding_check_detailed_unverifiable_empty_context():
    gc = GroundingChecker(threshold=0.15)
    supported, score, reason = gc.check_detailed("something", "")
    assert supported is False
    assert reason == UNVERIFIABLE


# ---- LLMGroundingChecker offline fallback -----------------------------

def test_llm_grounding_checker_defaults_to_token_mode():
    # conftest forces offline; USE_LLM_GROUNDING is False
    g = LLMGroundingChecker(llm=None)
    assert g.mode == "token"


def test_llm_grounding_checker_fallback_matches_token():
    g = LLMGroundingChecker(llm=None)
    context = "leave policy grants 12 days per year"
    supported, score = g.check("12 leave days per year", context)
    assert supported is True
    # score within [0,1]
    assert 0.0 <= score <= 1.0


def test_llm_grounding_checker_fallback_reason_code():
    g = LLMGroundingChecker(llm=None)
    supported, _, reason = g.check_detailed("quantum physics explained", "cats and dogs")
    assert supported is False
    assert reason in (NOT_GROUNDED, TOKENS_LOW, UNVERIFIABLE)


def test_llm_grounding_checker_falls_back_on_malformed_output():
    """A mock LLM returning garbage must not crash — falls back to token overlap."""

    class GarbageLLM:
        mode = "llm"
        def chat(self, messages):
            return "this is not json {{{"

    from maia.config import settings
    settings.USE_LLM_GROUNDING = True
    try:
        g = LLMGroundingChecker(llm=GarbageLLM())
        assert g.mode == "llm"
        # malformed JSON → fallback to token overlap
        supported, score = g.check("12 leave days", "leave 12 days")
        assert isinstance(supported, bool)
        assert 0.0 <= score <= 1.0
    finally:
        settings.USE_LLM_GROUNDING = False


def test_llm_grounding_checker_rejects_unsupported_claim():
    class JsonLLM:
        mode = "llm"
        def chat(self, messages):
            return '[]'  # empty array → cannot parse claims → UNVERIFIABLE

    from maia.config import settings
    settings.USE_LLM_GROUNDING = True
    try:
        g = LLMGroundingChecker(llm=JsonLLM())
        supported, score, reason = g.check_detailed("quantum physics", "cats dogs")
        assert supported is False
        assert reason == UNVERIFIABLE
    finally:
        settings.USE_LLM_GROUNDING = False


# ---- MAIA-008 & MAIA-022 Coverage (Phase-2 Remediation) ---------------

def test_maia_008_contradictory_documents_isolation():
    """MAIA-008: Verify contradictory SOPs do not cause hallucinated conclusions.

    When two candidate chunks have conflicting policy parameters (e.g. 5 million vs 10 million limit),
    an answer presenting both evidences with citations [S1] and [S2] is grounded and valid.
    An invented conclusion fabricating ungrounded figures (e.g. 20 million) must be rejected
    by GroundingChecker as ungrounded hallucination.
    """
    candidates = [
        {"chunk_id": "sop_v1", "text": "SOP-A quy định hạn mức tạm ứng công tác tối đa là 5 triệu đồng.",
         "metadata": {"filename": "SOP_A.md"}},
        {"chunk_id": "sop_v2", "text": "SOP-B quy định hạn mức tạm ứng công tác tối đa là 10 triệu đồng.",
         "metadata": {"filename": "SOP_B.md"}},
    ]
    context = " ".join(c["text"] for c in candidates)
    gc = GroundingChecker(threshold=0.3)
    cc = CitationChecker(candidates)

    # Leg 1: Stating both conflicting rules accurately citing respective sources
    answer_both = "Theo SOP-A hạn mức là 5 triệu [S1]. Trong khi SOP-B quy định 10 triệu [S2]."
    valid, invalid = cc.check(answer_both)
    assert valid is True and not invalid
    assert gc.grounded(answer_both, context) is True
    assert cc.verify_support("Theo SOP-A hạn mức là 5 triệu [S1]", cite_tag=1) is True
    assert cc.verify_support("Trong khi SOP-B quy định 10 triệu [S2]", cite_tag=2) is True

    # Leg 2: Invented conclusion (hallucinating 20 million) fails grounding
    hallucinated = "Do có sự khác nhau nên hạn mức thống nhất được duyệt là 20 triệu đồng cho toàn bộ nhân viên."
    assert gc.grounded(hallucinated, context) is False
    assert gc.score(hallucinated, context) < gc.threshold


def test_maia_022_citation_stability_deterministic():
    """MAIA-022: Verify repeated queries on the same context yield deterministic citations and grounding scores."""
    candidates = [
        {"chunk_id": "doc1", "text": "Chính sách bảo hiểm y tế doanh nghiệp chi trả 100% chi phí khám.",
         "metadata": {"filename": "Insurance.md"}},
        {"chunk_id": "doc2", "text": "Gói khám sức khỏe định kỳ diễn ra vào tháng 10 hàng năm.",
         "metadata": {"filename": "Health.md"}},
    ]
    answer = "Doanh nghiệp chi trả 100% chi phí [S1] và khám sức khỏe định kỳ tháng 10 [S2]."
    context = " ".join(c["text"] for c in candidates)

    gc = GroundingChecker(threshold=0.15)
    cc = CitationChecker(candidates)

    # First run baseline
    cites_0 = extract_cites(answer)
    valid_0, reasons_0 = cc.check_reasons(answer)
    score_0 = gc.score(answer, context)

    # Run 10 consecutive iterations
    for _ in range(10):
        cites_i = extract_cites(answer)
        valid_i, reasons_i = cc.check_reasons(answer)
        score_i = gc.score(answer, context)

        assert cites_i == cites_0 == [1, 2]
        assert valid_i == valid_0 is True
        assert reasons_i == reasons_0
        assert score_i == score_0

