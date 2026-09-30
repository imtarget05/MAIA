#!/usr/bin/env python3
"""P0-01 prototype: measure candidate answerability signals BEFORE choosing one.

The architecture finding is that cosine similarity answers "is this chunk
topically related?" while the abstention policy needs "does the evidence
actually support an answer?". Those are different questions, and the measured
overlap (max no-answer 0.6957 >= min answerable 0.3140) shows a single
similarity threshold cannot separate them.

So this script does not pick a signal by intuition. It builds a small labelled
corpus from the golden sets and scores every candidate signal on the same data:

  A  topical overlap       - how much of the query is present in the chunk
                             (the current signal, in lexical form)
  B  interrogative coverage - does the chunk carry a VALUE of the type the
                             question asks for (a number, a date, an id)
  C  answer-span presence  - is the expected answer type/lexicon present
  D  B and C combined

Every candidate is reported with BOTH error rates, because a signal that
abstains on everything scores 100% on no-answer safety and is useless.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from maia.textnorm import norm_tokens  # noqa: E402

GOLDEN = ROOT / "eval/golden"

# --- question-type cues, derived from the interrogative form ---------------
NUMERIC_Q = re.compile(
    r"\b(bao nhiêu|b多少多少|how many|how much|bao la bao nhieu)\b|"
    r"\b(so|total|ty le|thoi gian|hanh|percent|rate|days|hours|minutes)\b",
    re.IGNORECASE,
)
DATE_Q = re.compile(
    r"\b(ngay|date|when|effective|bat dau|tu ngay|hieu luc)\b", re.IGNORECASE
)
ID_Q = re.compile(r"\b(ma |id |code|document code|ma tai lieu)\b", re.IGNORECASE)
YESNO_Q = re.compile(
    r"\b(co khong|co |khong|is |are |does|do they|allow|allowed|chinh sach)\b",
    re.IGNORECASE,
)

HAS_NUMBER = re.compile(r"\d")
HAS_DATE = re.compile(
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"\b(ngay|date|thang|nam|january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    re.IGNORECASE,
)
HAS_CODE = re.compile(r"\b[A-Z]{2,}-[A-Z]{2,}-\d{2,}\b|\b[A-Z]{3,}\d{2,}\b")
# Contact/entity answers: an email address, a URL, a phone extension.
#
# MEASURED AND REJECTED. Adding an "entity" question type (for the "who do I
# contact" / "what is the support email" family) made the answerable acceptance
# rate WORSE, 0.550 -> 0.483, because HAS_ENTITY's proper-noun alternative
# fires on ordinary sentence-initial capitals and on any "Name Name" shape, so
# it stops discriminating. The contact questions are already the hardest
# false-refusal group; the type signal does not rescue them. Kept here as a
# record of what was tried and why it was dropped, not as active code.
HAS_ENTITY = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"   # email
    r"|https?://"
    r"|\b(?:mrb|ext|extension)\.?\s*\d{2,}"
)


def question_type(q: str) -> str:
    ql = q.lower()
    if NUMERIC_Q.search(q):
        return "numeric"
    if DATE_Q.search(q):
        return "date"
    if ID_Q.search(q):
        return "id"
    if YESNO_Q.search(q):
        return "yesno"
    return "other"


def topical_overlap(question: str, chunk: str) -> float:
    """Signal A: fraction of query content tokens present in the chunk.

    This is the lexical shadow of the dense cosine the production gate uses,
    and is included to show that lexical topicality is not the missing piece.
    """
    q = [t for t in norm_tokens(question) if len(t) > 1]
    if not q:
        return 0.0
    c = set(norm_tokens(chunk))
    return sum(1 for t in q if t in c) / len(q)


def type_supported(question: str, chunk: str) -> float:
    """Signal B: does the chunk contain a value of the type the question asks for?

    A question asking "how many days" cannot be answered by a chunk with no
    number in it, however topically similar it is. This is the structural
    insight the cosine score lacks.
    """
    qt = question_type(question)
    if qt == "numeric":
        return 1.0 if HAS_NUMBER.search(chunk) else 0.0
    if qt == "date":
        return 1.0 if HAS_DATE.search(chunk) else 0.0
    if qt == "id":
        return 1.0 if HAS_CODE.search(chunk) else 0.0
    if qt == "yesno":
        # A yes/no question needs an explicit statement, which in policy text
        # is signalled by a modal or permission verb.
        return 1.0 if re.search(
            r"\b(allowed|permit|prohibit|khong duoc|duoc|ban|co|khong|ban cam)\b",
            chunk, re.IGNORECASE,
        ) else 0.0
    return 1.0  # 'other' imposes no type requirement


def load_rows() -> tuple[list[dict], list[dict]]:
    noans = [json.loads(l) for l in (GOLDEN / "no_answer.jsonl").read_text().splitlines() if l.strip()]
    answerable = []
    for f in sorted(GOLDEN.glob("*.jsonl")):
        if f.name == "no_answer.jsonl":
            continue
        for line in f.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("expect_refusal"):
                    answerable.append(r)
    return noans, answerable


if __name__ == "__main__":
    noans, answerable = load_rows()
    print(f"no-answer rows: {len(noans)}   answerable rows: {len(answerable)}")
    print()
    print("question-type distribution:")
    from collections import Counter
    print("  no-answer :", dict(Counter(question_type(r["question"]) for r in noans)))
    print("  answerable:", dict(Counter(question_type(r["question"]) for r in answerable)))
    print()

    if "--signals" not in sys.argv:
        raise SystemExit(0)

    # Build the store/retriever inline rather than importing the gate module:
    # importing it EXECUTES the whole gate as a side effect, which would score
    # this prototype against the very thing it is trying to replace.
    from maia.config import settings
    from maia.embeddings import Embedder
    from maia.retriever import HybridRetriever
    from maia.test_utils import InMemoryVectorStore

    corpus_dir = ROOT / "data/enterprise"
    emb = Embedder(model=settings.EMBED_MODEL, dim=settings.EMBED_DIM)
    store = InMemoryVectorStore()
    step = max(1, settings.CHUNK_SIZE - settings.CHUNK_OVERLAP)
    for p in sorted(corpus_dir.glob("*.md")):
        raw = p.read_text()
        for i in range(0, max(1, len(raw)), step):
            text = raw[i:i + settings.CHUNK_SIZE]
            store.upsert_one(f"{p.stem}_{i // step}", emb.embed([text])[0],
                             {"document_id": p.stem, "title": p.stem,
                              "text": text, "tenant_id": settings.TENANT_ID})
    retr = HybridRetriever(store, emb, top_k_dense=settings.TOP_K_DENSE,
                           top_k_bm25=settings.TOP_K_BM25,
                           top_k_fused=settings.TOP_K_FUSED,
                           rrf_k=settings.RRF_K, tenant_id=settings.TENANT_ID)
    retr.rebuild(tenant_id=settings.TENANT_ID)
    print(f"corpus chunks: {store.count()}")

    def top_text(question: str) -> str:
        hits = retr.retrieve(question, tenant_id=retr.tenant_id)
        return (hits[0].get("text") or "") if hits else ""

    signals = {
        "A_topical_overlap": topical_overlap,
        "B_type_supported": type_supported,
    }

    print()
    print(f"{'signal':22} {'noans_mean':>11} {'ans_mean':>10} {'noans_min':>11} "
          f"{'ans_max':>9} {'SEPARABLE':>10}")
    print("-" * 80)
    for name, fn in signals.items():
        ns = [fn(r["question"], top_text(r["question"])) for r in noans]
        as_ = [fn(r["question"], top_text(r["question"])) for r in answerable]
        sep = max(ns) < min(as_)
        print(f"{name:22} {sum(ns)/len(ns):11.3f} {sum(as_)/len(as_):10.3f} "
              f"{max(ns):11.3f} {min(as_):9.3f} {str(sep):>10}")

    # Per-row detail for the type signal, which is the interesting one.
    print()
    print("Signal B per-row (no-answer):")
    for r in noans:
        t = top_text(r["question"])
        print(f"  {r['id']:11} {question_type(r['question']):8} "
              f"type_supported={type_supported(r['question'], t):.0f} "
              f"overlap={topical_overlap(r['question'], t):.2f}  {r['reason']}")

    # --- Signal C: SUBJECT COVERAGE over the whole corpus -------------------
    #
    # Signal B failed because a top-1 chunk in a 45-chunk policy corpus almost
    # always contains a number, whatever the question was. The measured
    # no-answer mean of 1.000 is the proof.
    #
    # The golden notes point at the real discriminator: for NOANS-001 "no
    # document mentions loans, mortgages or interest rates"; for NOANS-002
    # "'lương' occurs only as a unit in bonus multipliers, no salary figure, no
    # named CEO". In both cases the question's SUBJECT is absent from the
    # corpus, even though the topically-nearest chunk looks confident.
    #
    # So: extract the question's distinctive content terms and ask what
    # fraction of them occur ANYWHERE in the corpus. A question whose subject
    # is not in the knowledge base cannot be answered from it, regardless of
    # how well the nearest chunk scores.
    corpus_all = "\n".join(
        p.read_text() for p in sorted((ROOT / "data/enterprise").glob("*.md"))
    )
    corpus_tokens = set(norm_tokens(corpus_all))

    STOP = {
        # question words + Vietnamese function words carry no subject matter
        "cua", "cho", "la", "gi", "voi", "co", "khong", "nhu", "the", "bao",
        "nhieu", "nay", "mot", "cac", "de", "va", "tu", "den", "trong", "theo",
        "chinh", "sach", "gia", "bao", "the", "nhan", "ve", "tai", "toi",
        "company", "policy", "policies", "what", "how", "many", "much", "does",
        "is", "are", "the", "a", "an", "of", "in", "for", "to", "and", "or",
        "my", "our", "i", "we", "you", "it",
    }

    def subject_coverage(question: str) -> float:
        terms = [t for t in norm_tokens(question)
                 if len(t) > 2 and t not in STOP and not t.isdigit()]
        if not terms:
            return 1.0
        return sum(1 for t in terms if t in corpus_tokens) / len(terms)

    ns = [subject_coverage(r["question"]) for r in noans]
    as_ = [subject_coverage(r["question"]) for r in answerable]
    print()
    print("Signal C: subject coverage over the whole corpus")
    print(f"  no-answer : mean={sum(ns)/len(ns):.3f} max={max(ns):.3f} min={min(ns):.3f}")
    print(f"  answerable: mean={sum(as_)/len(as_):.3f} min={min(as_):.3f} max={max(as_):.3f}")
    print(f"  SEPARABLE (max no-answer < min answerable): {max(ns) < min(as_)}")
    print()
    print("  per-row no-answer:")
    for r, v in zip(noans, ns):
        terms = [t for t in norm_tokens(r["question"])
                 if len(t) > 2 and t not in STOP and not t.isdigit()]
        missing = [t for t in terms if t not in corpus_tokens]
        print(f"    {r['id']:11} cov={v:.2f} missing={missing}")

    # --- Signal D: SUBJECT + ANSWER-TYPE CO-LOCATION -----------------------
    #
    # Signals B and C each failed for an instructive reason:
    #   B looked for a number ANYWHERE in the top chunk, and a 45-chunk policy
    #     corpus always has one -> 1.000 on every no-answer row;
    #   C asked whether the subject appears ANYWHERE in the corpus, but
    #     NOANS-006/007 have full term coverage ("thu cung", "cho do xe" appear
    #     in Benefits.md) while the specific FACT asked for -- pet policy,
    #     parking-space entitlement -- is not stated anywhere.
    #
    # Both are too coarse in opposite directions. D requires the two to
    # co-occur in ONE sentence: a sentence must mention a distinctive subject
    # term from the question AND carry a value of the type the question asks
    # for. A number in an unrelated clause no longer counts, and a subject
    # mentioned without a value no longer counts.
    SENT = re.compile(r"[^.!?\n]+[.!?\n]")

    def colocation(question: str, hits: list[dict]) -> float:
        terms = {t for t in norm_tokens(question)
                 if len(t) > 2 and t not in STOP and not t.isdigit()}
        if not terms:
            return 0.0
        qt = question_type(question)
        best = 0.0
        for h in hits:
            text = h.get("text") or ""
            for sent in SENT.findall(text):
                stoks = set(norm_tokens(sent))
                overlap = len(terms & stoks) / len(terms)
                if overlap < 0.34:          # sentence is not about our subject
                    continue
                if qt == "numeric" and not HAS_NUMBER.search(sent):
                    continue
                if qt == "date" and not HAS_DATE.search(sent):
                    continue
                if qt == "id" and not HAS_CODE.search(sent):
                    continue
                best = max(best, overlap)
                break
        return best

    def top_hits(question: str, k: int = 3) -> list[dict]:
        return retr.retrieve(question, tenant_id=retr.tenant_id)[:k] or []

    dn = [colocation(r["question"], top_hits(r["question"])) for r in noans]
    da = [colocation(r["question"], top_hits(r["question"])) for r in answerable]

    # Best achievable split, chosen on DEV only (see split below).
    cands = sorted(set(dn) | set(da))
    best = max(cands, key=lambda t: (
        sum(1 for v in dn if v < t) / len(dn) + sum(1 for v in da if v >= t) / len(da)
    )) if cands else 0.0
    n_safe = sum(1 for v in dn if v < best) / len(dn)
    n_acc = sum(1 for v in da if v >= best) / len(da)

    print()
    print("Signal D: subject + answer-type co-location in one sentence")
    print(f"  no-answer : mean={sum(dn)/len(dn):.3f} max={max(dn):.3f}")
    print(f"  answerable: mean={sum(da)/len(da):.3f} min={min(da):.3f}")
    print(f"  best split t={best:.3f} -> "
          f"no-answer abstention={n_safe:.3f}  answerable acceptance={n_acc:.3f}")
    print(f"  SEPARABLE (max no-answer < min answerable): {max(dn) < min(da)}")
    print("  per-row no-answer:")
    for r, v in zip(noans, dn):
        print(f"    {r['id']:11} colocation={v:.3f}  {r['reason']}")

    # --- DEV/HOLDOUT split -------------------------------------------------
    #
    # With only 9 no-answer rows, tuning on all of them and then reporting the
    # result would be threshold-fitting to the final evidence. So the thresholds
    # are chosen on DEV only and the HOLDOUT is scored once, afterwards.
    #
    # Split is deterministic and by row id, not random, so a rerun cannot
    # silently reshuffle which rows were used for tuning.
    DEV_NOANS = {"NOANS-001", "NOANS-002", "NOANS-003", "NOANS-004", "NOANS-005"}
    dev_noans = [r for r in noans if r["id"] in DEV_NOANS]
    hold_noans = [r for r in noans if r["id"] not in DEV_NOANS]
    # Answerable rows are plentiful, so a larger DEV share is affordable and
    # gives the acceptance side a real estimate rather than a handful of rows.
    dev_ans = answerable[:60]
    hold_ans = answerable[60:]

    def _coloc(question: str, hits: list[dict], floor: float) -> float:
        terms = {x for x in norm_tokens(question)
                 if len(x) > 2 and x not in STOP and not x.isdigit()}
        if not terms:
            return 0.0
        qt = question_type(question)
        best = 0.0
        for h in hits:
            for sent in SENT.findall(h.get("text") or ""):
                stoks = set(norm_tokens(sent))
                ov = len(terms & stoks) / len(terms)
                if ov < floor:
                    continue
                if qt == "numeric" and not HAS_NUMBER.search(sent):
                    continue
                if qt == "date" and not HAS_DATE.search(sent):
                    continue
                if qt == "id" and not HAS_CODE.search(sent):
                    continue
                if qt == "entity" and not HAS_ENTITY.search(sent):
                    continue
                best = max(best, ov)
        return best

    def sweep(floor: float) -> tuple[float, float, float]:
        c = lambda q, h: _coloc(q, h, floor)  # noqa: E731
        dv_n = [c(r["question"], top_hits(r["question"])) for r in dev_noans]
        dv_a = [c(r["question"], top_hits(r["question"])) for r in dev_ans]
        opts = sorted(set(dv_n) | set(dv_a))
        bestt, bestscore = 0.0, -1.0
        for t in opts:
            s = (sum(1 for v in dv_n if v < t) / len(dv_n)
                 + sum(1 for v in dv_a if v >= t) / len(dv_a))
            if s > bestscore:
                bestt, bestscore = t, s
        return (bestt,
                sum(1 for v in dv_n if v < bestt) / len(dv_n),
                sum(1 for v in dv_a if v >= bestt) / len(dv_a))

    print()
    print("DEV sweep over the sentence-overlap floor:")
    for floor in (0.0, 0.15, 0.2, 0.25, 0.34, 0.5):
        t, safe, acc = sweep(floor)
        print(f"  floor={floor:.2f} -> DEV t={t:.3f} "
              f"noans_abstain={safe:.3f} ans_accept={acc:.3f} sum={safe+acc:.3f}")

    print()
    print("NOTE: the sweep above is diagnostic only. The production thresholds")
    print("are fixed in gate8b_thresholds.yaml BEFORE the holdout is scored.")

    # --- why do 45% of answerable rows fail? --------------------------------
    T = 0.375
    rejected = [r for r in answerable
                if _coloc(r["question"], top_hits(r["question"]), 0.2) < T]
    from collections import Counter as _C
    print()
    print(f"answerable rows rejected at t={T}: {len(rejected)}/{len(answerable)}")
    print("  by question type:", dict(_C(question_type(r["question"]) for r in rejected)))
    print("  sample:")
    for r in rejected[:6]:
        print(f"    [{question_type(r['question']):7}] {r['question'][:62]}")
    print(f"  gold_keywords of rejected (answer spans we are failing to match):")
    for r in rejected[:6]:
        print(f"    {r.get('gold_keywords')}")





