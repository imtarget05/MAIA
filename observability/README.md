# Observability - MAIA

Self-contained Prometheus + Grafana + Alertmanager stack for this repo. It lives
here rather than in a shared folder, so cloning **this** repository is enough to
see the whole runtime picture - which is what a reviewer, a demo or a debugging
session actually needs.

## Quickstart

```bash
cd MAIA/observability
docker compose up -d
docker compose config                     # validate without starting
```

| Service | Port | URL |
|---|---|---|
| Prometheus | 9101 | http://localhost:9101 (`Status -> Targets`) |
| Grafana | 3201 | http://localhost:3201 (admin / `$GF_SECURITY_ADMIN_PASSWORD`, default `admin`) |
| Alertmanager | 9301 | http://localhost:9301 |

Reload a config change without a restart:

```bash
curl -XPOST http://localhost:9101/-/reload
```

## Scrape targets

| Job | Metrics path | Port | Service |
|---|---|---|---|
| `llm-gateway` | `/metrics` | 8787 | vendored `llm-gateway/` |
| `maia` | `/metrics` | 8000 | MAIA |

## Dashboards

- `project-maia.json` - MAIA - RAG ingestion throughput, Kafka lag, embedding p95, CRAG retrievals
- `golden-signals.json` - Golden signals - QPS / error rate / P95 / saturation per scrape job
- `llm-platform.json` - LLM platform - traffic, latency, token usage, cost governance
- `ai-quality.json` - AI quality - PII redactions, quota denials, grounding score

## Metrics this repo exposes

| Endpoint | Service | Series |
|---|---|---|
| `GET /metrics` | FastAPI (`src/maia/api.py`) | `maia_ingestion_throughput`, `maia_kafka_consumer_lag`, `maia_queue_backlog`, `maia_worker_utilization`, `maia_embedding_latency_seconds` histogram + `maia_embedding_latency_p95`, `maia_conversations_total`, `maia_tool_calls_total`, `maia_crag_retrievals_total` |
| `GET /metrics` | llm-gateway (vendored) | `llm_requests_total`, `llm_latency_seconds`, `llm_tokens_total`, `llm_upstream_up`, `llm_circuit_breaker_open` |

## Alerts

`prometheus/alerts.yml` has two groups. `gateway`: upstream down, scrape down, 5xx ratio > 5%, P95 > 2s, circuit breaker open. `services`: any scrape target down for 2m.

## SLOs

`slo.yaml` holds the machine-readable SLI / target / window / error-budget table.

## Operational notes

- Grafana `admin` + a default password is fine for a local demo. For anything
  shared, set `GF_SECURITY_ADMIN_PASSWORD` and keep anonymous access off.
- Every `/metrics` endpoint is aggregate-only and unauthenticated, because a
  Prometheus scraper carries no session cookie. Business detail stays behind the
  existing auth-protected endpoints.
- A target that is not running shows `DOWN`; it never blocks the other jobs.
- Ports are offset per project (Prometheus 9101) so several portfolios can run
  at the same time. Override with `PROMETHEUS_PORT` / `GRAFANA_PORT` /
  `ALERTMANAGER_PORT`.
- Docker is required. CI asserts these configs parse; it does not start the stack.
