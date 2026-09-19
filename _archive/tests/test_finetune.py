"""Tests for fine-tune export (offline) + train-runner dep guard."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.finetune import build_triplets, check_deps, export_triplets, load_goldens, train


def _corpus():
    return [
        {"chunk_id": "c1", "text": "Leave Policy: 12 annual leave days per year."},
        {"chunk_id": "c2", "text": "IT Security: report lost device within 1 hour."},
        {"chunk_id": "c3", "text": "VPN Guide: connect via vpn.company.com with SSO."},
    ]


def _goldens():
    return [
        {"question": "Chính sách nghỉ phép?", "gold_keywords": ["leave"]},
        {"question": "Mất laptop làm gì?", "gold_keywords": ["lost device"]},
        {"question": "Câu không keyword?", "gold_keywords": []},
    ]


def test_build_triplets():
    trips = build_triplets(_corpus(), _goldens(), negatives_per_anchor=2)
    assert len(trips) == 2  # third golden skipped (no keywords)
    t0 = trips[0]
    assert "leave" in t0["positive"].lower()
    assert t0["positive_chunk_id"] == "c1"
    assert len(t0["negatives"]) == 2
    assert all("leave" not in n.lower() for n in t0["negatives"])


def test_build_triplets_no_positive_skipped():
    trips = build_triplets(_corpus(), [
        {"question": "Chuyện không liên quan?", "gold_keywords": ["zzz-no-match"]}])
    assert trips == []


def test_export_triplets_writes_jsonl(tmp_path):
    goldens_path = tmp_path / "goldens.jsonl"
    goldens_path.write_text("\n".join([
        json.dumps({"question": "Chính sách nghỉ phép?", "gold_keywords": ["leave"]}),
        json.dumps({"question": "Mất laptop làm gì?", "gold_keywords": ["lost device"]}),
    ]))
    out = str(tmp_path / "triplets.jsonl")
    res = export_triplets(_corpus(), dataset_paths=[str(goldens_path)],
                          out_path=out, negatives_per_anchor=1)
    assert res["ok"] is True and res["triplets"] == 2 and res["goldens"] == 2
    rows = [json.loads(l) for l in Path(out).read_text().splitlines()]
    assert len(rows) == 2
    assert set(rows[0]) == {"anchor", "positive", "positive_chunk_id", "negatives"}


def test_load_goldens_reads_repo_datasets():
    rows = load_goldens()
    assert len(rows) >= 20  # dataset + enterprise + retrieval goldens
    assert all(r["question"] for r in rows)


def test_train_missing_deps_or_runs():
    deps = check_deps()
    assert set(deps) == {"ok", "missing", "hint"}
    r = train("/nonexistent/triplets.jsonl")
    assert r["ok"] is False
    assert r["error"] in ("missing_deps", "empty_triplets") or "Error" in r["error"]
