# MCP Integration (Model Context Protocol)

MAIA speaks the **Model Context Protocol** — JSON-RPC 2.0 with the
`initialize` / `notifications/initialized` handshake, `tools/list` (cursor
paginated) and `tools/call` (content blocks + `isError`). This document is the
operating manual: what is implemented, how to run it, and what it refuses to do.

> **Why not the archived "MCP"?** `_archive/src/maia/mcp/` held read-only GitHub
> and Notion REST connectors *named* MCP, with no protocol framing at all. Those
> connectors stay archived (they are not part of this product surface); the
> protocol implementation below is new code — see
> `docs/adr/0004-marketing-data-plane-promptops-mcp.md`.

## 1. What ships

| Server | Tools | Backing system | Without credentials |
|---|---|---|---|
| `airtable` | `create_marketing_task`, `list_content_calendar`, `update_task_status` | Airtable REST | local JSON store, `dry_run: true` |
| `notification` | `send_email_report`, `send_teams_card`, `send_zalo_oa_message` | SMTP / Teams webhook / Zalo OA | local outbox, `delivered: false` |
| `sql_analytics` | `list_tables`, `describe_table`, `run_sql`, `game_kpi_summary` | SQLite warehouse (read-only) | works on local data |
| `market_insight` | `ingest_reviews`, `ingest_metrics`, `review_sentiment_summary`, `campaign_performance`, `retention_drop_check`, `nl_to_sql` | ingestion + analytics pipeline | works on bundled fixtures |

## 2. Running a server

```bash
# One-shot report of what a server exposes
python -m maia.mcp.bridge --server airtable --list-tools

# Serve over stdio for a real MCP client (Claude Desktop, Cursor, an agent runtime)
python -m maia.mcp.bridge --server sql_analytics
```

Client config snippet:

```json
{
  "mcpServers": {
    "maia-market": {
      "command": "python",
      "args": ["-m", "maia.mcp.bridge", "--server", "market_insight"],
      "env": { "PYTHONPATH": "src" }
    }
  }
}
```

Framing is one JSON-RPC message per line on stdin/stdout. **Nothing else may be
written to stdout** — diagnostics go to stderr.

## 3. In-process use (the API and the agent)

```python
from maia.mcp import MCPClient, make_transport

client = MCPClient("airtable", make_transport("airtable", mode="inprocess"))
client.connect()                       # handshake + tool discovery
schemas = client.tool_schemas()        # OpenAI-style function schemas for the LLM
result = client.call_tool("list_content_calendar", {"limit": 10})
print(result["ok"], result["structured"])
```

`MCP_BRIDGE_TRANSPORT=stdio` switches the client to a subprocess; the code above
is unchanged, which is how the same behaviour is tested in-process *and* over a
real pipe.

## 4. Policy (why a tool call can be refused)

`maia.agent.mcp_dispatch.MCPDispatchService` is the only supported entry point for
agent-driven calls, and it enforces, in this order:

1. **Server availability** — a server that fails to connect is recorded in
   `connect_errors`; the turn continues.
2. **Allowlist** — `MCP_TOOL_ALLOWLIST` (comma-separated `server.tool`). Empty
   means "every tool the connected servers advertise".
3. **Budget** — at most `MCP_MAX_TOOL_CALLS` (default 4) executions per turn; the
   rest land in `skipped` with `reason: "budget_exhausted"`.
4. **Argument schema** — validated by `maia.json_schema_lite` (backed by the
   `jsonschema` library) *before* the handler runs, so a hallucinated argument
   never reaches a side-effecting integration.
5. **Domain guards** — SQL goes through `maia.sql_guard`; ingestion fixtures must
   resolve inside `MARKET_DATA_DIR` (traversal refused).

Every execution is audited to `MCP_AUDIT_LOG_PATH` as JSONL with the tool name, a
**hash** of the arguments, the duration and the outcome — never the values.

## 5. Failure semantics

| Situation | JSON-RPC | Client sees |
|---|---|---|
| Bad JSON / envelope, unknown method | `error` `-32700 / -32600 / -32601` | `MCPError` raised by `_request` |
| Unknown tool / invalid arguments | `error` `-32602` | `{"ok": False, "error": "mcp_error[-32602]: …"}` |
| Tool ran and refused (Airtable rejected) | `result` with `isError: true` | `ok: False`, `structured.error` |
| Subprocess died or timed out | n/a | `{"ok": False, "error": "transport_error: …"}` |

The split matters operationally: *the server is broken* and *the integration said
no* need different responses, and neither is ever reported as a success.

## 6. Idempotency

Retries are normal (a timeout, a user pressing send twice), so every write tool is
content-addressed:

* Airtable tasks carry `external_key`; a repeat returns the existing row with
  `deduplicated: true`.
* Notifications derive `delivery_id` from channel + recipient + subject + body, so
  a retried send is provably the same delivery.
* Ingestion upserts on a content hash (`review_id`) and a composite key
  (`metric_date, game_id, channel, campaign_id`) — a re-run updates in place
  instead of inflating every KPI.

## 7. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `MCP_ENABLED` | `false` | Adds the `mcp_dispatch` LangGraph node (opt-in) |
| `MCP_SERVERS` | `airtable,notification,sql_analytics,market_insight` | Servers to connect |
| `MCP_BRIDGE_TRANSPORT` | `inprocess` | `inprocess` or `stdio` |
| `MCP_TOOL_TIMEOUT_SEC` | `15.0` | Per-call budget |
| `MCP_MAX_TOOL_CALLS` | `4` | Per-turn execution cap |
| `MCP_TOOL_ALLOWLIST` | *(empty)* | `server.tool` allowlist |
| `MCP_AUDIT_LOG_PATH` | `./storage/mcp_audit.jsonl` | Audit sink |
| `AIRTABLE_API_KEY` / `AIRTABLE_BASE_ID` | *(empty)* | Remote Airtable mode |
| `TEAMS_WEBHOOK_URL` / `ZALO_OA_ACCESS_TOKEN` | *(empty)* | Real delivery for those channels |
| `SMTP_HOST` | *(empty)* | Real email delivery |
| `NOTIFICATION_DRY_RUN` | `true` | Force dry-run even with credentials |
| `MARKET_DB_PATH` | `./storage/market.db` | Mini warehouse location |
| `MARKET_DATA_DIR` | `./data/market` | Fixture root for ingestion |

## 8. API surface

Authenticated (`get_current_active_user`), under `/api/v1/market`:

| Route | Purpose |
|---|---|
| `GET /mcp/servers` | configured vs connected servers, tool catalogue |
| `POST /mcp/call` | call one tool under allowlist + budget |
| `POST /mcp/dispatch` | agent-style: plan tool calls from a question, then run |
| `POST /scenarios/run` | run one of the three end-to-end demos |

## 9. Verification

```bash
pytest tests/test_mcp_protocol.py tests/test_mcp_client.py \
       tests/test_mcp_servers.py tests/test_mcp_dispatch.py -q
```

`tests/test_mcp_client.py` spawns `python -m maia.mcp.bridge` as a real
subprocess, so the stdio framing and shutdown path are covered end to end.

