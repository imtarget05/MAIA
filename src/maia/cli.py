"""CLI: python -m maia.cli ingest | query "question" | health"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from maia.pipeline_query import ingest_data_dir, query, build_stack


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "ingest":
        print(ingest_data_dir())
    elif cmd == "query":
        q = " ".join(sys.argv[2:]) or "MAIA là gì?"
        import json

        print(json.dumps(query(q), ensure_ascii=False, indent=2)[:4000])
    elif cmd == "health":
        _, store, _, reranker, llm = build_stack()
        print({"points": store.count(), "llm_mode": llm.mode, "rerank_mode": reranker.mode})
    else:
        print("usage: python -m maia.cli [ingest|query <q>|health]")


if __name__ == "__main__":
    main()
