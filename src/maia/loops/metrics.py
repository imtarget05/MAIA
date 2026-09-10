"""Prometheus-style metrics for MAIA loops.

Dependency-free registry with text exposition (Prometheus scrape format) and
an embedding-latency histogram that computes p95 on the fly.

Exposed metrics (https://prometheus.io/docs/instrumenting/writing_exporters/):
  maia_documents_processed_total
  maia_chunks_created_total
  maia_embeddings_total
  maia_embedding_latency_seconds        (summary samples -> p95)
  maia_embedding_latency_p95
  maia_embedding_failures_total
  maia_kafka_consumer_lag
  maia_ingestion_throughput             (chunks/sec)
  maia_worker_utilization               (active workers / assigned workers)
  maia_crag_retrievals_total            (CRAG retrievals)
"""
import math
import threading
from collections.abc import Sequence

_BUCKETS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]


def _percentile(sorted_vals: Sequence[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    lo, hi = sorted_vals[int(f)], sorted_vals[int(c)]
    return lo + ((k - f) * (hi - lo))


class MetricsRegistry:
    """Thread-safe metric store with Prometheus text exposition."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        # embedding latency in seconds: raw samples kept capped for p95.
        self._latency_samples: list[float] = []
        self._latency_hist: dict[float, int] = {b: 0 for b in _BUCKETS}
        self._latency_sum = 0.0

    # ---- numeric helpers -------------------------------------------------
    def inc(self, name: str, by: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + by

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def get(self, name: str) -> float:
        with self._lock:
            if name in self._counters:
                return float(self._counters[name])
            return float(self._gauges.get(name, 0.0))

    def observe_latency(self, seconds: float) -> None:
        with self._lock:
            self._latency_samples.append(seconds)
            self._latency_sum += seconds
            for b in _BUCKETS:
                if seconds <= b:
                    self._latency_hist[b] += 1

    def latency_p95(self) -> float:
        with self._lock:
            return _percentile(sorted(self._latency_samples), 0.95)

    def latency_avg(self) -> float:
        with self._lock:
            n = len(self._latency_samples)
            return self._latency_sum / n if n else 0.0

    def latency_count(self) -> int:
        with self._lock:
            return len(self._latency_samples)

    # ---- Prometheus text exposition -------------------------------------
    def render(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hist = dict(self._latency_hist)
            latency_sum = self._latency_sum
            latency_n = len(self._latency_samples)
            p95 = _percentile(sorted(self._latency_samples), 0.95)

        lines = [
            "# HELP maia_ingestion_throughput Chunks ingested per second.",
            "# TYPE maia_ingestion_throughput gauge",
            f'maia_ingestion_throughput {_g(gauges.get("maia_ingestion_throughput", 0))}',
            "# HELP maia_docs_per_minute Documents ingested per minute.",
            "# TYPE maia_docs_per_minute gauge",
            f'maia_docs_per_minute {_g(gauges.get("maia_docs_per_minute", 0))}',
            "# HELP maia_kafka_consumer_lag Total uncommitted messages behind.",
            "# TYPE maia_kafka_consumer_lag gauge",
            f'maia_kafka_consumer_lag {_g(gauges.get("maia_kafka_consumer_lag", 0))}',
            "# HELP maia_queue_backlog Produced minus consumed messages (backlog).",
            "# TYPE maia_queue_backlog gauge",
            f'maia_queue_backlog {_g(gauges.get("maia_queue_backlog", 0))}',
            "# HELP maia_worker_utilization Fraction of assigned workers actively pulling.",
            "# TYPE maia_worker_utilization gauge",
            f'maia_worker_utilization {_g(gauges.get("maia_worker_utilization", 0))}',
            "# HELP maia_conversations_total Total chat conversations.",
            "# TYPE maia_conversations_total counter",
            f'maia_conversations_total {_g(counters.get("maia_conversations_total", 0))}',
            "# HELP maia_tool_calls_total Total tool calls (leave/it).",
            "# TYPE maia_tool_calls_total counter",
            f'maia_tool_calls_total {_g(counters.get("maia_tool_calls_total", 0))}',
            "# HELP maia_embedding_latency_seconds Embedding+upsert latency distribution.",
            "# TYPE maia_embedding_latency_seconds histogram",
        ]
        for bucket in sorted(hist):
            lines.append(f'maia_embedding_latency_seconds_bucket{{le="{_g(bucket)}"}} {hist[bucket]}')
        lines.append(f'maia_embedding_latency_seconds_bucket{{le="+Inf"}} {latency_n}')
        lines.append(f"maia_embedding_latency_seconds_sum {_g(latency_sum)}")
        lines.append(f"maia_embedding_latency_seconds_count {latency_n}")
        lines.append("# HELP maia_embedding_latency_p95 Embedding+upsert latency p95 in seconds.")
        lines.append("# TYPE maia_embedding_latency_p95 gauge")
        lines.append(f"maia_embedding_latency_p95 {_g(p95)}")

        for name in sorted(counters):
            if name in ("maia_conversations_total", "maia_tool_calls_total"):
                continue
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {_g(counters[name])}")
        for name in sorted(gauges):
            if name in ("maia_ingestion_throughput", "maia_kafka_consumer_lag",
                        "maia_worker_utilization", "maia_embedding_latency_p95",
                        "maia_docs_per_minute", "maia_queue_backlog"):
                continue
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {_g(gauges[name])}")
        return "\n".join(lines) + "\n"


def _g(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.6g}"


# module-level default registry shared by loops
registry = MetricsRegistry()
