#!/usr/bin/env python3
"""Audit golden rows: normalise schema, then verify labels against the corpus.

Two stages, deliberately separate:

  1. NORMALISE  map expect_refusal -> answerability (provenance recorded).
                review_status is read, never written.
  2. VERIFY     run corpus checks on rows that already claim VERIFIED.

Stage 2 is where a legacy expectation can be shown WRONG. Stage 1 can only
show what it meant.

A row carrying no review_status is treated as PENDING at this layer via .get().
It is not written back: mutating the dataset to make it readably verified would
be the same failure as promoting a legacy label to truth.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from answerability_schema import (  # noqa: E402
    DEFAULT_PENDING, EXPLICIT, EXPLICIT_REJECTED, LEGACY_EXPECT_REFUSAL,
    map_legacy_answerability)
from verify_eval_rows import load_corpus, corpus_files, norm  # noqa: E402

GOLDEN = pathlib.Path(__file__).resolve().parent / "golden"

# Outcome vocabulary, kept deliberately distinct. EXPECTED REJECTION IS NOT
# FAILURE: a row retired because corpus verification contradicted its label is
# the system working correctly, and folding it into "failed" would overstate
# breakage and understate a real finding.
OUTCOMES = ("usable", "unusable", "rejected", "schema_conflict")


def read_rows(path: pathlib.Path) -> list[dict]:
    """Parse JSONL that may also contain pretty-printed multi-line objects.

    A row is a single-line dict, or a brace-balanced block. Tracking brace
    depth rather than assuming one-object-per-line keeps a hand-edited,
    fully formatted row readable instead of a parse error.
    """
    rows: list[dict] = []
    buf: list[str] = []
    depth = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if depth == 0 and not buf:
            if not line.startswith("{"):
                raise ValueError(f"{path.name}: line does not start with '{{': {line[:60]!r}")
        buf.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            rows.append(json.loads("\n".join(buf)))
            buf, depth = [], 0
    if buf:
        raise ValueError(f"{path.name}: unterminated JSON block")
    return rows


def audit_file(path: pathlib.Path) -> dict:
    corpus = load_corpus()
    names = corpus_files()
    rows = read_rows(path)

    explicit = legacy = pending = 0
    rejected: list[str] = []
    conflicts: list[str] = []
    verified = excluded = 0
    ver_answerable = ver_no_answer = ver_ambiguous = 0
    corpus_failures: list[str] = []

    for row in rows:
        rid = row.get("id") or str(row.get("question", "<no-id>"))[:48]
        try:
            answerability, source = map_legacy_answerability(row)
        except ValueError as exc:
            conflicts.append(f"{rid}: {exc}")
            continue

        if source == EXPLICIT:
            explicit += 1
        elif source == LEGACY_EXPECT_REFUSAL:
            legacy += 1
        elif source == EXPLICIT_REJECTED:
            # Corpus verification retired this label. Expected rejection.
            rejected.append(f"{rid}: {row.get('rejection_reason', 'REJECTED')}")
            continue
        else:
            pending += 1

        # Read-only. Absent review_status means unverified, not a value to write.
        if row.get("review_status", DEFAULT_PENDING) != "VERIFIED":
            excluded += 1
            continue

        verified += 1
        if answerability == "ANSWERABLE":
            ver_answerable += 1
        elif answerability == "NO_ANSWER":
            ver_no_answer += 1
        elif answerability == "AMBIGUOUS":
            ver_ambiguous += 1
        else:
            excluded += 1
            continue

        # Corpus check. For NO_ANSWER this is where a wrong label gets caught.
        if answerability == "NO_ANSWER":
            terms = row.get("probe_terms") or row.get("absence_probe_terms", [])
            for term in terms:
                hits = [n for n, body in corpus.items() if norm(term) in body]
                if hits:
                    corpus_failures.append(
                        f"{rid}: absence_probe_term {term!r} IS PRESENT in {hits}")
        elif answerability == "ANSWERABLE":
            srcs = [names.get(s, s) for s in row.get("expected_sources", [])]
            spans = row.get("evidence_spans", [])
            if not spans:
                corpus_failures.append(f"{rid}: ANSWERABLE with no evidence_spans")

            for span in spans:
                if not any(norm(span) in corpus.get(s, "") for s in srcs):
                    corpus_failures.append(
                        f"{rid}: evidence_span NOT in corpus: {span!r}")

    return {
        "file": path.stem, "rows": len(rows),
        "explicit": explicit, "legacy": legacy, "pending": pending,
        "rejected": rejected, "conflicts": conflicts,
        "verified": verified, "excluded": excluded,
        "ver_answerable": ver_answerable, "ver_no_answer": ver_no_answer,
        "ver_ambiguous": ver_ambiguous, "corpus_failures": corpus_failures,
    }


def parse_args(argv: list[str]) -> tuple[str | None, bool]:
    """Parse the CLI. Kept separate so it can be tested without side effects.

    Previous version did `only = sys.argv[1]`, which silently swallowed `--json`
    as a project name. Flags and positionals now have distinct namespaces, and a
    missing value for --only is an error rather than a silent whole-repo audit.
    """
    ap = argparse.ArgumentParser(
        prog="audit_eval_rows",
        description="Audit MAIA golden rows against the canonical corpus.")
    ap.add_argument("--only", metavar="PROJECT", default=None,
                    help="audit a single golden file stem, e.g. no_answer")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit machine-readable JSON on stdout only")
    try:
        ns = ap.parse_args(argv)
    except SystemExit:
        raise
    return ns.only, ns.as_json


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    only, as_json = parse_args(argv)

    all_files = sorted(GOLDEN.glob("*.jsonl"))
    if only:
        files = [f for f in all_files if f.stem == only]
        if not files:
            # Unknown project must not degrade into a silent pass over nothing.
            msg = f"no golden file matching {only!r}; available: " \
                  f"{', '.join(f.stem for f in all_files)}"
            if as_json:
                print(json.dumps({"error": msg, "available":
                                  [f.stem for f in all_files]}, indent=2))
            else:
                print(msg, file=sys.stderr)
            return 2
    else:
        files = all_files

    agg = dict.fromkeys(
        ["rows", "explicit", "legacy", "pending", "verified", "excluded",
         "ver_answerable", "ver_no_answer", "ver_ambiguous"], 0)
    all_conflicts: list[tuple[str, str]] = []
    all_failures: list[tuple[str, str]] = []
    all_rejected: list[tuple[str, str]] = []

    for path in files:
        r = audit_file(path)
        for k in agg:
            agg[k] += r[k]
        all_conflicts += [(r["file"], c) for c in r["conflicts"]]
        all_failures += [(r["file"], c) for c in r["corpus_failures"]]
        all_rejected += [(r["file"], c) for c in r["rejected"]]
        # In JSON mode stdout must contain only the JSON document, so the
        # per-file human line goes to stderr.
        if not as_json:
            print(f"{r['file']:<22} rows={r['rows']:<4} usable={r['verified']:<3} "
                  f"unusable={r['excluded']:<3} rejected={len(r['rejected']):<3} "
                  f"conflict={len(r['conflicts'])}")

    n_rejected = len(all_rejected)
    n_conflict = len(all_conflicts)
    n_failed = len(all_failures)

    # total = usable + unusable + rejected + conflict
    total = agg["rows"]
    accounted = agg["verified"] + agg["excluded"] + n_rejected + n_conflict
    denominator_ok = (total == accounted)

    if not denominator_ok:
        print(f"\nDENOMINATOR ERROR: total={total} but categories sum to "
              f"{accounted}. Some rows are unaccounted for.", file=sys.stderr)

    if as_json:
        print(json.dumps({
            "test_results": {
                "total": total,
                "usable": agg["verified"],
                "unusable": agg["excluded"],
                "rejected": n_rejected,
                "schema_conflict": n_conflict,
                "failed": n_failed,
            },
            "denominator_check": {
                "total": total, "sum_of_categories": accounted,
                "valid": denominator_ok,
                "identity": "total = usable + unusable + rejected + schema_conflict",
            },
            "label_provenance": {
                "explicit": agg["explicit"],
                "mapped_legacy_expect_refusal": agg["legacy"],
                "pending_or_unlabeled": agg["pending"],
            },
            "usable_breakdown": {
                "ANSWERABLE": agg["ver_answerable"],
                "NO_ANSWER": agg["ver_no_answer"],
                "AMBIGUOUS": agg["ver_ambiguous"],
            },
            "rejected": [{"file": f, "detail": d} for f, d in all_rejected],
            "corpus_failures": [{"file": f, "detail": d} for f, d in all_failures],
        }, indent=2, ensure_ascii=False))
        return 1 if n_conflict or n_failed or not denominator_ok else 0

    print("\n--- audit summary ---")
    print(f"total rows:                  {total}")
    print(f"usable in metrics:           {agg['verified']}")
    print(f"unusable (not yet evidence): {agg['excluded']}")
    print(f"rejected (expected):         {n_rejected}")
    print(f"schema conflict (error):     {n_conflict}")
    if n_failed:
        print(f"corpus failures (error):     {n_failed}")
    print(f"denominator valid:           {denominator_ok} "
          f"(total == usable+unusable+rejected+conflict)")

    print("\n--- label provenance ---")
    print(f"Explicit new-schema labels:     {agg['explicit']}")
    print(f"Mapped legacy expect_refusal:   {agg['legacy']}")
    print(f"Pending / unlabeled:            {agg['pending']}")

    if agg["verified"]:
        print("\n--- usable label breakdown ---")
        print(f"  ANSWERABLE: {agg['ver_answerable']}")
        print(f"  NO_ANSWER:  {agg['ver_no_answer']}")
        print(f"  AMBIGUOUS:  {agg['ver_ambiguous']}")

    if all_rejected:
        print("\nEXPECTED REJECTIONS (corpus verification retired these labels):")
        for f, c in all_rejected:
            print(f"  [{f}] {c}")
    if all_conflicts:
        print("\nSCHEMA CONFLICTS:")
        for f, c in all_conflicts:
            print(f"  [{f}] {c}")
    if all_failures:
        print("\nCORPUS VERIFICATION FAILURES "
              "(a label contradicted by the corpus):")
        for f, c in all_failures:
            print(f"  [{f}] {c}")
    elif agg["verified"]:
        print("\nNo corpus contradictions among usable rows.")

    # A schema conflict or a corpus failure is a real error. An expected
    # rejection is not: it is the verifier working.
    return 1 if n_conflict or n_failed or not denominator_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

