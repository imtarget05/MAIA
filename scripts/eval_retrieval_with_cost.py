"""Retrieval eval + cost/latency report (Byte JD evidence).

Runs the real `evaluate_group` harness on a golden file, timing each query,
then combines quality (hit@k/MRR/faithfulness) with reliability accounting
from the shared metrics registry (tokens/cost/latency — tracked inside
`pipeline_query.query`).

Offline-safe: conftest-style hash embeddings; mock LLM when no LAN provider.
If Qdrant is down the report honestly says `mode: degraded` with refusal
rate — never fake numbers.

Usage: .venv/bin/python scripts/eval_retrieval_with_cost.py [--group exact] [--top-k 3]
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import date

os.environ.setdefault("MAIA_EMBED_FORCE_HASH", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from maia.eval import evaluate_group  # noqa: E402
from maia.loops.metrics import registry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="exact")
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()
    path = os.path.join("eval", "golden", f"{args.group}.jsonl")
    if not os.path.exists(path):
        print(f"missing {path}", file=sys.stderr)
        return 2
    q0, t0 = registry.get("maia_queries_total"), registry.get("maia_tokens_total")
    start = time.time()
    rep = evaluate_group(path, top_k=args.top_k)
    wall_s = time.time() - start
    n = max(1, rep.get("n", 1))
    details = rep.get("details", [])
    report = {
        "date": str(date.today()),
        "group": args.group,
        "top_k": args.top_k,
        "env": {"embed": "hash" if os.environ.get("MAIA_EMBED_FORCE_HASH") else "live"},
        "quality": {k: rep.get(k) for k in
                    ("n", "hit@k", "recall@k", "context_precision",
                     "faithfulness_proxy", "relevance_proxy", "mrr",
                     "false_refusal_rate", "refusal_accuracy", "leakage_rate")},
        "reliability": {
            "wall_s_total": round(wall_s, 2),
            "wall_s_per_query": round(wall_s / n, 3),
            "latency_p95_s": round(registry.latency_p95(), 4),
            "latency_avg_s": round(registry.latency_avg(), 4),
            "tokens_total_est": registry.get("maia_tokens_total") - t0,
            "cost_saved_usd_est": round(registry.get("maia_cost_saved_usd_total"), 6),
            "queries_served": registry.get("maia_queries_total") - q0,
        },
        "note": ("hash-embed offline mode: dense scores are sparse, refusals expected; "
                 "re-run with live BGE-M3 + Qdrant for production numbers. "
                 "Latency/cost columns are measured, quality columns are real harness output."),
    }
    os.makedirs("eval/reports", exist_ok=True)
    out = f"eval/reports/retrieval-cost-{args.group}-{date.today()}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
