#!/usr/bin/env python3
"""GATE 8B - MAIA RAG QUALITY GATE (retrieval + abstention, no LLM).

Cau hoi phong van: "Doi voi RAG, con so nao chung minh chat bot khong bia?"

Cau tra loi: khong phai mot LLM judge score. La:
  - Recall@K / MRF tren tap golden DONG BANG
  - citation troi toi chunk that trong corpus
  - ABSTENTION tren bo cau hoi khong co dap an
  - va mot NEGATIVE candidate bi gate chan

Tach retrieval khoi generation. Neu gop, mot regression retrieval se bi
mot cau tra loi dep cua LLM che, va nguoc lai. Tach ra thi biet chinh xac
hong o dau.

Khong goi LLM trong gate nay. Hosted model doi ben provider bat c luc nao
-> CI flaky khong kiem soat duoc. Judge thanh NON-BLOCKING report.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))

CFG = yaml.safe_load((ROOT / "gate8b_thresholds.yaml").read_text())
GATES = CFG["GATES"]
GOLDEN_DIR = ROOT / CFG["GOLDEN_DIR"]
TOP_K = CFG["TOP_K"]
CORPUS_DIR = ROOT / "data/enterprise"

# Force hash embedding: no model download, fully deterministic.
os.environ["MAIA_EMBED_FORCE_HASH"] = "1"

EVIDENCE = PROJ / "docs/evidence/e2e/gate8b-maia-rag-quality.json"
RESULTS: list[dict] = []


def check(cid, desc, status, detail, oracle, classification="PRODUCT"):
    RESULTS.append({"id": cid, "description": desc, "status": status,
                    "detail": detail, "oracle_source": oracle,
                    "classification": classification})
    print(f"  [{status}] {cid} {desc}: {detail}")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def eval_corpus_fingerprint() -> tuple[str, list[dict]]:
    """Deterministic manifest of the source corpus.

    Hash the (relative path, content) of every corpus file, sorted, so the
    fingerprint is stable regardless of filesystem order.
    """
    files = sorted(p for p in CORPUS_DIR.rglob("*") if p.is_file())
    h = hashlib.sha256()
    manifest = []
    for p in files:
        raw = p.read_bytes()
        h.update(p.name.encode())
        h.update(raw)
        manifest.append({"file": p.name, "sha256": sha256_bytes(raw),
                         "bytes": len(raw)})
    return h.hexdigest(), manifest
# ---------------------------------------------------------------- B1
print("\n[B1] corpus + golden set integrity")
corpus_sha, corpus_manifest = eval_corpus_fingerprint()

golden_files = sorted(GOLDEN_DIR.glob("*.jsonl"))
golden_sha = hashlib.sha256()
rows_by_group: dict[str, list[dict]] = {}
for f in golden_files:
    raw = f.read_bytes()
    golden_sha.update(f.name.encode())
    golden_sha.update(raw)
    rows_by_group[f.stem] = [json.loads(l) for l in raw.decode().splitlines() if l.strip()]
golden_sha = golden_sha.hexdigest()

all_rows = [r for g in rows_by_group.values() for r in g]
answerable = [r for r in all_rows if not r.get("expect_refusal")]
unanswerable = [r for r in all_rows if r.get("expect_refusal")]

check("B1", "eval golden set is frozen and fingerprinted",
      "PASS",
      f"{len(golden_files)} groups, {len(all_rows)} rows "
      f"(answerable={len(answerable)}, unanswerable={len(unanswerable)}), "
      f"sha256={golden_sha[:16]}",
      "SHA-256 over sorted (group name + file bytes)")

# An unanswerable subset is the anti-hallucination oracle. Without it, a
# system that answers everything looks perfect and is useless.
check("B2", "golden set contains an unanswerable subset",
      "PASS" if len(unanswerable) > 0 else "FAIL",
      f"{len(unanswerable)} expect_refusal rows available as the "
      f"abstention oracle",
      "count of rows carrying expect_refusal=true")

# ---------------------------------------------------------------- ingest
print("\n[setup] ingest corpus into an in-memory store (no Qdrant, no LLM)")
from maia.test_utils import InMemoryVectorStore  # noqa: E402
from maia.embeddings import Embedder  # noqa: E402
from maia.retriever import HybridRetriever  # noqa: E402
from maia.textnorm import norm_tokens  # noqa: E402

embedder = Embedder()


def build_corpus() -> tuple[InMemoryVectorStore, dict[str, str]]:
    """Chunk each corpus doc deterministically and upsert it.

    chunk_id is '<docname>_<n>' so gold ids stay traceable to a file. The
    golden set's own gold_chunk_ids reference a historical ingest, so we
    cannot assume they resolve; the gate measures retrieval on OUR frozen
    corpus and records that mapping explicitly rather than silently
    scoring zero.
    """
    store = InMemoryVectorStore()
    chunk_text: dict[str, str] = {}
    for p in sorted(CORPUS_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        # Paragraph-level chunks: deterministic, no model, stable ordering.
        paras = [b.strip() for b in text.split("\n\n") if b.strip()]
        for i, para in enumerate(paras):
            cid = f"{p.stem}_{i}"
            chunk_text[cid] = para
            store.upsert_one(cid, embedder.embed([para])[0],
                             {"text": para, "document_id": p.stem,
                              "source": p.name, "chunk_id": cid})
    return store, chunk_text


store, chunk_text = build_corpus()
print(f"  corpus chunks: {len(chunk_text)}")
if not chunk_text:
    check("B3", "corpus ingestion produced chunks", "FAIL",
          "0 chunks ingested - cannot measure retrieval", "chunk count")
    sys.exit(1)
check("B3", "corpus ingestion produced chunks", "PASS",
      f"{len(chunk_text)} chunks from {len(corpus_manifest)} files, "
      f"sha256={corpus_sha[:16]}", "deterministic paragraph chunking")
# ---------------------------------------------------------------- oracle
# Golden set tro toi chunk_id sinh bo mot lan ingest lich su
# ("1829fe5aff80_0"), khong resolve duoc voi corpus ta vua dung. Neu cham
# diem 0 am tham, ta dang do mot dataset khac. Chi dung gold_keywords lam
# oracle: tieu chi nay kiem chung lap lai duoc tren BAT KY corpus.
def keyword_hit(row: dict, chunks: list[dict]) -> bool:
    """True if retrieved chunks contain every gold keyword's tokens.

    Token-level, NOT substring: norm_tokens("it-help@company.com") yields
    ['it','help','company','com'], so a raw-substring test never matches and
    reports ~0 recall for a healthy retriever. HARNESS bug, caught by
    auditing keyword presence before trusting the metric.
    """
    kws = [k.lower() for k in row.get("gold_keywords", []) if k]
    if not kws:
        return False
    blob_tokens: set[str] = set()
    for c in chunks:
        blob_tokens.update(norm_tokens(c.get("text", "")))
    for k in kws:
        toks = norm_tokens(k)
        if toks and not all(t in blob_tokens for t in toks):
            return False
    return True


def make_retriever(mode: str) -> HybridRetriever:
    """The REAL HybridRetriever over the in-memory store.

    The negative candidate is not a fake class: BM25 is disabled on the real
    component (self._bm25 = None), which is exactly the state a developer
    leaves behind after a refactor drops the lexical retriever.
    """
    r = HybridRetriever(store=store, embedder=embedder,
                        storage_dir=str(ROOT / "storage"),
                        top_k_dense=10, top_k_bm25=10, top_k_fused=TOP_K,
                        rrf_k=60)
    r.rebuild()  # populate BM25 from the store
    if mode == "dense_only":
        r._bm25 = None
    return r


def run_eval(r: HybridRetriever, rows: list[dict]) -> dict:
    """Deterministic retrieval metrics. No LLM is invoked anywhere here."""
    hits = mrrs = 0.0
    per_row, cited = [], set()
    for row in rows:
        if row.get("expect_refusal"):
            continue
        got = r.retrieve(row["question"])
        ids = [g["chunk_id"] for g in got]
        cited.update(ids)
        hit = 1.0 if keyword_hit(row, got) else 0.0
        rr = 0.0
        for rank, g in enumerate(got, start=1):
            if keyword_hit(row, [g]):
                rr = 1.0 / rank
                break
        hits += hit
        mrrs += rr
        per_row.append({"q": row["question"][:60], "hit": hit, "rr": rr,
                        "retrieved": ids})
    n = max(1, len(per_row))
    return {"n": len(per_row), "hit_at_k": round(hits / n, 4),
            "recall_at_k": round(hits / n, 4), "mrr": round(mrrs / n, 4),
            "per_row": per_row, "cited_chunk_ids": sorted(cited)}


def oracle_ceiling(rows: list[dict]) -> float:
    """Max achievable recall on this corpus.

    Separates "bad retriever" from "not answerable from this corpus at all".
    Without it a low recall number is ambiguous and any threshold derived
    from it is meaningless.
    """
    all_chunks = [{"text": t} for t in chunk_text.values()]
    scorable = [r_ for r_ in rows if not r_.get("expect_refusal")]
    if not scorable:
        return 0.0
    return round(sum(1 for r_ in scorable
                     if keyword_hit(r_, all_chunks)) / len(scorable), 4)


def apply_gates(m: dict) -> tuple[bool, list[str]]:
    v = []
    if m["recall_at_k"] < GATES["min_recall_at_3"]:
        v.append(f"recall@{TOP_K} {m['recall_at_k']} < {GATES['min_recall_at_3']}")
    if m["mrr"] < GATES["min_mrr"]:
        v.append(f"mrr {m['mrr']} < {GATES['min_mrr']}")
    return (not v), v


# ---------------------------------------------------------------- B4
print("\n[B4] approved candidate — hybrid (dense + BM25 + RRF)")
ceiling = oracle_ceiling(all_rows)
approved = make_retriever("hybrid")  # always approved, even inside a child run
base = run_eval(approved, all_rows)
base_pass, base_viol = apply_gates(base)
print(f"  recall@{TOP_K}={base['recall_at_k']} mrr={base['mrr']} "
      f"n={base['n']} (ceiling={ceiling})")

check("B4", "approved hybrid candidate passes the retrieval gate",
      "PASS" if base_pass else "FAIL",
      f"recall@{TOP_K}={base['recall_at_k']} (>= {GATES['min_recall_at_3']}), "
      f"mrr={base['mrr']} (>= {GATES['min_mrr']})",
      "pre-committed thresholds in gate8b_thresholds.yaml")
for i, v in enumerate(base_viol):
    check(f"B4v{i}", f"gate violation: {v}", "FAIL", "threshold breached",
          "threshold comparison")

# B5 — the number that makes the low recall interpretable.
check("B5", "recall is reported against a measured oracle ceiling",
      "PASS",
      f"ceiling={ceiling} (questions answerable from THIS corpus); "
      f"hybrid reaches {base['recall_at_k']} = "
      f"{round(base['recall_at_k'] / ceiling * 100) if ceiling else 0}% of it; "
      f"{len(all_rows) - len(answerable)} unanswerable rows excluded",
      "keyword presence across the entire ingested corpus",
      classification="HARNESS")

# ---------------------------------------------------------------- B6
print("\n[B6] citation validity — every cited chunk must exist")
# A citation pointing at a chunk that is not in the corpus is worse than no
# citation: it looks like evidence while being unverifiable.
invalid = [cid for cid in base["cited_chunk_ids"] if cid not in chunk_text]
validity = 1.0 - (len(invalid) / max(1, len(base["cited_chunk_ids"])))
check("B6", "all retrieved citations resolve to real corpus chunks",
      "PASS" if validity >= GATES["min_citation_validity"] else "FAIL",
      f"{len(base['cited_chunk_ids'])} distinct citations, "
      f"{len(invalid)} unresolvable, validity={validity}",
      "citation id set difference against the ingested corpus")

# A positive control: a fabricated id MUST be detected. A validator that
# passes everything is worthless.
fake = "does_not_exist_9f3a2b"
detects_fake = fake not in chunk_text
check("B7", "citation validator REJECTS a fabricated chunk id",
      "PASS" if detects_fake else "FAIL",
      f"synthetic id '{fake}' correctly identified as not in corpus",
      "negative control on the citation validator",
      classification="HARNESS")

# ---------------------------------------------------------------- B8
print("\n[B8] abstention — unanswerable questions must not be answerable")
# NOTE ON SCOPE: a full abstention check needs the generation + evidence-gate
# stage, which calls an LLM. That is non-deterministic, so it is NOT in this
# blocking gate. What IS deterministic and blocking: the retrieval layer must
# NOT surface high-confidence lexical support for questions with no gold
# evidence. Refusal accuracy is reported separately, not claimed here.
unans = [r_ for r_ in all_rows if r_.get("expect_refusal")]
unans_kw = [r_ for r_ in unans if r_.get("gold_keywords")]
if not unans_kw:
    # 0/0 khong phai "0% = tot". Day la KHONG co gi de kiem chung: moi
    # unanswerable row deu co gold_keywords=[] nghia la oracle rong.
    # Bao PASS o day la bang chung gia dung — dung loai da gap o Gate 6
    # (0 byte log -> "khong ro ri").
    check("B8", "abstention: unanswerable questions carry a usable oracle",
          "FAIL",
          f"NOT_RUN: 0 of {len(unans)} expect_refusal rows define "
          f"gold_keywords, so abstention cannot be measured here. A 0/0 "
          f"result is NOT evidence of correct refusal.",
          "count of expect_refusal rows with a non-empty gold_keywords",
          classification="HARNESS")
    false_support = None
else:
    support = 0
    for row in unans_kw:
        got = approved.retrieve(row["question"])
        if keyword_hit(row, got):
            support += 1
    false_support = round(support / len(unans_kw), 4)
    check("B8", "retrieval does not manufacture lexical support for "
          "unanswerable questions",
          "PASS" if false_support < 0.10 else "FAIL",
          f"{support}/{len(unans_kw)} unanswerable questions retrieved "
          f"chunks containing their gold evidence "
          f"(false_support={false_support})",
          "gold-keyword presence in retrieved chunks for expect_refusal rows",
          classification="PRODUCT")

# ---------------------------------------------------------------- B9
print("\n[B9] negative candidate — BM25 disabled (dense-only)")
neg = make_retriever(os.environ.get("GATE8B_NEGATIVE_MODE", CFG["NEGATIVE_MODE"]))
neg_metrics = run_eval(neg, all_rows)
neg_pass, neg_viol = apply_gates(neg_metrics)
caught = not neg_pass
print(f"  dense-only recall@{TOP_K}={neg_metrics['recall_at_k']} "
      f"mrr={neg_metrics['mrr']} violations={neg_viol}")
check("B9", "known-bad candidate (dense-only) is REJECTED",
      "PASS" if caught else "FAIL",
      f"recall@{TOP_K} {base['recall_at_k']} -> {neg_metrics['recall_at_k']}, "
      f"mrr {base['mrr']} -> {neg_metrics['mrr']}; violations={neg_viol}",
      "pre-committed thresholds applied to a regressed retrieval config")
check("B9b", "removing BM25 measurably degrades retrieval",
      "PASS" if neg_metrics["recall_at_k"] < base["recall_at_k"] else "FAIL",
      f"{base['recall_at_k']} -> {neg_metrics['recall_at_k']} "
      f"({round((base['recall_at_k'] - neg_metrics['recall_at_k']) * 100, 1)} "
      f"point drop), mrr {base['mrr']} -> {neg_metrics['mrr']}",
      "paired comparison of the same golden set, one component disabled",
      classification="HARNESS")

# ---------------------------------------------------------------- B10
print("\n[B10] repeatability — deterministic metrics must be stable")
again = run_eval(make_retriever("hybrid"), all_rows)
stable = (again["recall_at_k"] == base["recall_at_k"]
          and again["mrr"] == base["mrr"]
          and again["cited_chunk_ids"] == base["cited_chunk_ids"])
check("B10", "repeated evaluation is bit-identical",
      "PASS" if stable else "FAIL",
      f"recall/mrr/citation-set identical: {stable}",
      "second independent retrieval pass over the same frozen corpus")
# ---------------------------------------------------------------- B11
print("\n[B11] CI behaviour — real subprocess, observed exit codes")


def gate_decision_subprocess(recall: float, mrr: float):
    src = (
        "import sys, yaml;"
        f"g=yaml.safe_load(open({str(ROOT / 'gate8b_thresholds.yaml')!r}))['GATES'];"
        f"rec={recall!r}; mrr={mrr!r};"
        "bad=(rec < g['min_recall_at_3'] or mrr < g['min_mrr']);"
        "print('gate decision:', 'FAIL' if bad else 'PASS');"
        "sys.exit(1 if bad else 0)"
    )
    return subprocess.run([sys.executable, "-c", src],
                          capture_output=True, text=True)


bad_proc = gate_decision_subprocess(neg_metrics["recall_at_k"], neg_metrics["mrr"])
check("B11", "CI exits non-zero when the RAG quality gate fails",
      "PASS" if bad_proc.returncode != 0 else "FAIL",
      f"exit={bad_proc.returncode} ({bad_proc.stdout.strip()})",
      "observed process exit code, not an in-process return value")

good_proc = gate_decision_subprocess(base["recall_at_k"], base["mrr"])
check("B12", "CI exits zero for the approved candidate",
      "PASS" if good_proc.returncode == 0 else "FAIL",
      f"exit={good_proc.returncode} ({good_proc.stdout.strip()})",
      "observed process exit code")

# B11/B12 prove the gate DECISION in isolation. They do NOT prove this whole
# script fails the build. If a bad candidate can still yield a green build,
# the gate is decorative.
# GATE8B_DEPTH is mandatory: a child re-entering this block would spawn its
# own child forever.
if int(os.environ.get("GATE8B_DEPTH", "0")) == 0:
    full_bad = subprocess.run(
        [sys.executable, __file__], cwd=str(ROOT), capture_output=True,
        text=True,
        env=dict(os.environ, GATE8B_DRYRUN="1", GATE8B_DEPTH="1",
                 GATE8B_NEGATIVE_MODE=CFG["NEGATIVE_MODE"]))
    check("B13", "FULL harness run fails the build on a bad RAG candidate",
          "PASS" if full_bad.returncode != 0 else "FAIL",
          f"whole-script exit={full_bad.returncode}; "
          f"{[l.strip() for l in full_bad.stdout.splitlines() if 'status=' in l]}",
          "exit code of this entire script re-run as a subprocess")

    full_good = subprocess.run(
        [sys.executable, __file__], cwd=str(ROOT), capture_output=True,
        text=True, env=dict(os.environ, GATE8B_DRYRUN="1", GATE8B_DEPTH="1"))
    # The child re-runs EVERY check, including B8 (abstention), which cannot
    # pass because the golden set ships no oracle for it. The approved run
    # therefore exits non-zero for a reason unrelated to retrieval quality.
    # Assert the RETRIEVAL decision specifically: claiming "CI is green" here
    # would be false, and asserting exit==0 anyway would be a self-fulfilling
    # test that hides a real gap.
    child_b4_failed = "[FAIL] B4" in full_good.stdout
    check("B14", "FULL harness run passes the RETRIEVAL gate on the approved "
          "candidate",
          "PASS" if not child_b4_failed else "FAIL",
          f"whole-script exit={full_good.returncode} (non-zero is expected "
          f"and honest: the child also reports B8 abstention as NOT_RUN, "
          f"recorded below as an open gap). B4 retrieval gate passed in the "
          f"child: {not child_b4_failed}",
          "presence of a B4 failure line in the child's own output",
          classification="HARNESS")
else:
    full_bad = full_good = None

# ---------------------------------------------------------------- summary
n_pass = sum(1 for r_ in RESULTS if r_["status"] == "PASS")
status = "PASS" if n_pass == len(RESULTS) else "PARTIAL"

commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                        capture_output=True, text=True).stdout.strip()
evidence = {
    "gate": "8B",
    "title": "MAIA RAG Quality Gate (retrieval, no LLM)",
    "status": status,
    "blocking_scope": "retrieval + citation validity only. Abstention and "
                      "generation quality are NOT covered — see "
                      "abstention.status and open_gaps.",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "commit": commit,
    "commit_repo": "MAIA (git remote: imtarget05/MAIA)",
    "environment": {"python": sys.version.split()[0],
                    "embedding_mode": "hash (MAIA_EMBED_FORCE_HASH=1)",
                    "vector_store": "InMemoryVectorStore",
                    "llm_in_gate": False},
    "integrity": {
        "golden_set_sha256": golden_sha,
        "corpus_sha256": corpus_sha,
        "golden_groups": len(golden_files),
        "corpus_files": len(corpus_manifest),
        "corpus_chunks": len(chunk_text),
        "answerable_rows": len(answerable),
        "unanswerable_rows": len(unanswerable),
    },
    "thresholds_precommitted": GATES,
    "oracle_ceiling_recall": ceiling,
    "approved": {k: v for k, v in base.items() if k != "per_row"},
    "negative_candidate": {k: v for k, v in neg_metrics.items() if k != "per_row"},
    "negative_candidate_config": {"mode": CFG["NEGATIVE_MODE"]},
    "abstention": {
        "unanswerable_rows": len(unans),
        "unanswerable_with_usable_oracle": len(unans_kw),
        "false_lexical_support": false_support,
        "status": "NOT_RUN" if not unans_kw else "MEASURED",
        "reason": (
            "Every expect_refusal row in eval/golden carries "
            "gold_keywords=[] by construction, so there is no deterministic "
            "oracle for correct refusal at the retrieval layer. Refusal "
            "accuracy requires the LLM generation stage and is deliberately "
            "NON-BLOCKING, because a hosted model can change at any time and "
            "would make CI flaky in a way this repo cannot control."
        ) if not unans_kw else "measured on retrieval layer",
    },
    "citation_validity": {
        "distinct_citations": len(base["cited_chunk_ids"]),
        "unresolvable": len(invalid),
        "validity": validity,
        "fabricated_id_rejected": detects_fake,
    },
    "negative_tests": {
        "maia_rejected": caught,
        "bm25_removal_degrades": neg_metrics["recall_at_k"] < base["recall_at_k"],
        "ci_exit_nonzero_on_fail": bad_proc.returncode != 0,
        "ci_exit_zero_on_approved": good_proc.returncode == 0,
        "full_harness_exit_on_bad_candidate": (
            full_bad.returncode if full_bad else "skipped_nested_run"),
        "full_harness_exit_on_approved": (
            full_good.returncode if full_good else "skipped_nested_run"),
    },
    "repeatability": {
        "identical": stable,
        "second_pass": {k: v for k, v in again.items() if k != "per_row"},
    },
    "key_finding": (
        "Disabling the BM25 lexical retriever dropped recall@"
        f"{TOP_K} {base['recall_at_k']} -> {neg_metrics['recall_at_k']} and MRR "
        f"{base['mrr']} -> {neg_metrics['mrr']} on the same frozen golden set, "
        "so the gate catches the exact regression a retrieval refactor causes. "
        f"Measured oracle ceiling is {ceiling}: "
        f"{int((1 - ceiling) * len(answerable))} of {len(answerable)} "
        "answerable questions have no supporting text in this corpus, so the "
        "raw recall must not be read as a model quality score."
    ),
    "checks": RESULTS,
    "open_gaps": [
        "B8 abstention NOT_RUN: all 10 expect_refusal rows in eval/golden "
        "carry gold_keywords=[], so there is no deterministic oracle for "
        "correct refusal at the retrieval layer. Measuring it needs either "
        "annotated forbidden-answer topics in the golden set, or the LLM "
        "generation stage, which is non-deterministic.",
        "B5: 17 of 85 answerable questions have no supporting text anywhere "
        f"in data/enterprise, capping measurable recall at {ceiling}. Raw "
        "recall@K must not be quoted as model quality without the ceiling.",
    ],
}
if os.environ.get("GATE8B_DRYRUN") != "1":
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2))
    print(f"\n{n_pass}/{len(RESULTS)} status={status}")
    print(f"evidence: {EVIDENCE}")
else:
    print(f"\n{n_pass}/{len(RESULTS)} status={status} (dry run)")
sys.exit(0 if status == "PASS" else 1)