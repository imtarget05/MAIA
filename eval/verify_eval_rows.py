#!/usr/bin/env python3
"""Verify MAIA eval rows against the canonical corpus.

WHY THIS EXISTS
---------------
A golden set whose labels are justified only by free-text `notes` is not
verifiable: a reviewer has to trust the note. This replaces trust with a
mechanical check.

  ANSWERABLE  requires >=1 `evidence_spans` entry, and EVERY span must appear
              literally in one of the `expected_sources` files. A span absent
              from the corpus is a hallucinated citation.

  NO_ANSWER   requires `absence_probe_terms`, and EVERY term must be absent
              from the ENTIRE corpus. If the term appears, the row is
              mislabelled: the fact exists, so the question is answerable.

This is how NOANS-009 was caught. It asserted "no clause states a carry-over
cap or number of days", while Leave_Policy.md states "Chuyển phép (carry over):
tối đa 3 ngày". The note was confidently wrong.

LABEL POLICY
------------
PENDING rows are never scored. Only VERIFIED rows enter metrics. A row that
fails verification is REJECTED, not silently kept.

Usage:
    python3 eval/verify_eval_rows.py
    python3 eval/verify_eval_rows.py --file no_answer
    python3 eval/verify_eval_rows.py --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import unicodedata

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "data/enterprise"
GOLDEN_DIR = ROOT / "eval/golden"

# maia.textnorm is stdlib-only (re + unicodedata) and is the SAME tokenizer the
# production retriever and gate8b_abstention.py use. Reusing it here is the point:
# a second home-grown tokenizer here would drift and silently disagree with the
# matcher the gate trusts.
sys.path.insert(0, str(ROOT / "src"))
from maia.textnorm import norm_tokens  # noqa: E402

SOURCE_ALIASES = {
    "leave_policy": "Leave_Policy.md",
    "hr_policy": "HR_Policy.md",
    "benefits": "Benefits.md",
    "expense_policy": "Expense_Policy.md",
    "it_handbook": "IT_Handbook.md",
    "it_security": "IT_Security_Policy_v4.2.md",
    "onboarding": "Onboarding_Guide.md",
    "vpn_guide": "VPN_Guide.md",
}

# Terminal labels. PENDING is deliberately absent: it is the absence of one.
TERMINAL = {"ANSWERABLE", "NO_ANSWER", "AMBIGUOUS"}


def norm(text: str) -> str:
    """Casefold and strip diacritics, matching MAIA's norm_tokens behaviour."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text.lower()).strip()


def load_corpus() -> dict[str, str]:
    return {p.name: norm(p.read_text(encoding="utf-8"))
            for p in sorted(CORPUS_DIR.glob("*.md"))}


# Longest probe phrase the absence matcher indexes. "cong thuc nau" is 3 tokens,
# "vay mua nha" is 3; 5 leaves headroom for the compound probes without turning
# the index into an unbounded memory cost on an 8-document corpus.
_MAX_NGRAM = 5


def build_token_index(corpus: dict[str, str]) -> dict[str, set[tuple[str, ...]]]:
    """filename -> set of contiguous token n-grams occurring in that document."""
    return {
        name: {tuple(toks[i:i + n])
               for n in range(1, _MAX_NGRAM + 1)
               for i in range(len(toks) - n + 1)}
        for name, body in corpus.items()
        for toks in (norm_tokens(body),)
    }


def term_present(term: str, index: dict[str, set[tuple[str, ...]]]) -> list[str]:
    """Documents in which `term` occurs as a contiguous TOKEN SEQUENCE.

    NOT a substring test. `"cat" in "Authentication"` is true, and substring
    matching reported that as `the fact exists, this row is mislabelled` for
    NOANS-006 - a correct row contradicted by a broken oracle.
    gate8b_abstention.py already carries the same finding at length: "A false
    INVALID_GOLDEN_ROW is worse than a false pass here: it would send someone to
    edit a correct dataset to satisfy a broken oracle."

    The fix belongs in the matcher, never in the data. Deleting the probe term
    "cat" would have made the check green by weakening the question.
    """
    toks = tuple(norm_tokens(term))
    if not toks:
        return []
    return [name for name, grams in index.items() if toks in grams]


def corpus_files() -> dict[str, str]:
    """Alias -> filename, plus the raw filename as its own key."""
    return {**SOURCE_ALIASES, **{v: k for k, v in SOURCE_ALIASES.items()}}


def verify_row(row: dict, corpus: dict[str, str], names: dict[str, str],
               index: dict[str, set[tuple[str, ...]]] | None = None) -> dict:
    """Return a verification result dict for one row. Never raises.

    `index` is the token n-gram index used by the NO_ANSWER absence check. It is
    optional so existing callers keep working; built from `corpus` when omitted.
    """
    rid = row.get("id", "<no-id>")
    status = row.get("review_status")
    label = row.get("answerability")
    if index is None:
        index = build_token_index(corpus)

    result = {"id": rid, "file_status": status, "answerability": label,
              "verdict": "UNKNOWN", "problems": []}
    problems = result["problems"]

    if status == "PENDING" or label == "PENDING":
        result["verdict"] = "PENDING"
        problems.append("row is PENDING; never counted in metrics")
        return result

    if status != "VERIFIED":
        result["verdict"] = "NOT_VERIFIED"
        problems.append(f"review_status={status!r} is not VERIFIED")
        return result

    if label not in TERMINAL:
        result["verdict"] = "BAD_LABEL"
        problems.append(f"answerability={label!r} is not a terminal label")
        return result

    if label == "AMBIGUOUS":
        # Preserved on purpose: an ambiguous row is not a failed row.
        if not row.get("reason"):
            problems.append("AMBIGUOUS row must carry a reason")
        result["verdict"] = "INCOMPLETE" if problems else "VERIFIED"
        return result

    if label == "ANSWERABLE":
        sources = [names.get(s, s) for s in row.get("expected_sources", [])]
        spans = row.get("evidence_spans", [])
        if not sources:
            problems.append("ANSWERABLE row has no expected_sources")
        if not spans:
            problems.append("ANSWERABLE row has no evidence_spans "
                            "(an unsupported answerable row is unverifiable)")
        haystacks = [corpus.get(s, "") for s in sources]
        if sources and not any(haystacks):
            problems.append(f"expected_sources not in corpus: {sources}")
        for span in spans:
            if not any(norm(span) in h for h in haystacks):
                problems.append(
                    f"evidence_span NOT in expected_sources: {span!r}")
        result["verdict"] = "VERIFIED" if not problems else "FAILED"
        return result

    # NO_ANSWER
    absent = row.get("absence_probe_terms", [])
    if not absent:
        problems.append("NO_ANSWER row has no absence_probe_terms "
                        "(absence cannot be proven)")
    for term in absent:
        hits = term_present(term, index)
        if hits:
            problems.append(
                f"absence_probe_term {term!r} IS PRESENT in {hits} "
                "-> the fact exists, this row is mislabelled")
    result["verdict"] = "VERIFIED" if not problems else "FAILED"
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="single golden file stem, e.g. no_answer")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    corpus = load_corpus()
    names = corpus_files()
    if not corpus:
        print("FATAL: canonical corpus is empty", file=sys.stderr)
        return 2

    files = sorted(GOLDEN_DIR.glob("*.jsonl"))
    if args.file:
        files = [f for f in files if f.stem == args.file]

    tally: dict[str, int] = {}
    failures: list[dict] = []
    pendings: list[dict] = []
    total = 0

    for path in files:
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                failures.append({"id": f"{path.stem}:{lineno}",
                                 "verdict": "BAD_JSON", "problems": [str(exc)]})
                continue
            res = verify_row(row, corpus, names)
            res["file"] = path.stem
            res["line"] = lineno
            tally[res["verdict"]] = tally.get(res["verdict"], 0) + 1
            if res["verdict"] in ("FAILED", "BAD_JSON", "BAD_LABEL",
                                  "INCOMPLETE", "NOT_VERIFIED"):
                failures.append(res)
            elif res["verdict"] == "PENDING":
                pendings.append(res)

    if args.json:
        print(json.dumps({"tally": tally, "failures": failures,
                          "pending": pendings, "total": total},
                         indent=2, ensure_ascii=False))
    else:
        print(f"corpus: {len(corpus)} files, "
              f"{sum(len(v) for v in corpus.values())} normalised chars")
        print(f"rows scanned: {total}\n")
        for verdict, count in sorted(tally.items()):
            print(f"  {verdict:<14} {count}")
        if failures:
            print(f"\n{len(failures)} ROW(S) FAILING VERIFICATION:")
            for f in failures:
                print(f"\n  [{f['verdict']}] {f.get('file','')}:"
                      f"{f.get('line','')} {f['id']}")
                for p in f["problems"]:
                    print(f"      - {p}")
        if pendings:
            print(f"\n{len(pendings)} PENDING row(s) excluded from all metrics.")
        # Fail loudly: a silently broken golden set is worse than a loud one.
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

