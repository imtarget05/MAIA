"""API surface for PromptOps, MCP tools and the marketing/product pipeline.

Mounted under ``/api/v1/market`` by :mod:`maia.api`. Every route is authenticated
with the same ``get_current_active_user`` dependency as the rest of the service —
a marketing pipeline that creates Airtable rows and sends emails must not be
reachable anonymously.

Routes, grouped by what a reviewer wants to see working:

* **Prompts** — list versions, diff two versions, run the offline eval suite.
* **MCP** — list tools/servers, call one tool, run the agent-style dispatch.
* **Data** — ingest fixtures, query the warehouse, ask a business question.
* **Scenarios** — run the three end-to-end showcase flows.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .agent.mcp_dispatch import MCPDispatchService
from .config import settings
from .mcp.servers import SERVER_NAMES
from .pipeline import (
    FileConnector,
    MarketWarehouse,
    ingest_metrics,
    ingest_reviews,
    plan_sql,
    rule_rules,
)
from .promptops import PromptEvalError, default_registry, run_prompt_evals

__all__ = ["market_router"]

market_router = APIRouter(prefix="/api/v1/market", tags=["market"])


def _warehouse() -> MarketWarehouse:
    return MarketWarehouse(settings.MARKET_DB_PATH)


def _require_user(current_user: Any) -> Any:
    """Dependency placeholder (resolved in api.py against the auth module).

    Kept as a name so the router can be imported (and unit-tested) without
    pulling in the whole FastAPI auth stack.
    """
    return current_user


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
@market_router.get("/prompts")
def list_prompts() -> dict[str, Any]:
    """Every prompt version with its status, owner and content hash."""
    registry = default_registry()
    return {
        "library": str(settings.PROMPTS_DIR),
        "errors": [e.error for e in registry.errors],
        "prompts": [
            {
                "name": s.name,
                "version": s.version,
                "ref": s.ref,
                "category": s.category,
                "status": s.status,
                "owner": s.owner,
                "supersedes": s.supersedes,
                "temperature": s.parameters.temperature,
                "eval_cases": len(s.eval_cases),
                "content_hash": s.content_hash(),
            }
            for s in registry.all()
        ],
    }


@market_router.get("/prompts/{name}/versions")
def prompt_versions(name: str) -> dict[str, Any]:
    registry = default_registry()
    versions = registry.versions(name)
    if not versions:
        raise HTTPException(status_code=404, detail=f"unknown prompt {name!r}")
    current = registry.current(name)
    return {
        "name": name,
        "current": current.ref if current else None,
        "versions": [
            {"ref": s.ref, "status": s.status, "content_hash": s.content_hash(),
             "changelog": s.changelog}
            for s in versions
        ],
    }


@market_router.get("/prompts/{name}/diff")
def prompt_diff(name: str, left: str, right: str) -> dict[str, Any]:
    """Field-level diff between two prompt versions (the PR-review view)."""
    registry = default_registry()
    try:
        diff = registry.diff(f"{name}@{left}", f"{name}@{right}")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "left": diff.left,
        "right": diff.right,
        "identical": diff.identical,
        "changed_fields": sorted(diff.changed_fields),
        "summary": diff.summary(),
    }


class PromptEvalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = ""
    golden_only: bool = True
    min_score: float | None = None


@market_router.post("/prompts/evals")
def run_prompt_eval(req: PromptEvalRequest) -> dict[str, Any]:
    """Run the offline (golden) eval suite for one prompt version.

    Uses a stub model that returns an empty answer: golden cases pin their own
    answer, so this proves the *contract* without needing a gateway. The response
    says so explicitly via ``golden_only``/``model``.
    """
    registry = default_registry()
    try:
        spec = (
            registry.require(req.name, req.version) if req.version
            else registry.require(req.name)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    report = run_prompt_evals(
        spec, lambda messages, params: "", golden_only=req.golden_only,
        library_snapshot=registry.snapshot(),
    )
    payload = report.to_dict()
    threshold = req.min_score if req.min_score is not None else settings.PROMPT_EVAL_MIN_SCORE
    try:
        report.regression_gate(min_score=threshold)
        payload["gate"] = {"passed": True, "min_score": threshold}
    except PromptEvalError as exc:
        payload["gate"] = {"passed": False, "min_score": threshold, "error": str(exc)}
    return payload


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
@market_router.get("/mcp/servers")
def mcp_servers() -> dict[str, Any]:
    """Which servers are configured, and which actually connected."""
    service = MCPDispatchService()
    try:
        errors = service.connect()
        return {
            "configured": service.server_names,
            "known": list(SERVER_NAMES),
            "connected": sorted(service.clients),
            "connect_errors": errors,
            "transport": service.transport_mode,
            "allowlist": sorted(service.allowlist),
            "max_tool_calls": service.max_calls,
            "tools": [
                {
                    "name": t.qualified_name,
                    "description": t.description,
                    "read_only": t.read_only,
                    "input_schema": t.input_schema,
                }
                for t in service.available_tools()
            ],
        }
    finally:
        service.close()


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


@market_router.post("/mcp/call")
def mcp_call(req: ToolCallRequest) -> dict[str, Any]:
    """Call one MCP tool through the same policy the agent uses (allowlist+budget)."""
    from .agent.mcp_dispatch import ToolCall

    service = MCPDispatchService()
    try:
        errors = service.connect()
        if req.server not in service.clients:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "server_unavailable",
                    "server": req.server,
                    "connect_error": errors.get(req.server, "not configured"),
                },
            )
        if not service.is_allowed(req.server, req.tool):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "tool_not_allowed",
                    "tool": f"{req.server}.{req.tool}",
                    "hint": "tool is not advertised or not on MCP_TOOL_ALLOWLIST",
                },
            )
        report = service.execute(
            [ToolCall(server=req.server, tool=req.tool, arguments=req.arguments,
                      reason="api")]
        )
        return report.to_dict()
    finally:
        service.close()


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)


@market_router.post("/mcp/dispatch")
def mcp_dispatch(req: DispatchRequest) -> dict[str, Any]:
    """Agent-style run: plan tool calls from a question, then execute them."""
    service = MCPDispatchService()
    try:
        return service.run(req.question, arguments=req.arguments)
    finally:
        service.close()


# --------------------------------------------------------------------------- #
# Data pipeline
# --------------------------------------------------------------------------- #
class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=400)
    kind: str = Field(default="reviews", pattern="^(reviews|metrics)$")
    game_id: str = Field(default="", max_length=64)
    source: str = Field(default="", max_length=64)


@market_router.post("/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    """Ingest a fixture file from ``MARKET_DATA_DIR`` (no arbitrary URLs)."""
    from pathlib import Path

    from .mcp.servers.market_insight_server import _resolve_fixture

    warehouse = _warehouse()
    try:
        path = _resolve_fixture(req.path, Path(settings.MARKET_DATA_DIR))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    connector = FileConnector(path, source=req.source or path.stem)
    if req.kind == "reviews":
        result = ingest_reviews(connector, warehouse, game_id=req.game_id)
    else:
        result = ingest_metrics(connector, warehouse, game_id=req.game_id)
    return result.to_dict()


@market_router.get("/warehouse")
def warehouse_summary() -> dict[str, Any]:
    """Row counts + the schema an analyst is allowed to query."""
    from .mcp.servers.market_insight_server import live_schema

    warehouse = _warehouse()
    if not warehouse.path.exists():
        return {
            "initialised": False,
            "database": str(warehouse.path),
            "hint": "POST /api/v1/market/ingest to load the bundled fixtures",
        }
    return {
        "initialised": True,
        "database": str(warehouse.path),
        "row_counts": warehouse.row_counts(),
        "schema": {t: sorted(cols) for t, cols in live_schema(warehouse).items()},
    }


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=500)
    execute: bool = True


@market_router.post("/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    """Ask a business question: plan SQL (reviewed rules) and, if supported, run it."""
    from .mcp.servers.market_insight_server import live_schema

    warehouse = _warehouse()
    if not warehouse.path.exists():
        raise HTTPException(status_code=409, detail="warehouse not initialised")
    plan = plan_sql(req.question, schema=live_schema(warehouse))
    payload = plan.to_dict()
    payload["available_rules"] = rule_rules()
    if plan.supported and req.execute:
        payload["rows"] = warehouse.query(plan.sql, plan.params, max_rows=200)
    return payload


# --------------------------------------------------------------------------- #
# Showcase scenarios
# --------------------------------------------------------------------------- #
@market_router.get("/scenarios")
def list_scenarios() -> dict[str, Any]:
    from .scenarios import describe_scenarios

    return {"scenarios": describe_scenarios()}


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


@market_router.post("/scenarios/run")
def run_scenario(req: ScenarioRequest) -> dict[str, Any]:
    """Run one end-to-end scenario (the interview demo entry point)."""
    from .scenarios import run_scenario as _run

    try:
        return _run(req.name, req.params)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


