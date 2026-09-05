"""Loop 5 - Reliability / Operations.

Production observability & self-healing for the ingestion path:

  * retry / backoff / DLQ  - handled in the embedding worker (§8)
  * consumer lag           - maia_kafka_consumer_lag
  * queue backlog          - maia_queue_backlog (produced - consumed)
  * worker utilization     - maia_worker_utilization
  * embedding p95          - maia_embedding_latency_p95

Adds:
  * HealthReport     - snapshot of the ops metrics from a transport + registry
  * alert            - threshold-based alerting hook (lag / failures) -> callable
  * should_scale_workers - autoscaling decision table (lag -> replicas)
"""
from __future__ import annotations

from typing import Callable

# Stream metrics registry (defined in maia.stream.metrics) for ops health reports.
from maia.stream.metrics import MetricsRegistry, registry


def alert(metric: str, value: float, threshold: float, unit: str = "",
          handler: Callable[[str], None] | None = None) -> bool:
    """Raise an alert if `value` crosses `threshold`. Returns True when alerted.

    `handler` is an optional sink (SMS/email/PagerDuty/webhook); default prints.
    """
    fired = value > threshold
    if fired:
        msg = f"[MAIA ALERT] {metric} = {value:.2f}{' ' + unit if unit else ''} > {threshold}"
        if handler:
            handler(msg)
        else:  # default: log to console
            print(msg)
    return fired


def should_scale_workers(lag: float) -> int:
    """Autoscaling policy (spec §10): lag thresholds -> worker replicas."""
    if lag > 500:
        return 8
    if lag > 100:
        return 4
    return 2


def health_report(transport, group: str, topic: str,
                  metrics: MetricsRegistry = registry,
                  alert_handler: Callable[[str], None] | None = None) -> dict:
    """Collect the Loop-5 ops snapshot from a transport + metric registry."""
    lag = float(transport.lag(group, topic))
    produced = float(transport.total_produced(topic))
    committed = float(transport.committed(group, topic)) if hasattr(transport, "committed") else produced - lag
    queue_backlog = max(0.0, produced - committed)
    p95 = metrics.latency_p95()
    failures = metrics.get("maia_embedding_failures_total")
    util = metrics.get("maia_worker_utilization")

    alerts = []
    if alert("consumer_lag", lag, 500, "msgs", alert_handler):
        alerts.append("consumer_lag>500")
    if alert("embedding_failures", failures, 10, "count", alert_handler):
        alerts.append("embedding_failures>10")

    return {
        "consumer_lag": int(lag),
        "queue_backlog": int(queue_backlog),
        "produced": int(produced),
        "committed": int(committed),
        "embedding_p95_seconds": round(p95, 4),
        "worker_utilization": round(util, 3),
        "embedding_failures": int(failures),
        "recommended_workers": should_scale_workers(lag),
        "alerts": alerts,
    }