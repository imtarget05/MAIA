"""CLI: python -m maia.cli ingest | query "question" | health | stream {produce|worker} | benchmark | metrics | lag"""
import json
import sys

from maia.ingestion_pipeline import ingest_data_dir
from maia.pipeline_query import build_stack, query


def _cmd_stream(args) -> None:
    """Streaming ingestion subcommands (PROJECT 2)."""
    sub = args[0] if args else "help"
    from maia.config import settings
    from maia.stream.metrics import registry

    if sub == "produce":
        from maia.stream.producer import ChunkProducer
        from maia.stream.transport import build_default_transport

        transport = build_default_transport()
        producer = ChunkProducer(transport, partitioning=settings.KAFKA_PARTITIONING)
        result = producer.ingest_dir(args[1] if len(args) > 1 else settings.DATA_DIR)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif sub == "worker":
        from maia.embeddings import Embedder
        from maia.stream.store import build_stream_store
        from maia.stream.transport import build_default_transport
        from maia.stream.worker import run_workers

        transport = build_default_transport()
        embedder = Embedder(model=settings.EMBED_MODEL, dim=settings.EMBED_DIM)
        store = build_stream_store(embedder)
        workers = int(args[1]) if len(args) > 1 else settings.KAFKA_WORKERS
        print(json.dumps(run_workers(workers, transport, store, embedder), indent=2))
    elif sub == "lag":
        from maia.stream.transport import build_default_transport

        transport = build_default_transport()
        print({"consumer_group": settings.KAFKA_CONSUMER_GROUP,
               "topic": settings.KAFKA_TOPIC_CHUNKS,
               "lag": transport.lag(settings.KAFKA_CONSUMER_GROUP, settings.KAFKA_TOPIC_CHUNKS)})
    elif sub == "metrics":
        print(registry.render(), end="")
    else:
        print("usage: python -m maia.cli stream [produce|worker <n>|lag|metrics]")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "ingest":
        # support --enterprise flag
        if "--enterprise" in sys.argv:
            from maia.config import settings
            print(ingest_data_dir(settings.ENTERPRISE_DATA_DIR))
        else:
            print(ingest_data_dir())
    elif cmd == "query":
        q = " ".join([a for a in sys.argv[2:] if not a.startswith("--")]) or "MAIA là gì?"
        print(json.dumps(query(q), ensure_ascii=False, indent=2)[:4000])
    elif cmd == "chat":
        from maia.agent.agent import EnterpriseAgent
        # NOTE: settings are loaded at import, so flags mutate the singleton.
        if "--crag" in sys.argv:
            from maia.config import settings as _s
            _s.CRAG_ENABLED = True
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        q = " ".join(args) or "Tôi muốn xin nghỉ phép 5 ngày từ 10/09"
        agent = EnterpriseAgent()
        res = agent.chat(q)
        print(json.dumps(res, ensure_ascii=False, indent=2)[:6000])
        if res.get("status") == "needs_approval":
            if "--approve" in sys.argv:
                print(json.dumps(agent.confirm_action("default", approved=True),
                                 ensure_ascii=False, indent=2)[:4000])
            else:
                print("\nHINT: side-effect proposed but NOT executed. "
                      "Re-run with --approve, or POST /actions/confirm (same server process).")
    elif cmd == "confirm":
        # NOTE: pending actions live in-process; this works with a running
        # server only via /actions/confirm. For CLI demos use: chat --approve
        from maia.agent.agent import EnterpriseAgent
        sub = sys.argv[2] if len(sys.argv) > 2 else "default"
        approved = (sys.argv[3].lower() if len(sys.argv) > 3 else "yes") in ("yes", "y", "1", "true")
        agent = EnterpriseAgent()
        print(json.dumps(agent.confirm_action(sub, approved=approved),
                         ensure_ascii=False, indent=2)[:4000])
    elif cmd == "health":
        _, store, _, reranker, llm = build_stack()
        print({"points": store.count(), "llm_mode": llm.mode, "rerank_mode": reranker.mode})
    elif cmd == "stream":
        _cmd_stream(sys.argv[2:])
    elif cmd == "benchmark":
        from maia.benchmark import _print_table, run_benchmark

        _print_table(run_benchmark(n_docs=int(sys.argv[2]) if len(sys.argv) > 2 else 50))
    else:
        print("usage: python -m maia.cli [ingest [--enterprise]|query <q>|chat <q> [--approve] [--crag]|confirm <session> <yes|no>|health|stream <sub>|benchmark]")


if __name__ == "__main__":
    main()
