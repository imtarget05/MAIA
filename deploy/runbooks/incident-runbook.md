# Incident Runbook — MAIA (FPT AI Camera evidence)

## Khi nào dùng
Alert `ServiceDown`, `HighErrorRate`, `GroundingLow`, `QdrantDown`, `LLMUpstreamDown` từ `observability/prometheus/alerts.yml`.

## 5 phút đầu (triage)
1. `curl -s localhost:8001/health && curl -s localhost:8001/ready` — phân biệt live vs ready.
   - `/health` 200 + `/ready` 503 → dependency (Qdrant/LLM), không phải API chết.
2. `curl -s localhost:8001/metrics | head -40` — xem `http_requests_total`, embed p95, tool counters.
3. `docker logs maia-api --tail 100` — tìm `correlation_id` + stage `query_input..response` (PipelineTracer).
4. `curl -s localhost:8787/admin/stats | jq .projects.MAIA` — xem gateway latency_p95, error_rate, quota.

## Phân loại
| Dấu hiệu | Nguyên nhân likely | Lệnh |
|---|---|---|
| `ready: qdrant unreachable` | Qdrant down | `docker restart qdrant; curl localhost:6333/collections` |
| `ready: llm unreachable` | LM Studio/Ollama tắt | `curl $LLM_BASE_URL/models`; fallback `MAIA_LLM_MODE=mock` để giữ demo |
| grounding_score < 0.9 | corpus thiếu/tenant leak | `python src/maia/eval.py --group no_answer,unauthorized` |
| latency p95 > 2s | embed/rerank chậm | tắt rerank: `RERANK_MODE=fallback`; check `storage/bm25_corpus_*.json` |

## Ghi incident
Append vào `storage/incidents.jsonl`: `{ts, alert, root_cause, evidence_lines, action, resolved}`.
Đóng alert trong Alertmanager sau khi `/ready` 200 lại 3 lần liên tiếp.
