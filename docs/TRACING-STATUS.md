# Tracing status — MAIA (Byte/FPT observability evidence)

## Hiện tại (chạy thật)
- `PipelineTracer` (`src/maia/pipeline_wiring.py` + `observability.py`): 7 stages
  `query_input → retrieval → rerank → evidence_gate → guardrail → generation → response`,
  mỗi request có `correlation_id`, redacted khi export (`finalize_redacted()`).
- Per-query reliability: `pipeline_query._track_ai_query` → registry → `GET /metrics/ai`
  (latency p95/avg, tokens, cost-saved, refusal/citation rates) + Prometheus `/metrics`.
- llm-gateway telemetry JSONL (`logs/llm-telemetry.jsonl`): latency/project/model,
  không lưu prompt body.

## Đã có (opt-in) — OpenTelemetry OTLP exporter
- `src/maia/tracing.py` (`get_tracer()` / `init_tracing()` / `span_context()` /
  `start_span()`): NoOp khi `OTEL_ENABLED=false` (default) hoặc thiếu endpoint /
  packages — mọi import opentelemetry đều lazy + `try/except`.
- `pipeline_query.query()` mở root span `maia.query`, mỗi stage
  (`maia.retrieval` → `maia.rerank` → `maia.evidence_gate` → `maia.guardrail` →
  `maia.generation` → `maia.response`) là span con; attributes chỉ chứa
  structural signal (counts, scores, flags) qua `sanitize_attributes()` reuse
  `observability.REDACTED_FIELDS` — không raw query/chunk/PII.
- Bật: `OTEL_ENABLED=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector>:4318/v1/traces`
  (+ `OTEL_SERVICE_NAME`, default `maia`). Packages:
  `opentelemetry-api/sdk/exporter-otlp==1.45.0` trong `requirements.txt`.
- Tests: `tests/test_tracing.py` (NoOp khi disabled, spans + parent-child qua
  `InMemorySpanExporter`, assert không leak PII, wiring `query()`).
- Reference port: `FlashSale-Backend/src/Order.Api/Program.cs`.

## Chưa có (ghi rõ để không bị coi khai khống)
- Chưa có Loki/ELK central log. Hiện tại: JSON logger + gateway JSONL + Prometheus.
