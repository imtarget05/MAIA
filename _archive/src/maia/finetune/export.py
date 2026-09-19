"""Fine-tune data export: MAIA goldens -> sentence-transformers triplets.

Builds (anchor, positive, negative) triplets for embedding fine-tuning
(MultipleNegativesRankingLoss) from:
  * eval retrieval/RAG datasets (question + gold_keywords), and
  * a corpus [{chunk_id, text, metadata}] (live Qdrant scroll or test fixture).

Positives = chunks containing a gold keyword (case-insensitive); negatives =
deterministic sample of non-matching chunks. Pure stdlib + jsonl I/O, fully
offline. Output ready for `sentence-transformers` training scripts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import settings


def load_goldens(dataset_paths: list[str] | None = None) -> list[dict]:
    """Load {question, gold_keywords} rows from eval JSONL files."""
    paths = dataset_paths or [
        "eval/dataset.jsonl", "eval/enterprise_dataset.jsonl", "eval/retrieval_dataset.jsonl"]
    rows = []
    for p in paths:
        try:
            for line in Path(p).read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("question"):
                    rows.append({"question": r["question"],
                                 "gold_keywords": r.get("gold_keywords", []) or []})
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return rows


def build_triplets(corpus: list[dict], goldens: list[dict],
                   negatives_per_anchor: int = 3) -> list[dict]:
    """Deterministic (anchor, positive, negative) triplets.

    A chunk is positive for a question when any gold keyword appears in it
    (case-insensitive substring). Questions without keywords or with no
    positive chunk in this corpus are skipped (reported, not failed).
    """
    texts = [(c.get("chunk_id", f"chunk_{i}"), c.get("text", "") or "")
             for i, c in enumerate(corpus)]
    triplets, skipped = [], []
    for g in goldens:
        q = g["question"]
        kws = [k.lower() for k in g.get("gold_keywords", []) if k]
        if not kws:
            skipped.append({"question": q[:60], "reason": "no_gold_keywords"})
            continue
        pos = [(cid, t) for cid, t in texts if t and any(k in t.lower() for k in kws)]
        if not pos:
            skipped.append({"question": q[:60], "reason": "no_positive_in_corpus"})
            continue
        neg_pool = [(cid, t) for cid, t in texts
                    if t and not any(k in t.lower() for k in kws)]
        for pid, ptext in pos:
            negs = [t for _, t in neg_pool[:negatives_per_anchor]]
            # pad by cycling when the pool is smaller than requested
            while negs and len(negs) < negatives_per_anchor:
                negs.append(neg_pool[len(negs) % len(neg_pool)][1])
            triplets.append({"anchor": q, "positive": ptext,
                             "positive_chunk_id": pid, "negatives": negs})
    return triplets


def export_triplets(corpus: list[dict], dataset_paths: list[str] | None = None,
                    out_path: str | None = None,
                    negatives_per_anchor: int = 3) -> dict:
    """Write triplets JSONL. Returns {ok, path, triplets, skipped, goldens}."""
    goldens = load_goldens(dataset_paths)
    triplets = build_triplets(corpus, goldens, negatives_per_anchor)
    skipped = len(goldens) - len({t["anchor"] for t in triplets})
    out = out_path or os.path.join(settings.FINETUNE_OUTPUT_DIR, "triplets.jsonl")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w") as f:
            for t in triplets:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        return {"ok": True, "path": out, "triplets": len(triplets),
                "goldens": len(goldens), "skipped_goldens": skipped}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "triplets": len(triplets), "goldens": len(goldens)}
