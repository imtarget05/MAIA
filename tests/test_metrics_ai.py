"""Byte JD evidence: GET /metrics/ai returns real per-query reliability accounting.

Marked ``infra``: it drives two queries through ``pipeline_query.query``, which
reads the real vector store. It passes with no LLM (conftest forces
``MAIA_EMBED_FORCE_HASH=1``; the LLM falls back to mock), but it still needs a
running Qdrant — so it belongs to the manual live/infra job
(``ci-live.yml``), not to the default offline suite. Previously unmarked, which
made it a false failure in every environment without Qdrant.
"""
import time

import pytest
from fastapi.testclient import TestClient

from maia import api
from maia.loops.metrics import registry


def _query_with_retry(question, top_k_final=1, attempts=3):
    """Infra-flake tolerance: retry transport timeouts (slow Docker Qdrant
    under load), but keep every assertion strict — never fake a pass."""
    from maia import pipeline_query as pq

    last = None
    for i in range(attempts):
        try:
            return pq.query(question, top_k_final=top_k_final)
        except Exception as exc:  # noqa: BLE001 - transport flake only
            if "timed out" not in str(exc).lower() and "timeout" not in type(exc).__name__.lower():
                raise
            last = exc
            time.sleep(5 * (i + 1))
    raise last


@pytest.mark.infra
def test_metrics_ai_reflects_real_queries():
    c = TestClient(api.app)
    before = c.get("/metrics/ai").json()
    assert set(before) >= {"queries_total", "latency_p95_s", "tokens_total_est",
                           "cost_saved_usd_est", "refusal_rate", "citations_per_answer"}
    # Drive two real queries through the instrumented pipeline (mock LLM offline).
    r1 = _query_with_retry("What is the leave policy?", top_k_final=1)
    r2 = _query_with_retry("zxqv nonsense lacking evidence 12345", top_k_final=1)
    assert "answer" in r1 and "answer" in r2
    after = c.get("/metrics/ai").json()
    assert after["queries_total"] == before["queries_total"] + 2
    assert after["tokens_total_est"] > before["tokens_total_est"]
    assert after["latency_p95_s"] >= 0
    assert 0.0 <= after["refusal_rate"] <= 1.0
    # Registry counters backing /metrics and /metrics/ai are the same object.
    assert registry.get("maia_queries_total") == after["queries_total"]
