# MCP · RAG · AI AGENT — MAIA

> Enterprise Target. MCP + RAG + AI Agent appear in **all three** repos, but with **different roles**. See [`ROADMAP.md`](./ROADMAP.md) and [`AI-PRODUCTION-GATE.md`](./AI-PRODUCTION-GATE.md).

## Division of responsibility

```text
RAG        = retrieve the right knowledge
AI Agent   = decide the next step and coordinate work
MCP        = standardize how Agent/LLM reaches tools, data and external systems
```

## Agent must never bypass security

```text
Agent → Typed Intent → Schema Validation → RBAC / Tenant Check
      → Risk Check → HITL if required → Deterministic Tool
```

## Role by repository

| Repo | RAG | AI Agent | MCP |
|---|---|---|---|
| **MAIA** | ⭐ Core | ⭐ Core | ⭐ Core |
| **Helpdesk** | ⭐ Core (ITIL / runbook) | ⭐ Core (support workflow) | ⭐ Core (tool integration) |
| **Factory** | 🟡 Supporting | 🟡 Supporting (investigation / reporting) | ⭐ Core (integration layer) |

**`MCP ≠ Agent`.** MCP is a *protocol* for exposing resources / prompts / tools. The **Agent** decides *when* a tool is needed; **MCP** defines *how* it is called; **RAG** supplies knowledge for reasoning.

---

## This repository — MAIA

**Identity: Production Agentic RAG + MCP AI Platform.**

### Architecture

```text
                 FastAPI / Auth
                       │
                 Agent Orchestrator
                 /       |        \
              Planner  Reviewer  Executor
                 │
                 ├──── RAG
                 │      Embedding → Dense + BM25 → RRF → Reranker
                 │      → Tenant filter → Context Builder → LLM → Citation
                 │
                 └──── MCP Client
                          ├ Knowledge MCP   (Qdrant / Azure AI Search)
                          ├ Search MCP      (external APIs)
                          └ Action MCP      (approved tools)
```

### RAG (core)

```text
Query → Embedding → Dense + BM25 → RRF → Cross-encoder reranker
      → Tenant filter → Context Builder → LLM → Citation
```
Metrics: `Recall@K`, `Precision@K`, `MRR`, `NDCG`, `Faithfulness`, `Groundedness`, `Citation Accuracy`, `Abstention Accuracy`.

### Agent (core)

Orchestration only: `Planner → retrieve → decide tool need → tool call → Reviewer → answer / HITL`.
Bounds required: `max_steps`, `max_tool_calls`, timeout, token budget, cost budget, durable checkpoint, resume.
PostgreSQL stores: conversation, agent state, LangGraph checkpoint, HITL state.

### MCP (core)

```text
mcp/
├── knowledge-server
├── search-server
├── document-server
├── database-server
└── safe-actions-server
```

Tools: `knowledge.search`, `document.get`, `document.metadata`, `search.web`, `database.query_readonly`, `action.create_task`.
**Never expose:** `shell.execute`, `sql.execute_raw`, `delete_anything`.

### MAIA demonstrates

```text
RAG Engineering + Agent Engineering + MCP Integration + Production AI
```

### Example end-to-end request

> User: "Why did yesterday's deployment fail?"

```text
User → Auth → Agent → Planner
  → RAG: retrieve deployment docs
  → MCP: deployment.get_run
  → MCP: logs.search
  → Agent reasoning → Reviewer → Grounded answer → SSE
```

---

## PHASE 6E — RAG Production Hardening

MAIA **mandatory**. Helpdesk applies to its KB. Factory applies to docs/SOP only.

**Gate:**
```text
[ ] retrieval ACL
[ ] tenant isolation
[ ] hybrid actually executes
[ ] reranker actually executes
[ ] empty retrieval behavior
[ ] no-answer behavior
[ ] chunk provenance
[ ] source identity
[ ] citation mapping
[ ] Recall@K
[ ] Precision@K
[ ] MRR
[ ] NDCG
[ ] retrieval regression gate
```

## PHASE 6F — MCP Tool Platform

```text
Implement PHASE 6F — MCP TOOL PLATFORM.

Do NOT use MCP merely as a wrapper around arbitrary code execution.

For each project inventory existing deterministic business capabilities and
expose only appropriate capabilities through typed MCP tools/resources.

MAIA:     knowledge / search / document / safe-action MCP.
Helpdesk: ticket / asset / knowledge / directory / approved-automation MCP.
Factory:  KPI / batch / quality / lineage / document / job MCP.

Every MCP tool must have:
  name
  version
  input schema
  output schema
  authorization policy
  tenant policy
  timeout
  retry classification
  side-effect classification
  idempotency semantics
  audit contract

Separate READ and WRITE tools.

High-risk write tools must never be directly executable by an LLM without
the existing deterministic authorization/HITL layer.

Treat every MCP response as untrusted external data when returning it to an
agent. Add indirect prompt-injection tests.

Instrument every call with: request_id, trace_id, agent_run_id, tool_call_id.

Add MCP negative controls for:
  unauthorized caller
  wrong tenant
  schema violation
  server timeout
  server unavailable
  malicious tool output
  duplicate write
  unsupported tool version.

No project may depend on another project's MCP server for its core
availability. Finish one repository before starting the next.
```

## PHASE 6G — Agent + RAG + MCP End-to-End

The strongest gate. Prove the **complete business workflow**, not each subsystem alone.

```text
MAIA:     question → agent → RAG → MCP → reasoning → SSE → citation
Helpdesk: ticket → RAG → agent → MCP read tools → proposal → HITL → queue → executor
Factory:  anomaly → deterministic gate → agent → MCP → RAG SOP → evidence-backed report
```

```text
Execute PHASE 6G — AGENT + RAG + MCP END-TO-END VALIDATION.

The purpose is not to prove each subsystem independently. Prove the complete
business workflow.

MAIA:
  authenticated tenant request
  → agent
  → tenant-filtered retrieval
  → MCP tool
  → grounded response
  → true SSE
  → citations.

Helpdesk:
  ticket
  → runbook retrieval
  → MCP investigation
  → typed proposal
  → deterministic authorization
  → HITL
  → queued execution
  → post-check
  → audit.

Factory:
  quality anomaly
  → deterministic quality decision
  → MCP evidence collection
  → RAG SOP/manual retrieval
  → investigation agent
  → structured report.

Inject failures at each boundary. Required failures:
  RAG unavailable
  MCP unavailable
  tool timeout
  LLM provider unavailable
  invalid tool output
  prompt injection in tool output
  agent max-step reached
  duplicate write delivery.

The system must fail predictably without bypassing authorization or
fabricating evidence.

Only after all three workflows pass positive and negative E2E tests may
Phase 6G be marked VERIFIED.
```

## MCP PRODUCTION READINESS (add to FINAL FREEZE)

```text
[ ] MCP tool schema versioned
[ ] tool input schema validation
[ ] tool output schema validation
[ ] per-tool authorization
[ ] tenant authorization
[ ] tool allowlist
[ ] no arbitrary shell tool
[ ] no unrestricted SQL tool
[ ] read vs write tools separated
[ ] risky tools require HITL
[ ] tool timeout
[ ] retry policy
[ ] idempotency for write tools
[ ] MCP server authentication
[ ] MCP transport encrypted
[ ] secrets never exposed as MCP resources
[ ] tool output treated as untrusted data
[ ] indirect prompt injection tested
[ ] audit every tool call
[ ] request_id / trace_id / agent_run_id / tool_call_id
[ ] latency metrics
[ ] error metrics
[ ] tool success rate
[ ] MCP server unavailable test
[ ] malformed tool response test
[ ] unauthorized tool call test
[ ] tool version compatibility test
```

## Monitoring additions (Agent + MCP + RAG)

Prometheus:
```text
rag_retrieval_total · rag_retrieval_duration_seconds · rag_empty_result_total
agent_runs_total · agent_steps_total · agent_tool_calls_total
agent_budget_exceeded_total · agent_loop_blocked_total
mcp_requests_total · mcp_request_duration_seconds · mcp_errors_total · mcp_unauthorized_total
tool_calls_total · tool_failures_total · tool_retries_total
llm_ttft_seconds · llm_tokens_total · llm_cost_estimate
```

Grafana dashboards: **RAG Quality** · **Agent Runtime** · **MCP / Tool Health**.

Trace:
```text
HTTP → agent.run → agent.plan
        ├─ rag.retrieve (embedding · vector.search · bm25 · reranker)
        └─ mcp.tool → external dependency
      → llm → SSE
```
