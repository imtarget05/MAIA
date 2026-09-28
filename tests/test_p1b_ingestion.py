"""P1b: ingestion formats (csv/html) + paragraph chunking strategy."""
from maia.chunking import split_documents
from maia.ingestion import RawDoc, _read_csv_text, _read_html_text


def test_csv_flattens_rows(tmp_path):
    f = tmp_path / "policies.csv"
    f.write_text("title,owner\nexpense,finance\nleave,hr\n")
    text = _read_csv_text(f)
    assert "expense" in text and "row1" in text


def test_html_strips_tags(tmp_path):
    f = tmp_path / "p.html"
    f.write_text("<html><body><h1>Leave</h1><script>evil()</script><p>5 days</p></body></html>")
    text = _read_html_text(f)
    assert "Leave" in text and "evil" not in text


def test_paragraph_strategy_never_cuts_paragraph():
    doc = RawDoc(text="Para one is here.\n\nPara two is here.\n\nPara three.",
                 metadata={"doc_id": "d"})
    chunks = split_documents([doc], chunk_size=30, chunk_overlap=0, strategy="paragraph")
    assert len(chunks) >= 2
    for c in chunks:
        assert "Para" in c.text  # no mid-paragraph cut
