#!/usr/bin/env python3
"""Micro-benchmark: MAIA chunking invariant timing.

Tries import-clean module maia.chunking (needs PYTHONPATH=src); if absent,
falls back to a stdlib text-split baseline explicitly labeled as such.
Never fails: prints SKIP + exit 0 only when nothing runnable exists
(which cannot happen — baseline is stdlib-only).

Run: PYTHONPATH=src python3 scripts/bench_maia.py   (from MAIA/)
"""
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))

DOC = (
    "Leave policy section 1. Employees are entitled to annual leave. "
    "IT ticket workflow requires approval. Security policy forbids exfiltration. "
) * 40

try:
    from maia.chunking import split_documents  # import-clean: stdlib + lazy llama_index

    MODE = "maia.chunking.split_documents"
    Doc = dict

    def make_docs():
        return [{"text": DOC, "metadata": {"doc_id": "bench"}}]

    def op():
        split_documents(make_docs(), chunk_size=512, chunk_overlap=50)

except Exception as exc:
    MODE = f"text-split baseline (maia.chunking unavailable: {exc})"

    def op():
        words = DOC.split()
        win, ov = 102, 10
        step = win - ov
        return [" ".join(words[s:s + win]) for s in range(0, len(words), step)]


def main():
    n = 200
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        op()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    mean = statistics.fmean(samples)
    p95 = samples[min(n - 1, int(n * 0.95))]
    print(f"mode: {MODE}")
    print(f"{'op':<18}{'n':>8}{'mean_ms':>12}{'p95_ms':>12}")
    print(f"{'chunking':<18}{n:>8}{mean:>12.4f}{p95:>12.4f}")


if __name__ == "__main__":
    main()
