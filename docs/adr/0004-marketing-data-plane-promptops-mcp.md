# ADR-0004 — Marketing/Product data plane, PromptOps and protocol-level MCP

Status: accepted (2026-09-28).

## Context

The product surface was a single-purpose RAG assistant. Three requirements
appeared that the current architecture could not serve honestly:

1. **Game marketing/product analysts** need numbers (CPI, ROAS, review pain
   points) from their own data, not from a document corpus.
2. **Workflows** (publish a campaign, alert a Lead Game) need side effects in
   Airtable / Teams / Zalo / email — the assistant could only read.
3. **Prompts** lived as string constants in Python, so a prompt change was a code
   change with no review surface, no version and no regression test.

An earlier attempt (`_archive/src/maia/mcp/`) was named "MCP" but was a set of
read-only GitHub/Notion REST connectors: no JSON-RPC, no `tools/list`, no
`tools/call`. It was archived under "Simplification Step 1" as out of spec.

## Decision

Add three layers, each offline-capable and independently testable:

1. **`src/maia/promptops/`** — prompts become versioned YAML artefacts with
   bounded parameters, declared guardrails, an output schema, eval cases and a
   content hash. Offline golden suite + regression gate.
2. **`src/maia/pipeline/`** — a mini warehouse (SQLite), idempotent connectors with
   a run ledger, deterministic analytics (lexicon sentiment, topic buckets, KPI
   rollups, KPI-drop detection) and a rule-based NL→SQL planner that **refuses**
   rather than guessing. Remote collection is opt-in.
3. **`src/maia/mcp/`** — a real MCP implementation (JSON-RPC 2.0, handshake with
   version negotiation, paginated `tools/list`, `tools/call` with content blocks
   and `isError`), four integration servers, in-process and stdio-subprocess
   transports, and a `python -m maia.mcp.bridge` entry point for external clients.

## Why not restore the archived code

The archived connectors solve a different problem (read-only access to GitHub /
Notion) and drag in a `requests` dependency. Restoring them into `src/` would have
made "MCP" mean two different things. They stay archived; this ADR records the name
collision so the next reader does not assume the two are the same thing.

## Why the agent graph is unchanged by default

`MCP_ENABLED=false` by default. The LangGraph node `mcp_dispatch` and its edge are
added **only** when the flag is on, so the existing control flow, its 524-test suite
and the RAG contract are untouched. Tool calls are additionally gated by
`MCP_TOOL_ALLOWLIST` and `MCP_MAX_TOOL_CALLS`, and audited without argument values.

## Consequences

* Two answer paths coexist: grounded RAG (documents) and live integrations (data).
  A marketing question routed to retrieval would be answered from the wrong corpus,
  hence the opt-in router branch.
* Credentials are optional by construction: absent keys produce a local store and a
  visible `dry_run: true` / `delivered: false` — never a fake success.
* The prompt library is a compatibility surface: a prompt change is now a PR with a
  diff, a changelog and a gate.
* Open cost: +217 offline tests and two more configuration surfaces
  (`MCP_*`, `MARKET_*`).
