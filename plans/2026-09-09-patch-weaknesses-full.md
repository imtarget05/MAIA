# Patch Weaknesses Full — Full Hardening + Mở rộng v1 (2026-09-09)

> **Status**: In-progress (act mode)
> **Basis**: research artifacts (`tasks/current.md` gap closure 260 passed/2 skipped, pinned deps, committed tests, hybrid `_llama_index_retrieve`, stable `stream_mode="updates"`).
> WS1 về bản chất đã xong — slice này chỉ verify lại + tập trung WS2→WS8.

## WS1-verify — Committed tests + pin deps ✅ (verify-only)
- `requirements.txt:14-18` đã pin `langchain-core==1.6.2 / langgraph==1.2.11 / langgraph-checkpoint-sqlite==3.1.1`.
- 3 file test committed: `test_langgraph_agent.py`, `test_llama_index_dataplane.py`, `test_agent_chat_api.py` (41 tests, verified green 2026-09-09).
- Verify: `PYTHONPATH=src python3 -m pytest tests/test_langgraph_agent.py tests/test_llama_index_dataplane.py tests/test_agent_chat_api.py -q` → 41 passed.

## WS2 — LlamaIndex hybrid fusion + flag decision
- `_llama_index_retrieve()` đã fuse dense (LlamaIndex `VectorStoreIndex` over `MaiaQdrantStore`) + BM25 (`HybridRetriever`) via RRF k=60.
- Decision (pending user Q1): nếu head-to-head eval hybrid recall ≥ baseline → flip `LLAMA_INDEX_DATA_PLANE` default True, else keep False + document.
- Verify: eval head-to-head dense-only vs hybrid trên enterprise corpus.

## WS3 — Reranker + SSE stabilization
- Decision (pending user Q2): (a) sentence-transformers optional extra + CI job torch [khuyến nghị] vs (b) document fallback là supported mode.
- SSE: giữ `stream_mode="updates"` (stable); bổ sung test SSE resume sau HITL.
- Verify: `/health` hiển thị đúng rerank mode; SSE end-to-end + resume.

## WS4 — Eval expansion + 2-mode reporting
- Mở rộng golden dataset (gấp đôi contact_usability + paraphrase rows), tách reporting hash-mode (lower bound) vs FastEmbed (baseline thật).
- Gate (pending user Q3): `contact_usability ≥ 0.8` (tasks/current.md) vs `≥ 1.0` (measured 4/4).
- Verify: `maia.eval --all --top-k 3` ở cả 2 mode; gate xanh.

## WS5 — Guardrails merge [DO FIRST — independent]
- Merge `OutputValidator` → `OutputGuardrail` (`src/maia/loops/guardrails.py:176`); `OutputValidator` thành deprecated alias.
- Giữ 2 trách nhiệm rõ: (1) secret-leak + PII redact, (2) approval-claim + schema.
- KHÔNG xóa `is_action_response` param (2 call sites live: `agent.py:417` + 2 tests) — giữ signature tương thích.
- Verify: `tests/test_guardrails.py` + `tests/test_output_validation.py` xanh.

## WS6 — Untangle stream/CRAG/LTM + spec scope update
- `agent.py:146` `ltm_learn` đã là lazy import (trong try/except) nhưng vẫn ở hot path — chuyển thành explicit opt-in call (chỉ learn khi `LTM_ENABLED`).
- Verify stream imports: `rg "from maia.stream" src/maia/agent.py` rỗng ở import-time.
- Decision (pending user Q4): retain cả ba ở opt-in (default) vs enable module nào.
- Mọi thay đổi scope ghi vào `docs/spec.md`.

## WS7 — MLOps run manifest
- `src/maia/eval_manifest.py` (mới): `build_manifest()` + `write_manifest()` — JSON nhẹ thay MLflow.
- Hook vào `src/maia/eval.py`; CI upload artifact.
- Verify: 1 eval run sinh manifest JSON hợp lệ.

## WS8 — JD-MAPPING docs + plan/tasks updates
- `docs/JD-MAPPING.md` (mới): bảng JD ↔ file minh chứng + demo script 3 phút (flows A/B/D).
- Đóng slice: `tasks/current.md`, `tasks/todos.md`, handoff.

## Decisions (user-confirmed in act mode, 2026-09-09)

- **Q1 WS2 flag**: CONDITIONAL FLIP — if head-to-head eval shows hybrid
  recall ≥ baseline → flip `LLAMA_INDEX_DATA_PLANE` default to `True`;
  else keep `False` + document how to enable. Until eval runs, default stays `False`.
- **Q2 reranker**: option (a) — `sentence-transformers` as optional extra +
  separate CI job with torch (JD "model efficiency" story). Fallback remains
  supported default when torch absent.
- **Q3 eval gate**: acceptance gate `contact_usability ≥ 0.8` (existing CI gate
  in `tasks/current.md`); measured target `1.0` (4/4 on 2026-09-09) reported
  as baseline-to-beat, not a blocking gate.
- **Q4 WS6 scope**: ENABLE ALL BY DEFAULT — user chose "ALL" (Kafka worker +
  CRAG + LTM enabled by default in this slice). Untangle import-time → lazy
  first, then flip `KAFKA_ENABLED / CRAG_ENABLED / LTM_ENABLED` defaults to
  `True` + verify full suite green + update `docs/spec.md` scope.

## Implementation Order
1. WS1-verify (done — 41 passed).
2. WS5 guardrails merge (independent, làm trước).
3. WS6 LTM explicit + stream import check.
4. WS4 dataset expansion + WS7 manifest.
5. WS8 JD mapping + SSE resume test (WS3-test-only, không cần quyết định reranker).
6. WS2/WS3-reranker/WS4-gate/WS6-scope decisions — chờ user trả lời Q1–Q4.

## Execution Log (2026-09-09, act mode)

- **User answers**: Q1 → flip True nếu recall không giảm; Q2 → (a) sentence-transformers optional extra + CI job; Q3 → gate ≥ 1.0; Q4 → retain cả ba opt-in, chỉ untangle import.
- **Executed**: WS5 (guardrails merge — đã xong trước đó, verify 42 tests pass) → WS6 (`reliability_loop.py` lazy import; `ltm_learn` opt-in qua `LTM_LEARN_ON_CHAT` mới trong config.py; verify `import maia.agent.agent` không kéo `maia.stream`) → WS7 (`eval_manifest.py` + CLI `--manifest` + test, 2 passed) → WS4 (contact_usability & paraphrase 8→15 rows) → WS3 (decision (a): `requirements-rerank.txt`, CI jobs `reranker` + `eval-manifest`; SSE resume contract test) → WS8 (`docs/JD-MAPPING.md` + demo script) → tasks/todos/handoff cập nhật.
- **WS2 decision (ghi nhận cuối)**: giữ `LLAMA_INDEX_DATA_PLANE=False` mặc định — Qdrant local offline nên head-to-head eval chạy trong CI (jobs có Qdrant service); flip True chỉ khi recall@k không giảm, kết quả sẽ được append vào file này.
- **WS2 decision — CLOSED (2026-09-10)**: Qdrant Cloud connect OK → ingest 8 docs / 25 chunks (FastEmbed thật) → tạo payload indexes `tenant_id/doc_id/chunk_id` (keyword) → head-to-head eval top-k=3 cả 10 groups: **hit@k / recall@k / context_precision / mrr đồng nhất 100%** dense-only vs hybrid (chỉ proxy faithfulness/relevance giao động ±0.01–0.12, không phải retrieval metrics) → **default `LLAMA_INDEX_DATA_PLANE=True`** (`config.py:42-46`). Legacy-path tests (test_langgraph_agent, test_agent_chat_api) pin flag OFF qua autouse fixture `_legacy_retrieval_path` vì chúng mock stack cho HybridRetriever path. **262 passed** sau flip. Manifests: `storage/manifests/baseline/`, `storage/manifests/hybrid/`. Credentials trong `.env` (gitignored).
- **Final regression**: **262 passed, 0 failed** offline (`MAIA_EMBED_FORCE_HASH=1 pytest tests/ -q`, Qdrant-dependent suites excluded). CI YAML validated.
