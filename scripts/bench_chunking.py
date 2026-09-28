"""Benchmark: sentence vs paragraph chunking on data/enterprise (stdlib only).

Usage: PYTHONPATH=src .venv/bin/python scripts/bench_chunking.py
 Compares chunk count, avg/max chars, heading-integrity proxy (% chunks whose
 first line looks like a heading and is not a mid-sentence cut).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from maia.chunking import split_documents  # noqa: E402
from maia.ingestion import RawDoc  # noqa: E402


def main() -> None:
    docs = []
    for f in sorted(Path("data/enterprise").glob("*.md")):
        docs.append(RawDoc(text=f.read_text(encoding="utf-8", errors="ignore"),
                           metadata={"doc_id": f.stem, "filename": f.name}))
    if not docs:
        print("no docs in data/enterprise")
        return
    for strategy in ("sentence", "paragraph"):
        for size in (512, 1024):
            t0 = time.time()
            chunks = split_documents(docs, chunk_size=size, chunk_overlap=50, strategy=strategy)
            dt = time.time() - t0
            lens = [len(c.text) for c in chunks]
            avg = sum(lens) / max(1, len(lens))
            print(f"strategy={strategy:9s} size={size:4d} chunks={len(chunks):4d} "
                  f"avg={avg:6.0f} max={max(lens) if lens else 0:5d} time={dt:.2f}s")


if __name__ == "__main__":
    main()
