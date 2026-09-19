"""Source reader + answer sanitizer: evidence zone stays highlighted in full text."""
from maia.answer_format import clean_answer, has_technical_leak
from maia.source_reader import (
    build_source_view,
    locate_excerpt,
    render_excerpt_html,
    render_source_html,
)


def _corpus():
    return [
        {"chunk_id": "docA_0", "text": "Chính sách nghỉ phép mở đầu. Mọi nhân viên có 12 ngày.",
         "metadata": {"doc_id": "docA", "filename": "Leave_Policy.md"}},
        {"chunk_id": "docA_1", "text": "Chuyển phép tối đa 3 ngày sang năm sau khi được duyệt.",
         "metadata": {"doc_id": "docA", "filename": "Leave_Policy.md"}},
        {"chunk_id": "docB_0", "text": "VPN truy cập qua vpn.company.com.",
         "metadata": {"doc_id": "docB", "filename": "VPN_Guide.md"}},
    ]


def test_grouping_order_and_cited_flag():
    view = build_source_view(
        {"chunk_id": "docA_1", "filename": "Leave_Policy.md",
         "text": "Chuyển phép tối đa 3 ngày"},
        _corpus(),
    )
    assert view["filename"] == "Leave_Policy.md"
    assert view["total_chunks"] == 2
    assert [c["chunk_id"] for c in view["chunks"]] == ["docA_0", "docA_1"]
    assert [c["cited"] for c in view["chunks"]] == [False, True]


def test_fallback_single_passage_when_store_misses():
    view = build_source_view(
        {"chunk_id": "web_0", "filename": "web", "text": "snippet ngoài"},
        _corpus(),
    )
    assert view["total_chunks"] == 1
    assert view["chunks"][0]["cited"] is True
    assert "snippet ngoài" in view["full_text"]


def test_full_html_keeps_cited_highlight():
    view = build_source_view(
        {"chunk_id": "docA_0", "filename": "Leave_Policy.md",
         "text": "Mọi nhân viên có 12 ngày"},
        _corpus(),
    )
    html = render_source_html(view)
    assert "<mark class='maia-cited-mark'" in html
    assert "12 ngày" in html
    # non-cited sibling present without highlight
    assert "Chuyển phép" in html
    assert html.count("<mark") == 1


def test_excerpt_card_labels_cited_zone():
    html = render_excerpt_html("Mọi nhân viên có 12 ngày", "[S1]", "Leave_Policy.md")
    assert "Đoạn AI đã đọc" in html
    assert "[S1]" in html and "Leave_Policy.md" in html


def test_locate_excerpt_inside_full_chunk():
    start, end = locate_excerpt(
        "Mở đầu không liên quan. Mọi nhân viên có 12 ngày phép năm. Kết thúc.",
        "Mọi nhân viên có 12 ngày",
    )
    assert 0 <= start < end


def test_clean_answer_strips_tags_and_footer():
    raw = ("Trả lời ngắn [S1].\n"
           "<retrieved_document id=\"abc_0\" source=\"Leave_Policy.md\">quote</retrieved_document>\n"
           "Sources: [S1] Leave_Policy.md, [S2] HR_Policy.md")
    out = clean_answer(raw)
    assert "retrieved_document" not in out
    assert "Sources:" not in out
    assert "[S1]" in out  # inline citation preserved
    assert has_technical_leak(raw) is True
    assert has_technical_leak(out) is False


def test_clean_answer_keeps_footer_when_only_citation_carrier():
    raw = "Tóm tắt chung.\nSources: [S2]"
    out = clean_answer(raw)
    assert "[S2]" in out
