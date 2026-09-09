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

from maia.pipeline_query import query


def evaluate_agent(dataset_path: str) -> dict:
    """Eval for enterprise agent: intent accuracy + keyword precision."""
    from maia.agent.intents import detect_intent
    rows = [json.loads(l) for l in Path(dataset_path).read_text().splitlines() if l.strip()]
    from maia.agent.agent import EnterpriseAgent
    agent = EnterpriseAgent()
    correct = 0
    details = []
    for r in rows:
        pred = detect_intent(r["question"])
        gold = r.get("intent", "general")
        hit = 1 if pred == gold else 0
        correct += hit
        # also check action if expected
        details.append({"q": r["question"][:50], "gold_intent": gold, "pred": pred, "hit": hit})
    return {"n": len(rows), "intent_accuracy": round(correct / max(1, len(rows)), 3), "details": details}


def _overlap(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa))


# --- per-row evaluation (shared by evaluate / evaluate_group / benchmark) ---

def _eval_row(r: dict, res: dict, top_k: int) -> dict:
    """Evaluate one golden row against a query() result dict."""
    got_ids = [c["chunk_id"] for c in res.get("citations", [])]
    gold = set(r.get("gold_chunk_ids", []))
    if not gold:
        # no gold ids -> judge by evidence presence
        hit = 1 if res.get("has_evidence") else 0
        rec = float(hit)
        mrr = 0.0
    else:
        hit = 1 if gold & set(got_ids) else 0
        rec = len(gold & set(got_ids)) / max(1, len(gold))
        # MRR: reciprocal rank of first gold chunk in citation order
        mrr = 0.0
        for rank, cid in enumerate(got_ids, start=1):
            if cid in gold:
                mrr = 1.0 / rank
                break
    ctx = " ".join([c.get("text", "") for c in res.get("citations", [])])
    kw = r.get("gold_keywords", [])
    prec = sum(1 for k in kw if k.lower() in ctx.lower()) / max(1, len(kw)) if kw else 1.0
    faith = _overlap(res.get("answer", ""), ctx)
    rel = _overlap(res.get("answer", ""), r["question"])
    refused = bool(res.get("refused")) or (not res.get("has_evidence") and not got_ids)
    expect_refusal = bool(r.get("expect_refusal")) or bool(r.get("expect_no_evidence"))
    # G-04-FU2: contact usability — after PII redaction, does the final answer
    # still contain a usable contact? (None for rows without expect_contact.)
    # contact_in_ctx is the LLM-independent CONDITIONAL invariant: given the row
    # was retrieved (hit==1), did the role email survive ingest-time redaction
    # and reach the retrieved context (what the LLM sees)? Retrieval misses
    # (hit==0) carry no redact signal -> None, so the aggregate measures pure
    # redaction survival, decoupled from retrieval depth. contact_usable stays
    # unconditional (honest end-to-end metric). The mock LLM never echoes
    # context, so only contact_in_ctx is CI-gateable.
    expect_contact = (r.get("expect_contact") or "").strip().lower()
    usable = None
    in_ctx = None
    if expect_contact:
        usable = 1 if expect_contact in res.get("answer", "").lower() else 0
        if hit:
            in_ctx = 1 if expect_contact in ctx.lower() else 0
    return {"hit": hit, "recall": rec, "mrr": mrr, "prec": prec,
            "faith": min(1.0, faith * 4), "rel": min(1.0, rel * 6),
            "refused": refused, "expect_refusal": expect_refusal,
            "leaked": bool(got_ids) if r.get("expect_no_evidence") else None,
            "has_evidence": res.get("has_evidence"),
            "contact_usable": usable, "contact_in_ctx": in_ctx}


def evaluate_group(dataset_path: str, top_k: int = 3, agent: bool = False) -> dict:
    """Evaluate one golden-group file. Adds audit metrics on top of evaluate():
    - false_refusal_rate: correct questions wrongly refused
    - refusal_accuracy: for expect_refusal rows, fraction actually refused
    - leakage_rate: for expect_no_evidence rows, fraction that returned citations
    - mrr: mean reciprocal rank of the first gold chunk"""
    rows = [json.loads(l) for l in Path(dataset_path).read_text().splitlines() if l.strip()]
    hits = recalls = precs = faiths = rels = mrrs = 0.0
    false_refusals = refusal_cases = refusal_correct = 0
    leak_cases = leak_hits = 0
    contact_cases = contact_usable = 0
    ctx_cases = ctx_usable = 0
    details = []
    for r in rows:
        if agent:
            from maia.agent.intents import detect_intent
            pred = detect_intent(r["question"])
            hit = 1 if pred == r.get("intent", "general") else 0
            m = {"hit": hit, "recall": float(hit), "mrr": 0.0, "prec": 1.0,
                 "faith": 1.0, "rel": 1.0, "refused": False,
                 "expect_refusal": False, "leaked": None, "contact_usable": None}
        else:
            res = query(r["question"], top_k_final=top_k)
            m = _eval_row(r, res, top_k)
        hits += m["hit"]; recalls += m["recall"]; precs += m["prec"]
        faiths += m["faith"]; rels += m["rel"]; mrrs += m["mrr"]
        if not m["expect_refusal"] and m["refused"]:
            false_refusals += 1
        if m["expect_refusal"]:
            refusal_cases += 1
            refusal_correct += 1 if m["refused"] else 0
        if m["leaked"] is not None:
            leak_cases += 1
            leak_hits += 1 if m["leaked"] else 0
        if m.get("contact_usable") is not None:
            contact_cases += 1
            contact_usable += m["contact_usable"]
        if m.get("contact_in_ctx") is not None:
            ctx_cases += 1
            ctx_usable += m["contact_in_ctx"]
        details.append({"q": r["question"][:60], "hit": m["hit"],
                        "recall": round(m["recall"], 3), "ctx_prec": round(m["prec"], 3),
                        "refused": m["refused"],
                        "has_evidence": m["has_evidence"],
                        "contact_usable": m.get("contact_usable"),
                        "contact_in_ctx": m.get("contact_in_ctx")})
    n = max(1, len(rows))
    rep = {"n": len(rows), "hit@k": round(hits / n, 3), "recall@k": round(recalls / n, 3),
           "context_precision": round(precs / n, 3), "faithfulness_proxy": round(faiths / n, 3),
           "relevance_proxy": round(rels / n, 3), "mrr": round(mrrs / n, 3)}
    if agent:
        rep = {"n": len(rows), "intent_accuracy": round(hits / n, 3), "details": details}
        return rep
    rep["false_refusal_rate"] = round(false_refusals / n, 3)
    rep["refusal_accuracy"] = round(refusal_correct / refusal_cases, 3) if refusal_cases else None
    rep["leakage_rate"] = round(leak_hits / leak_cases, 3) if leak_cases else None
    # G-04-FU2: None for groups without expect_contact rows (not 0.0 — no signal).
    rep["contact_usability_rate"] = (
        round(contact_usable / contact_cases, 3) if contact_cases else None
    )
    # CI-gateable CONDITIONAL invariant (LLM-independent): among retrieved rows,
    # did the role contact survive ingest redaction and reach the context?
    # None when no retrieved row carries expect_contact (no signal, not 0.0).
    rep["contact_context_rate"] = (
        round(ctx_usable / ctx_cases, 3) if ctx_cases else None
    )
    rep["details"] = details
    return rep


def contact_gate(report: dict, threshold: float,
                 group: str = "contact_usability") -> tuple[bool, str]:
    """CI gate for G-04-FU2: fail the build if role contacts don't survive.

    Gates on ``contact_context_rate`` (retrieved context), NOT the answer-level
    ``contact_usability_rate`` — the mock LLM never echoes context, so the
    answer-level metric is only meaningful with real generation.
    """
    rate = report.get("contact_context_rate")
    if rate is None:
        return False, f"group '{group}': no contact signal (no expect_contact rows)"
    if rate < threshold:
        return False, f"group '{group}': contact_context_rate={rate} < {threshold}"
    return True, f"group '{group}': contact_context_rate={rate} >= {threshold}"


GOLDEN_DIR = Path(__file__).resolve().parents[2] / "eval" / "golden"


def evaluate_all(top_k: int = 3, agent: bool = False) -> dict:
    """Run evaluation for every golden-group file and report per group."""
    out = {}
    for f in sorted(GOLDEN_DIR.glob("*.jsonl")):
        out[f.stem] = evaluate_group(str(f), top_k=top_k, agent=agent)
    return out


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
    import argparse
    p = argparse.ArgumentParser(description="MAIA golden-set evaluation (per group)")
    p.add_argument("dataset", nargs="?", default=None,
                   help="path to a .jsonl dataset (optional when --all/--group used)")
    p.add_argument("--group", help="evaluate one golden group by name (file in eval/golden/)")
    p.add_argument("--all", action="store_true", help="evaluate every golden group")
    p.add_argument("--agent", action="store_true", help="run enterprise agent intent eval")
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--fail-under-contact", type=float, default=None, metavar="RATE",
                   help="CI gate (G-04-FU2): exit 1 unless contact_context_rate >= RATE "
                        "on the contact_usability split (e.g. 0.8)")
    p.add_argument("--manifest", default=None, metavar="DIR",
                   help="WS7: write a run manifest JSON (git sha, mode, thresholds, "
                        "flags, metrics) into DIR for reproducibility")
    args = p.parse_args()

    def _emit(out: dict, dataset: str, top_k: int):
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if args.manifest:
            from maia.eval_manifest import build_manifest, write_manifest
            m = build_manifest(metrics=out, dataset=dataset, top_k=top_k)
            path = write_manifest(m, args.manifest)
            print(f"manifest: {path}", file=sys.stderr)

    if args.all:
        out = evaluate_all(top_k=args.top_k, agent=args.agent)
        _emit(out, "eval/golden/ (all groups)", args.top_k)
        if args.fail_under_contact is not None:
            rep = out.get("contact_usability")
            if rep is None:
                print("contact gate: contact_usability split missing", file=sys.stderr)
                raise SystemExit(1)
            ok, msg = contact_gate(rep, args.fail_under_contact)
            print(f"contact gate: {msg}", file=sys.stderr)
            raise SystemExit(0 if ok else 1)
    elif args.group:
        path = GOLDEN_DIR / f"{args.group}.jsonl"
        rep = evaluate_group(str(path), top_k=args.top_k, agent=args.agent)
        _emit(rep, str(path), args.top_k)
        if args.fail_under_contact is not None:
            if args.group != "contact_usability":
                print("contact gate: --fail-under-contact only applies to the "
                      "contact_usability split", file=sys.stderr)
                raise SystemExit(1)
            ok, msg = contact_gate(rep, args.fail_under_contact)
            print(f"contact gate: {msg}", file=sys.stderr)
            raise SystemExit(0 if ok else 1)
    else:
        ds = args.dataset or str(GOLDEN_DIR / "vi_policy.jsonl")
        print(json.dumps(evaluate_group(ds, top_k=args.top_k, agent=args.agent),
                         ensure_ascii=False, indent=2))
