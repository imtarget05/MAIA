"""P1c: embedding-grounded faithfulness metric."""
from maia.eval import faithfulness_embed


def test_paraphrase_scores_high():
    ctx = ["Chính sách nghỉ phép: nhân viên được nghỉ 12 ngày mỗi năm."]
    ans = "Bạn được nghỉ 12 ngày phép trong một năm."
    s = faithfulness_embed(ans, ctx)
    assert s is not None and s > 0.3


def test_unrelated_scores_low():
    ctx = ["Chính sách nghỉ phép: nhân viên được nghỉ 12 ngày mỗi năm."]
    ans = "Con mèo màu xanh bay qua mặt trăng."
    s = faithfulness_embed(ans, ctx)
    assert s is not None
    assert s < faithfulness_embed("Bạn được nghỉ 12 ngày phép trong một năm.", ctx)


def test_empty_returns_none():
    assert faithfulness_embed("", ["ctx"]) is None
    assert faithfulness_embed("answer", []) is None
