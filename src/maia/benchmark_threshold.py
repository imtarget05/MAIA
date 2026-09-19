"""MAIA-01: threshold sensitivity benchmark on the golden dataset.

Sweeps SIMILARITY_THRESHOLD and AGENT_EVIDENCE_THRESHOLD over a data-driven grid and measures, per value:
  recall@k, hit@k, context_precision, MRR, false_refusal_rate, leakage_rate.
Uses the real Qdrant stack (enterprise docs must be ingested first).

Usage:
  PYTHONPATH=src python -m maia.benchmark_threshold --group vi_policy en_policy no_answer
  PYTHONPATH=src python -m maia.benchmark_threshold --all
  PYTHONPATH=src python -m maia.benchmark_threshold --sweep-both  # sweep both thresholds
  PYTHONPATH=src python -m maia.benchmark_threshold --lock-thresholds  # output locked values for config.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import settings
from .eval import GOLDEN_DIR, evaluate_group

REPORT_DIR = Path(__file__).resolve().parents[2] / "eval" / "reports"

DEFAULT_SIMILARITY_GRID = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
DEFAULT_AGENT_EVIDENCE_GRID = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]


def benchmark_threshold(groups: list[str] | None = None, grid: list[float] | None = None,
                        top_k: int = 3) -> dict:
    """Sweep SIMILARITY_THRESHOLD only (legacy mode for backward compat)."""
    grid = grid or DEFAULT_SIMILARITY_GRID
    if groups:
        files = [GOLDEN_DIR / f"{g}.jsonl" for g in groups]
    else:
        files = sorted(GOLDEN_DIR.glob("*.jsonl"))
    old = settings.SIMILARITY_THRESHOLD
    report = {"generated_at": datetime.now().isoformat(),
              "grid": grid, "top_k": top_k, "groups": {}}
    try:
        for f in files:
            if not f.exists():
                continue
            per_th = []
            for th in grid:
                settings.SIMILARITY_THRESHOLD = th
                rep = evaluate_group(str(f), top_k=top_k)
                # keep only the decision-relevant metrics per threshold
                per_th.append({"threshold": th,
                               "recall@k": rep["recall@k"],
                               "hit@k": rep["hit@k"],
                               "context_precision": rep["context_precision"],
                               "mrr": rep["mrr"],
                               "false_refusal_rate": rep["false_refusal_rate"],
                               "refusal_accuracy": rep["refusal_accuracy"],
                               "leakage_rate": rep["leakage_rate"]})
            # pick best threshold: maximize (recall@k - false_refusal_rate),
            # tie-break by mrr. Purely data-driven — no hardcoded value.
            best = max(per_th, key=lambda m: (m["recall@k"] - m["false_refusal_rate"], m["mrr"]))
            report["groups"][f.stem] = {"per_threshold": per_th,
                                        "recommended_threshold": best["threshold"],
                                        "basis": "max(recall@k - false_refusal_rate, tie-break mrr)"}
    finally:
        settings.SIMILARITY_THRESHOLD = old
    return report


def benchmark_both_thresholds(groups: list[str] | None = None,
                              sim_grid: list[float] | None = None,
                              agent_grid: list[float] | None = None,
                              top_k: int = 3) -> dict:
    """Sweep both SIMILARITY_THRESHOLD and AGENT_EVIDENCE_THRESHOLD.

    For each combination, evaluates on golden sets and finds the Pareto-optimal
    pair maximizing (recall@k - false_refusal_rate) with MRR tie-break.
    """
    sim_grid = sim_grid or DEFAULT_SIMILARITY_GRID
    agent_grid = agent_grid or DEFAULT_AGENT_EVIDENCE_GRID
    if groups:
        files = [GOLDEN_DIR / f"{g}.jsonl" for g in groups]
    else:
        files = sorted(GOLDEN_DIR.glob("*.jsonl"))

    old_sim = settings.SIMILARITY_THRESHOLD
    old_agent = settings.AGENT_EVIDENCE_THRESHOLD

    report = {"generated_at": datetime.now().isoformat(),
              "sim_grid": sim_grid, "agent_grid": agent_grid, "top_k": top_k, "groups": {}}

    try:
        for f in files:
            if not f.exists():
                continue
            per_combo = []
            for sim_th in sim_grid:
                for agent_th in agent_grid:
                    settings.SIMILARITY_THRESHOLD = sim_th
                    settings.AGENT_EVIDENCE_THRESHOLD = agent_th
                    rep = evaluate_group(str(f), top_k=top_k)
                    per_combo.append({
                        "sim_threshold": sim_th,
                        "agent_threshold": agent_th,
                        "recall@k": rep["recall@k"],
                        "hit@k": rep["hit@k"],
                        "context_precision": rep["context_precision"],
                        "mrr": rep["mrr"],
                        "false_refusal_rate": rep["false_refusal_rate"],
                        "refusal_accuracy": rep["refusal_accuracy"],
                        "leakage_rate": rep["leakage_rate"],
                    })
            # pick best combo: maximize (recall@k - false_refusal_rate), tie-break MRR
            best = max(per_combo, key=lambda m: (m["recall@k"] - m["false_refusal_rate"], m["mrr"]))
            report["groups"][f.stem] = {"per_combination": per_combo,
                                        "recommended_sim_threshold": best["sim_threshold"],
                                        "recommended_agent_threshold": best["agent_threshold"],
                                        "basis": "max(recall@k - false_refusal_rate, tie-break mrr)"}
    finally:
        settings.SIMILARITY_THRESHOLD = old_sim
        settings.AGENT_EVIDENCE_THRESHOLD = old_agent
    return report


def lock_thresholds(report: dict) -> tuple[float, float]:
    """Aggregate per-group recommendations into a single locked pair.

    Uses median of per-group recommended thresholds to avoid outlier bias.
    """
    sim_vals = []
    agent_vals = []
    for r in report.get("groups", {}).values():
        if "recommended_sim_threshold" in r:
            sim_vals.append(r["recommended_sim_threshold"])
            agent_vals.append(r["recommended_agent_threshold"])
        elif "recommended_threshold" in r:
            # Legacy single-threshold report
            sim_vals.append(r["recommended_threshold"])
            agent_vals.append(r["recommended_threshold"])

    if not sim_vals:
        return settings.SIMILARITY_THRESHOLD, settings.AGENT_EVIDENCE_THRESHOLD

    sim_vals.sort()
    agent_vals.sort()
    locked_sim = sim_vals[len(sim_vals) // 2]
    locked_agent = agent_vals[len(agent_vals) // 2]
    return locked_sim, locked_agent


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Threshold sensitivity benchmark (MAIA-01)")
    p.add_argument("--group", nargs="*", help="golden group names (default: all)")
    p.add_argument("--all", action="store_true")
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--sweep-both", action="store_true",
                   help="sweep both SIMILARITY_THRESHOLD and AGENT_EVIDENCE_THRESHOLD")
    p.add_argument("--lock-thresholds", action="store_true",
                   help="output locked threshold values for config.py")
    args = p.parse_args()
    groups = None if (args.all or not args.group) else args.group

    if args.sweep_both:
        rep = benchmark_both_thresholds(groups=groups, top_k=args.top_k)
    else:
        rep = benchmark_threshold(groups=groups, top_k=args.top_k)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"threshold_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    # console summary
    if args.sweep_both:
        for g, r in rep["groups"].items():
            sim_rec = r["recommended_sim_threshold"]
            agent_rec = r["recommended_agent_threshold"]
            row = max(r["per_combination"],
                      key=lambda m: (m["recall@k"] - m["false_refusal_rate"], m["mrr"]))
            print(f"[{g}] SIM={sim_rec} AGENT={agent_rec} "
                  f"(recall@k={row['recall@k']}, false_refusal={row['false_refusal_rate']}, mrr={row['mrr']})")
    else:
        for g, r in rep["groups"].items():
            row = max(r["per_threshold"], key=lambda m: (m["recall@k"] - m["false_refusal_rate"], m["mrr"]))
            print(f"[{g}] recommended threshold = {r['recommended_threshold']} "
                  f"(recall@k={row['recall@k']}, false_refusal={row['false_refusal_rate']}, mrr={row['mrr']})")

    if args.lock_thresholds:
        locked_sim, locked_agent = lock_thresholds(rep)
        print("\nLOCKED THRESHOLDS:")
        print(f"  SIMILARITY_THRESHOLD = {locked_sim}")
        print(f"  AGENT_EVIDENCE_THRESHOLD = {locked_agent}")
        print("\nAdd to config.py:")
        print(f'  SIMILARITY_THRESHOLD: float = {locked_sim}  # locked from threshold sweep')
        print(f'  AGENT_EVIDENCE_THRESHOLD: float = {locked_agent}  # locked from threshold sweep')

    print(f"full report -> {out}")
