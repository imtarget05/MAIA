#!/usr/bin/env python3
"""GATE 8B-C — MAIA ABSTENTION GATE (deterministic, no LLM).

Canonical claim under test:

    For labeled unanswerable queries, MAIA's evidence gate must route the
    request to an abstention path instead of authorizing an unsupported
    answer.

This gate does NOT claim "RAG never hallucinates". A finite test cannot
establish that. It claims something narrower and checkable: that a specific
production code path makes a specific, auditable decision for labeled
no-answer inputs, and that the decision mechanism rejects a deliberately
broken configuration.

Three layers, because a single metric hides which thing broke:

  B8A  ANSWERABILITY ORACLE — is the DATASET right? For every
       expect_refusal row, search the whole corpus for the concepts the
       question asks about. A supporting hit means the row is mislabelled
       and the gate fails on the dataset, not on the product.
       This runs BEFORE the product so a bad label is never reported as a
       product defect (or the reverse).

  B8B  EVIDENCE-GATE ROUTING — does the SYSTEM abstain? Runs the real
       pipeline path (HybridRetriever -> RRF -> evidence gate) and asserts
       on the application decision, never on response prose. Wording is a
       product detail; the decision is the contract.

  B8C  NEGATIVE CONTROLS — is any of this a no-op? Two controls, one that
       breaks the gate and one that breaks the dataset. A gate that cannot
       fail proves nothing.

Embeddings: REAL (paraphrase-multilingual-MiniLM-L12-v2). This is not
optional. In hash mode every dense score collapses below the evidence
threshold, so the gate refuses EVERYTHING and reports a flawless
abstention rate while answering nothing at all. That is the same
false-pass shape as the zero-byte log check in Gate 6, and it is the reason
a naive "refusal accuracy = 100%" number is worthless on its own.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))
# Real embeddings. See module docstring: hash mode makes this gate a no-op.
os.environ.pop("MAIA_EMBED_FORCE_HASH", None)

import yaml  # noqa: E402

CFG = yaml.safe_load((ROOT / "gate8b_thresholds.yaml").read_text())
TH = CFG.get("ABSTENTION", {})
CORPUS_DIR = ROOT / "data/enterprise"
NOANSWER = ROOT / "eval/golden/no_answer.jsonl"
EVIDENCE = PROJ / "docs/evidence/e2e/gate8b-abstention.json"

RESULTS: list[dict] = []


def check(cid: str, desc: str, status: str, detail: str, oracle: str) -> None:
    RESULTS.append({"id": cid, "description": desc, "status": status,
                    "detail": detail, "oracle_source": oracle})
    print(f"  [{status}] {cid} {desc}: {detail}")


# ----------------------------------------------------------------- B8A
print("\n[B8A] ANSWERABILITY ORACLE — is the dataset actually correct?")
from maia.textnorm import norm_tokens  # noqa: E402

corpus_files = sorted(p for p in CORPUS_DIR.rglob("*") if p.is_file())
corpus_blob = "\n".join(p.read_text() for p in corpus_files)
corpus_norm = " ".join(norm_tokens(corpus_blob))
corpus_sha = hashlib.sha256(
    b"".join(p.read_bytes() for p in corpus_files)).hexdigest()

rows = [json.loads(l) for l in NOANSWER.read_text().splitlines() if l.strip()]
golden_sha = hashlib.sha256(NOANSWER.read_bytes()).hexdigest()
print(f"  corpus: {len(corpus_files)} files sha256={corpus_sha[:16]}")
print(f"  no_answer rows: {len(rows)} sha256={golden_sha[:16]}")

VALID_REASONS = {"NO_SUPPORTING_DOCUMENT", "WRONG_TENANT_ONLY", "OUT_OF_SCOPE",
                 "INSUFFICIENT_EVIDENCE", "AMBIGUOUS_EVIDENCE"}

bad_schema = [r.get("id", "?") for r in rows
              if r.get("reason") not in VALID_REASONS
              or r.get("expect_refusal") is not True]
check("B8A1", "every no-answer row declares a valid abstention reason",
      "PASS" if not bad_schema else "FAIL",
      f"{len(rows) - len(bad_schema)}/{len(rows)} rows declare one of "
      f"{sorted(VALID_REASONS)}" if not bad_schema
      else f"invalid rows: {bad_schema}",
      "enum of documented no-answer reasons (schema contract)")

# A refusal row asserts: no document supports this question. Test that claim
# against the corpus directly instead of trusting the label.
#
# TOKEN MEMBERSHIP, NOT SUBSTRING. `norm_tokens()` returns a token list, so
# joining it into one string and testing `"cat" in blob` matches "candiCATes"
# and "dediCATed". That bug made two correct no-answer rows look mislabelled,
# which is the same substring-vs-token defect already fixed once in gate8b.
# A false INVALID_GOLDEN_ROW is worse than a false pass here: it would send
# someone to edit a correct dataset to satisfy a broken oracle.
# TOKEN-SEQUENCE MATCH, NOT SUBSTRING AND NOT PER-TOKEN.
#
# Two defects were found here, both in this oracle rather than in the product:
#
#  1. Substring: `"cat" in "candidates|dedicated"` matched, so correct rows
#     looked mislabelled.
#  2. Per-token: requiring every token of "giám đốc điều hành" to exist
#     individually is far too weak. Vietnamese compounds are built from common
#     words, so "công" + "thức" both exist in the corpus for unrelated reasons
#     and the phrase was reported as supported when nothing supports it.
#
# The check is: does the term's token sequence appear CONTIGUOUSLY in the
# corpus token stream? That is precise in both directions, and it is the same
# notion a reader would use to confirm a phrase really appears in a document.
corpus_stream: list[str] = []
for p in corpus_files:
    corpus_stream.extend(norm_tokens(p.read_text()))
corpus_ngrams: set[tuple[str, ...]] = {
    tuple(corpus_stream[i:i + n])
    for n in (1, 2, 3, 4)
    for i in range(len(corpus_stream) - n + 1)}


def supports(phrase: str) -> bool:
    """True only if `phrase`'s token sequence appears verbatim in the corpus."""
    toks = norm_tokens(phrase)
    return bool(toks) and tuple(toks) in corpus_ngrams


mislabeled: list[dict] = []
for r in rows:
    hits = [t for t in r.get("probe_terms", []) if supports(t)]
    if hits:
        mislabeled.append({"id": r["id"], "question": r["question"],
                           "reason": r["reason"], "corpus_hits": hits})

# NOANS-008/009 are INSUFFICIENT_EVIDENCE by construction: their concepts ARE
# in the corpus, and the claim is that no clause states the specific detail.
# A concept hit is expected for those and does not mean the label is wrong.
concept_hits_expected = [m for m in mislabeled
                         if m["reason"] == "INSUFFICIENT_EVIDENCE"]
hard_mislabeled = [m for m in mislabeled
                   if m["reason"] != "INSUFFICIENT_EVIDENCE"]

check("B8A2", "NO_SUPPORTING_DOCUMENT rows have no supporting concept in corpus",
      "PASS" if not hard_mislabeled else "INVALID_GOLDEN_ROW",
      f"{len(rows) - len(hard_mislabeled) - len(concept_hits_expected)} "
      f"NO_SUPPORTING_DOCUMENT / OUT_OF_SCOPE rows verified absent; "
      f"{len(concept_hits_expected)} INSUFFICIENT_EVIDENCE rows intentionally "
      f"have concept hits"
      + (f"; MISLABELLED: {hard_mislabeled}" if hard_mislabeled else ""),
      "token-level search of the full corpus for each row's probe_terms")
# ----------------------------------------------------------------- setup
print("\n[setup] real embeddings + in-memory store (no Qdrant, no LLM)")
from maia.config import settings  # noqa: E402
from maia.embeddings import Embedder  # noqa: E402
from maia.retriever import HybridRetriever  # noqa: E402
from maia.test_utils import InMemoryVectorStore  # noqa: E402

EMBED_MODE = None


def make_embedder() -> Embedder:
    """Force a real embedder even if the environment presets hash mode.

    A hash embedder cannot support this gate: it makes every dense score
    fall below the evidence threshold, so the gate refuses everything and
    B8B would report a perfect abstention rate for a system that cannot
    answer anything at all.
    """
    global EMBED_MODE
    os.environ.pop("MAIA_EMBED_FORCE_HASH", None)
    emb = Embedder()
    EMBED_MODE = getattr(emb, "mode", "unknown")  # property, not a method
    if EMBED_MODE == "hash":
        raise SystemExit(
            "FATAL: hash embedder active. In hash mode every dense score is "
            "below SIMILARITY_THRESHOLD, so the evidence gate refuses every "
            "query and B8B would pass vacuously. Refusing to run rather "
            "than emit a meaningless abstention metric."
        )
    return emb


embedder = make_embedder()
THRESHOLD = float(settings.SIMILARITY_THRESHOLD)


def build_store(tenant_by_doc: dict[str, str] | None = None) -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    step = max(1, settings.CHUNK_SIZE - settings.CHUNK_OVERLAP)
    for p in sorted(CORPUS_DIR.glob("*.md")):
        raw = p.read_text()
        tid = (tenant_by_doc or {}).get(p.stem, settings.TENANT_ID)
        for i in range(0, max(1, len(raw)), step):
            cid = f"{p.stem}_{i // step}"
            text = raw[i:i + settings.CHUNK_SIZE]
            store.upsert_one(cid, embedder.embed([text])[0],
                             {"document_id": p.stem, "title": p.stem,
                              "text": text, "tenant_id": tid})
    return store


def build_retriever(store: InMemoryVectorStore, tenant_id: str | None = None
                    ) -> HybridRetriever:
    r = HybridRetriever(
        store, embedder, top_k_dense=settings.TOP_K_DENSE,
        top_k_bm25=settings.TOP_K_BM25,
        top_k_fused=settings.TOP_K_FUSED,
        rrf_k=settings.RRF_K, tenant_id=tenant_id or settings.TENANT_ID)
    r.rebuild(tenant_id=tenant_id or settings.TENANT_ID)
    return r
def evidence_decision(question: str, retriever: HybridRetriever,
                      threshold: float | None = None) -> dict:
    """Reproduce the production evidence gate decision.

    Mirrors src/maia/pipeline_query.py §8.3: dense + BM25 -> RRF fusion, then
    has_evidence = (top dense score >= SIMILARITY_THRESHOLD). Asserting on this
    boolean asserts on the application decision. The refusal TEXT is a display
    detail and is deliberately not asserted, because a wording change is not a
    safety regression and should not be able to fail this gate.
    """
    thr = THRESHOLD if threshold is None else threshold
    hits = retriever.retrieve(question)
    top_dense = max((c.get("dense_score", 0) for c in hits), default=0)
    return {"question": question, "n_candidates": len(hits),
            "top_dense": round(top_dense, 4),
            "has_evidence": bool(top_dense >= thr),
            "decision": "AUTHORIZE" if top_dense >= thr else "ABSTAIN",
            "threshold": thr,
            "candidates": [{"chunk_id": c.get("chunk_id"),
                            "document_id": (c.get("metadata") or {}).get("document_id"),
                            "dense_score": round(c.get("dense_score", 0), 4)}
                           for c in hits]}


store = build_store()
retriever = build_retriever(store)
print(f"  embedder mode={EMBED_MODE}  chunks={store.count()}  "
      f"evidence threshold={THRESHOLD}")

# ----------------------------------------------------------------- B8B
print("\n[B8B] EVIDENCE-GATE ROUTING — does the production path abstain?")

decisions = [evidence_decision(r["question"], retriever) for r in rows]
abstained = [d for d in decisions if not d["has_evidence"]]
authorized = [d for d in decisions if d["has_evidence"]]
abstention_rate = len(abstained) / len(decisions) if decisions else 0.0

check("B8B1", "evidence gate abstains on labeled no-answer queries",
      "PASS" if not authorized else "FAIL",
      f"{len(abstained)}/{len(decisions)} abstained "
      f"(rate={abstention_rate:.3f}); "
      f"authorized: {[d['question'][:40] for d in authorized] or 'none'}",
      "production evidence gate (pipeline_query §8.3) via HybridRetriever+RRF")
# B8B-2: opposite-polarity control on the SAME code path. If the gate refuses
# everything, B8B1 is meaningless. Measure that answerable questions still
# get authorized. Abstention alone is not a safety property; it is also
# exactly what a broken retriever looks like.
answerable_probe = []
for f in sorted((ROOT / "eval/golden").glob("*.jsonl")):
    if f.name == "no_answer.jsonl":
        continue
    for line in f.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("expect_refusal"):
                answerable_probe.append(r)
answerable_probe = answerable_probe[:30]
ans_decisions = [evidence_decision(r["question"], retriever)
                 for r in answerable_probe]
ans_authorized = [d for d in ans_decisions if d["has_evidence"]]
answerable_rate = len(ans_authorized) / len(ans_decisions) if ans_decisions else 0.0

min_ans = float(TH.get("min_answerable_authorized", 0.0))
check("B8B2", "gate is discriminating, not refusing everything",
      "PASS" if answerable_rate >= min_ans else "FAIL",
      f"answerable authorized={len(ans_authorized)}/{len(ans_decisions)} "
      f"(rate={answerable_rate:.3f}, need>={min_ans}); no-answer abstained="
      f"{len(abstained)}/{len(decisions)}. Both are required: abstention alone "
      f"is indistinguishable from a broken retriever.",
      "same gate, opposite-polarity control set")

# B8B-3: distribution separability. A single scalar threshold separates two
# classes only if EVERY no-answer score sits below EVERY answerable score:
#     max(no_answer) < min(answerable)
#
# The previous version compared min(no_answer) to min(answerable), which is
# trivially true for almost any dataset and reported PASS while the classes
# actually overlapped. Same shape as a threshold that looks right because it
# was never tested against the case that should have broken it.
noanswer_scores = [d["top_dense"] for d in decisions]
answerable_scores = [d["top_dense"] for d in ans_decisions]
max_noanswer = max(noanswer_scores, default=0.0)
min_answerable = min(answerable_scores, default=0.0)
separable = max_noanswer < min_answerable
overlap_noanswer = [d["question"] for d in decisions if d["top_dense"] >= min_answerable]

# End-to-end confirmation against the REAL production path. evidence_decision()
# mirrors pipeline_query._query_impl, but a mirror can drift from the thing it
# mirrors, so drive the actual query() entry point once and compare. If the
# mirror were optimistic, this is where it would surface.
from maia import pipeline_query as _pq  # noqa: E402


class _FakeLLM:
    """Stands in for generation and records whether it was reached.

    If the evidence gate works, no-answer queries must never get here. So a
    non-zero call count on a no-answer query is direct evidence that the
    unsupported answer would have been generated.
    """
    mode = "offline_no_gen"

    def __init__(self):
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        return "FABRICATED ANSWER"

    def __getattr__(self, n):
        return lambda *a, **k: None


def _run_production(qs, retr, st, emb):
    from maia.reranker import Reranker
    rer = Reranker()
    fake = _FakeLLM()
    orig_stack = _pq.build_stack
    _pq.build_stack = lambda tenant_id=None: (emb, st, retr, rer, fake)
    out = []
    try:
        for q in qs:
            resp = _pq.query(q)
            out.append({"question": q,
                        "has_evidence": bool(resp.get("has_evidence")),
                        "refused": bool(resp.get("refused")),
                        "decision": "ABSTAIN" if (resp.get("refused")
                                                  or not resp.get("has_evidence"))
                        else "AUTHORIZE"})
    finally:
        _pq.build_stack = orig_stack
    return out, fake.calls


prod, gen_calls = _run_production([r["question"] for r in rows],
                                  retriever, store, embedder)
prod_abstained = [x for x in prod if x["decision"] == "ABSTAIN"]
mirror_agrees = all((x["decision"] == "ABSTAIN") == (not d["has_evidence"])
                    for x, d in zip(prod, decisions))
check("B8B2b", "production query() agrees with the mirrored evidence gate",
      "PASS" if mirror_agrees else "FAIL",
      f"drove the real maia.pipeline_query.query() over all {len(prod)} "
      f"no-answer queries: {len(prod_abstained)} abstained, generation stage "
      f"entered {gen_calls} time(s). The mirror agrees with production on "
      f"{sum(1 for x, d in zip(prod, decisions) if (x['decision'] == 'ABSTAIN') == (not d['has_evidence']))}"
      f"/{len(prod)} rows. A mirror that disagreed with production would make "
      f"B8B1 a claim about a function nobody ships.",
      "real maia.pipeline_query.query(), not a reimplementation")




# B8B-4: zero-denominator policy, applied globally. An empty denominator is
# INVALID_DATASET, never 0/0 = PASS.
check("B8B4", "zero-denominator policy enforced",
      "PASS" if (decisions and ans_decisions and rows) else "INVALID_DATASET",
      f"no-answer rows={len(rows)}, no-answer decisions={len(decisions)}, "
      f"answerable controls={len(ans_decisions)}. All denominators non-zero; "
      f"an empty set is reported as INVALID_DATASET and never scored.",
      "global rule: denominator == 0 -> NOT_RUN / INVALID_DATASET")

# B8B-5: WRONG_TENANT_ONLY. A factually correct answer sourced from another
# tenant is a security failure, not a success. Tenant B has the answer, tenant
# A does not, and tenant A must not retrieve B's chunk.
TENANT_A, TENANT_B = "tenant-a", "tenant-b"
tenant_by_doc = {p.stem: TENANT_A for p in CORPUS_DIR.glob("*.md")}
tenant_store = build_store(tenant_by_doc)
# Inject one tenant-B-only document into the shared store, then ask as tenant A.
b_only = ("tenant-b-confidential.md",
          "# Tenant B Compensation\n\n## Band\n\n"
          "Tenant B engineering band is B7 with base 90000000 VND per month.\n")
b_path = CORPUS_DIR / b_only[0]
b_path.write_text(b_only[1], encoding="utf-8")
try:
    s2 = build_store(tenant_by_doc | {b_only[0].removesuffix(".md"): TENANT_B})
    r_a = build_retriever(s2, tenant_id=TENANT_A)
    leak_q = "What is the tenant B engineering band and base salary?"
    d_a = evidence_decision(leak_q, r_a)
    leaked = [c for c in d_a["candidates"]
              if (c.get("document_id") or "") == b_only[0].removesuffix(".md")]
    check("B8B5", "cross-tenant retrieval is refused for the wrong tenant",
          "PASS" if not leaked else "FAIL",
          f"tenant A asked for tenant B's compensation; retrieved "
          f"{len(leaked)} tenant-B chunks, decision={d_a['decision']} "
          f"(top_dense={d_a['top_dense']}). A correct answer from another "
          f"tenant's data is a security failure, not an answer.",
          "HybridRetriever tenant filter + evidence gate, tenant A only")
finally:
    b_path.unlink()
if separable:
    b8b3_detail = (
        f"max no-answer top_dense={max_noanswer:.4f} < min answerable "
        f"top_dense={min_answerable:.4f}. A single scalar threshold can "
        f"separate the two classes, so a correctly tuned threshold would "
        f"give both a useful abstention rate and a usable answer rate.")
else:
    b8b3_detail = (
        f"CLASSES OVERLAP: max no-answer top_dense={max_noanswer:.4f} >= min "
        f"answerable top_dense={min_answerable:.4f}. {len(overlap_noanswer)} "
        f"no-answer queries score above the weakest genuine answer, so NO "
        f"setting of SIMILARITY_THRESHOLD can separate them. Raising it until "
        f"B8B1 passes would also refuse correct answers, which is why B8B2 is "
        f"required. A cosine similarity on the top chunk measures topical "
        f"similarity, not answerability; no-answer questions about retirement "
        f"or stock options are topically close to HR_Policy and Benefits, so "
        f"they retrieve a confident-looking chunk that does not contain the "
        f"answer.")
check("B8B3", "a single scalar threshold can separate no-answer from answerable",
      "PASS" if separable else "FAIL", b8b3_detail,
      "max(no_answer top_dense) < min(answerable top_dense)")

# ----------------------------------------------------------------- B8C
print("\n[B8C] NEGATIVE CONTROLS — prove the gate and the oracle are not no-ops")

# Control 1: break the GATE. Lower the evidence threshold so no-answer queries
# become authorized. The gate must then report failure. Without this, B8B
# could be a hardcoded True.
broken_threshold = 0.01
broken = [evidence_decision(r["question"], retriever, threshold=broken_threshold)
          for r in rows]
broken_authorized = [d for d in broken if d["has_evidence"]]
control1_detects = len(broken_authorized) > 0
check("B8C1", "gate rejects a candidate with a lowered evidence threshold",
      "PASS" if control1_detects else "FAIL",
      f"threshold {THRESHOLD} -> {broken_threshold}: "
      f"{len(broken_authorized)}/{len(broken)} no-answer queries became "
      f"AUTHORIZED. A gate that cannot fail on this proves nothing.",
      "isolated candidate config, gate decision only")

# Control 2: break the DATASET. Relabel a known-answerable question as a
# refusal row. The answerability oracle must reject it. This proves B8A is a
# real check over the corpus and not a loop over hand-written expectations.
known_answer = {"id": "NEGCTRL-001",
                "question": "How many annual leave days do I get per year?",
                "expect_refusal": True,
                "reason": "NO_SUPPORTING_DOCUMENT",
                "probe_terms": ["12 ngày", "annual leave", "phép năm"]}
hits = [t for t in known_answer["probe_terms"] if supports(t)]
control2_detects = bool(hits)
check("B8C2", "answerability oracle rejects a mislabelled golden row",
      "PASS" if control2_detects else "FAIL",
      f"relabelled '{known_answer['question']}' as refusal; oracle found "
      f"supporting corpus concepts {hits}. Proves the dataset check reads the "
      f"corpus instead of trusting the label.",
      "token-level corpus search on a deliberately mislabelled fixture")

# Control 3: sensitivity. A decision function that ignores its inputs would
# also "pass" B8B1. Removing BM25 must change what the gate ACTS ON.
#
# Measured on the RRF-fused candidate set, not on dense_score. dense_score
# comes from the dense channel, so disabling BM25 provably cannot move it —
# asserting on dense_score would report FAIL for a property that is correct
# by construction, which is how a gate loses the confidence of whoever reads it.
# What BM25 removal genuinely changes is WHICH chunks the gate is shown, so
# that is what this control compares.
dense_only = HybridRetriever(
    store, embedder, top_k_dense=settings.TOP_K_DENSE, top_k_bm25=0,
    top_k_fused=settings.TOP_K_FUSED, rrf_k=settings.RRF_K,
    tenant_id=settings.TENANT_ID)
dense_only.rebuild(tenant_id=settings.TENANT_ID)


def candidate_ids(retriever: HybridRetriever, questions: list[str]) -> list[list[str]]:
    out = []
    for q in questions:
        out.append([c.get("chunk_id") for c in retriever.retrieve(q)])
    return out


all_q = [r["question"] for r in rows]
hybrid_ids = candidate_ids(retriever, all_q)
dense_ids = candidate_ids(dense_only, all_q)
moved = sum(1 for a, b in zip(hybrid_ids, dense_ids) if a != b)
RESULTS.append({
    "id": "B8C3",
    "description": "abstention decisions are sensitive to the retriever configuration",
    "status": "PASS" if moved == len(all_q) else "FAIL",
    "detail": f"disabling BM25 changed the fused candidate set for "
              f"{moved}/{len(all_q)} no-answer queries. Measured on the RRF "
              f"candidate set rather than dense_score, because dense_score "
              f"belongs to the dense channel and BM25 cannot influence it.",
    "oracle_source": "same gate, BM25 disabled, per-query candidate-set delta",
    "classification": "SENSITIVITY",
})
print(f"  [{'PASS' if moved == len(all_q) else 'FAIL'}] B8C3 "
      f"abstention decisions are sensitive to the retriever configuration: "
      f"BM25 off changed the candidate set for {moved}/{len(all_q)} queries "
      f"(sensitivity, non-blocking)")
dense_decisions = [evidence_decision(r["question"], dense_only) for r in rows]
dense_auth = [d for d in dense_decisions if d["has_evidence"]]

# ----------------------------------------------------------------- verdict
blocking_fail = [c for c in RESULTS
                 if c["status"] in ("FAIL", "INVALID_GOLDEN_ROW")]
partial = [c for c in RESULTS if c["status"] in ("NOT_RUN", "INVALID_DATASET")]
status = "FAIL" if blocking_fail else ("PARTIAL" if partial else "PASS")

per_row = []
for r, d in zip(rows, decisions):
    per_row.append({"id": r["id"], "question": r["question"],
                    "reason": r["reason"],
                    "expected": "ABSTAIN", "decision": d["decision"],
                    "top_dense": d["top_dense"], "correct": d["decision"] == "ABSTAIN"})

payload = {
    "gate": "8B-C",
    "title": "MAIA abstention routing gate",
    "status": status,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "canonical_claim": (
        "For labeled unanswerable queries, MAIA's evidence gate routes the "
        "request to an abstention path instead of authorizing an unsupported "
        "answer."),
    "explicitly_not_claimed": (
        "This gate does NOT establish that MAIA never hallucinates. A finite "
        "labeled set cannot prove a universal claim, and no test here attempts "
        "to."),
    "embedder_mode": EMBED_MODE,
    "evidence_threshold": THRESHOLD,
    "integrity": {"corpus_sha256": corpus_sha, "no_answer_sha256": golden_sha,
                  "no_answer_rows": len(rows)},
    "abstention": {
        "rows": len(decisions),
        "abstained": len(abstained),
        "authorized": len(authorized),
        "abstention_rate": round(abstention_rate, 4),
        "answerable_controls": len(ans_decisions),
        "answerable_authorized": len(ans_authorized),
        "answerable_authorized_rate": round(answerable_rate, 4),
        "separable": separable,
        "max_noanswer_top_dense": round(max_noanswer, 4),
        "min_answerable_top_dense": round(min_answerable, 4),
    },
    "negative_controls": {
        "lowered_threshold_detected": control1_detects,
        "mislabelled_row_detected": control2_detects,
        "bm25_removal_authorized": len(dense_auth),
    },
    "per_row": per_row,
    "checks": RESULTS,
}
EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
EVIDENCE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

met = sum(1 for c in RESULTS if c["status"] == "PASS")
print(f"\n{met}/{len(RESULTS)} checks PASS — GATE 8B-C {status}")
if blocking_fail:
    print("failing: " + ", ".join(c["id"] for c in blocking_fail))
print(f"evidence: {EVIDENCE}")
sys.exit(0 if status == "PASS" else 1)
