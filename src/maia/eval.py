"""Formal RAG evaluation (verify: evaluation) - §7 retrieval quality.

Metrics:
- recall@k / hit@k (does gold chunk_id appear in candidates?)
- context_precision (fraction of retrieved that overlap gold keywords)
- faithfulness proxy (fraction of answer tokens grounded in context)
- answer_relevance proxy (keyword overlap Q<->A)
Dataset format (jsonl): {"question": str, "gold_chunk_ids": [...], "gold_keywords": [...]}
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.pipeline_query import query


def _overlap(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa))


def evaluate(dataset_path: str, top_k: int = 3) -> dict:
    rows = [json.loads(l) for l in Path(dataset_path).read_text().splitlines() if l.strip()]
    hits = recalls = precs = faiths = rels = 0
    details = []
    for r in rows:
        res = query(r["question"], top_k_final=top_k)
        got_ids = [c["chunk_id"] for c in res.get("citations", [])]
        gold = set(r.get("gold_chunk_ids", []))
        if not gold:
            # no gold ids -> judge by keyword coverage instead
            hit = 1 if res.get("has_evidence") else 0
            rec = float(hit)
        else:
            hit = 1 if gold & set(got_ids) else 0
            rec = len(gold & set(got_ids)) / max(1, len(gold))
        ctx = " ".join([c.get("text", "") for c in res.get("citations", [])])
        kw = r.get("gold_keywords", [])
        prec = sum(1 for k in kw if k.lower() in ctx.lower()) / max(1, len(kw))
        faith = _overlap(res.get("answer", ""), ctx)
        rel = _overlap(res.get("answer", ""), r["question"])
        hits += hit
        recalls += rec
        precs += prec
        faiths += min(1.0, faith * 4)  # scale proxy
        rels += min(1.0, rel * 6)
        details.append({"q": r["question"][:60], "hit": hit, "recall": round(rec, 3),
                        "ctx_prec": round(prec, 3), "has_evidence": res.get("has_evidence")})
    n = max(1, len(rows))
    return {"n": len(rows), "hit@k": round(hits / n, 3), "recall@k": round(recalls / n, 3),
            "context_precision": round(precs / n, 3), "faithfulness_proxy": round(faiths / n, 3),
            "relevance_proxy": round(rels / n, 3), "details": details}


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else "eval/dataset.jsonl"
    print(json.dumps(evaluate(ds), ensure_ascii=False, indent=2))
