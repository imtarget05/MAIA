"""Three end-to-end showcase scenarios (the interview demo).

Each scenario composes the whole stack — pipeline → analytics → MCP tool →
notification — and returns a structured run report (steps, artifacts, warnings)
instead of prose, so a demo is reproducible and reviewable:

1. ``review_insight_report`` — ingest a review fixture, extract sentiment/topic
   evidence, produce a product-ready brief. *(Product insight)*
2. ``campaign_content_sync`` — draft campaign copy, push each channel variant to
   the Airtable content calendar, report created vs deduplicated rows.
   *(Marketing automation)*
3. ``kpi_alert_fanout`` — detect a retention drop, then fan the alert out to
   Teams/Zalo/email. *(Live-ops alerting)*

When a credential is missing the scenario still completes and says
``dry_run: true`` — a demo must never imply a message was really sent.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["SCENARIOS", "describe_scenarios", "run_scenario"]

Scenario = Callable[[dict[str, Any]], dict[str, Any]]


def _warehouse():
    from .config import settings
    from .pipeline import MarketWarehouse

    return MarketWarehouse(settings.MARKET_DB_PATH)


def _service(**kwargs):
    from .agent.mcp_dispatch import MCPDispatchService

    return MCPDispatchService(**kwargs)


def _tool_call(server: str, tool: str, arguments: dict[str, Any], reason: str):
    from .agent.mcp_dispatch import ToolCall

    return ToolCall(server=server, tool=tool, arguments=arguments, reason=reason)


def _review_insight_report(params: dict[str, Any]) -> dict[str, Any]:
    """Ingest reviews (if a fixture is given) and produce an evidence-carrying brief."""
    from .pipeline import FileConnector, ingest_reviews
    from .pipeline.analytics import review_insight

    game_id = str(params.get("game_id") or "demo-game")
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []

    fixture = params.get("fixture")
    if fixture:
        from .config import settings
        from .mcp.servers.market_insight_server import _resolve_fixture

        try:
            # Relative fixtures resolve inside MARKET_DATA_DIR, so the default
            # example works out of the box and cannot reach outside it.
            path = _resolve_fixture(str(fixture), Path(settings.MARKET_DATA_DIR))
        except (ValueError, FileNotFoundError) as exc:
            warnings.append(f"fixture unusable: {exc}")
            path = None  # type: ignore[assignment]
        if path is not None:
            result = ingest_reviews(FileConnector(path), _warehouse(), game_id=game_id)
            steps.append({"step": "ingest", **result.to_dict()})
            if not result.ok:
                warnings.append(f"ingest reported a problem: {result.note}")
    else:
        warnings.append(
            "no 'fixture' provided — analysing the reviews already in the warehouse "
            "(pass fixture='samples/game_reviews.jsonl' for the demo)"
        )

    warehouse = _warehouse()
    if not warehouse.path.exists():
        return {"scenario": "review_insight_report", "ok": True, "steps": steps,
                "warnings": warnings + ["warehouse chưa có dữ liệu — hãy ingest fixture trước"],
                "insight": None}
    rows = warehouse.query(
        "SELECT rating, title, body, review_date FROM reviews WHERE game_id = ? "
        "ORDER BY review_date DESC",
        [game_id],
        max_rows=500,
    )
    steps.append({"step": "query", "rows": len(rows)})
    insight = review_insight(rows, product_name=game_id)
    insight["game_id"] = game_id
    insight["sample_size"] = len(rows)
    return {
        "scenario": "review_insight_report",
        "ok": bool(rows),
        "steps": steps,
        "warnings": warnings,
        "insight": insight,
        "method": insight.get("method"),
    }


def _campaign_content_sync(params: dict[str, Any]) -> dict[str, Any]:
    """Draft campaign copy and create one Airtable task per channel variant."""
    from .promptops import default_registry, render
    from .promptops.evals import extract_json

    registry = default_registry()
    spec = registry.require("campaign_copywriter", params.get("version") or "^1.1.0")
    rendered = render(
        spec,
        {
            "game_name": params.get("game_name", "Game Demo"),
            "update_name": params.get("update_name", "Bản cập nhật mùa hè"),
            "key_features": params.get(
                "key_features", "- Sự kiện đăng nhập 7 ngày\n- Bản đồ mới"
            ),
            "channels": params.get("channels", "facebook, zalo"),
            "tone": params.get("tone", "thân thiện"),
            "audience": params.get("audience", "người chơi mới"),
            "legal_notes": params.get("legal_notes", "Không quy đổi giải thưởng."),
            "language": params.get("language", "Tiếng Việt"),
        },
    )
    steps: list[dict[str, Any]] = [
        {"step": "render_prompt", "prompt": spec.ref,
         "content_hash": rendered.content_hash, "parameters": rendered.parameters}
    ]

    # Offline: reuse the prompt's own golden answer when no LLM is wired, so the
    # scenario demonstrates the *contract* without pretending a model ran.
    draft = params.get("draft_json")
    source = "provided"
    if not draft:
        from .promptops.evals import run_prompt_evals

        report = run_prompt_evals(
            spec, lambda m, p: "", golden_only=True,
            case_ids=["compliance_block_required"],
        )
        golden = next(
            (r for r in report.results if r.case_id == "compliance_block_required"), None
        )
        draft = golden.output if golden else ""
        source = "golden"
    payload = extract_json(draft) if isinstance(draft, str) else draft
    steps.append({"step": "draft_copy", "source": source, "parsed": bool(payload)})

    service = _service()
    try:
        connect_errors = service.connect()
        variants = (payload or {}).get("channel_variants", [])
        created: list[dict[str, Any]] = []
        for variant in variants:
            report = service.execute(
                [
                    _tool_call(
                        "airtable", "create_marketing_task",
                        {
                            "campaign_id": (payload or {}).get("campaign_id", "campaign"),
                            "channel": variant.get("channel", "facebook"),
                            "content": (
                                f"{variant.get('headline', '')} {variant.get('body', '')}"
                            )[:500],
                        },
                        "scenario:campaign_content_sync",
                    )
                ]
            )
            created.extend(report.results)
        steps.append({"step": "airtable_sync", "created": len(created),
                      "results": created, "connect_errors": connect_errors})
        return {
            "scenario": "campaign_content_sync",
            "ok": bool(created) and all(r.get("ok") for r in created),
            "steps": steps,
            "warnings": [] if created else ["no Airtable task was created"],
            "dry_run": any(
                (r.get("structured") or {}).get("dry_run") for r in created
            ),
            "variants": variants,
        }
    finally:
        service.close()


def _kpi_alert_fanout(params: dict[str, Any]) -> dict[str, Any]:
    """Detect a KPI drop, then fan the alert out over Teams / Zalo / email."""
    from .pipeline.analytics import detect_kpi_drop

    game_id = str(params.get("game_id") or "demo-game")
    warehouse = _warehouse()
    steps: list[dict[str, Any]] = []
    if not warehouse.path.exists():
        return {"scenario": "kpi_alert_fanout", "ok": True, "steps": steps,
                "warnings": ["warehouse chưa có dữ liệu — hãy ingest fixture trước"],
                "alerts": []}
    rows = warehouse.query(
        "SELECT metric_date, AVG(retention_d1) AS retention_d1, SUM(logins) AS logins, "
        "SUM(revenue_usd) AS revenue_usd FROM marketing_metrics "
        "WHERE game_id = ? AND retention_d1 IS NOT NULL "
        "GROUP BY metric_date ORDER BY metric_date",
        [game_id],
        max_rows=500,
    )
    steps.append({"step": "load_series", "rows": len(rows)})
    detection = detect_kpi_drop(rows, metric=params.get("metric", "retention_d1"))
    steps.append(
        {"step": "detect", **{k: v for k, v in detection.items() if k != "points"}}
    )
    if not detection.get("alert"):
        return {
            "scenario": "kpi_alert_fanout", "ok": True, "steps": steps,
            "warnings": ["no alert threshold met — nothing was sent"],
            "detection": detection, "alerts": [],
        }

    severity = detection.get("severity", "low")
    title = f"[{severity.upper()}] {game_id} retention D1 giảm"
    summary = (
        f"Retention D1 {detection.get('pct_change')}% "
        f"({detection.get('previous_value')} -> {detection.get('latest_value')}), "
        f"z={detection.get('zscore')}"
    )
    service = _service()
    try:
        service.connect()
        plan = [
            _tool_call("notification", "send_teams_card",
                       {"title": title, "summary": summary},
                       "scenario:kpi_alert_fanout"),
            _tool_call("notification", "send_email_report",
                       {"recipient": str(params.get("recipient", "lead@game.example")),
                        "subject": title, "body": summary},
                       "scenario:kpi_alert_fanout"),
        ]
        if params.get("zalo_user_ids"):
            plan.append(
                _tool_call(
                    "notification", "send_zalo_oa_message",
                    {
                        "user_ids": list(params["zalo_user_ids"]),
                        "template_id": str(params.get("zalo_template_id", "tpl")),
                        "template_data": {"title": title, "summary": summary},
                    },
                    "scenario:kpi_alert_fanout",
                )
            )
        report = service.execute(plan)
        steps.append({"step": "fanout", "executed": len(report.results),
                      "skipped": report.skipped, "results": report.results})
        return {
            "scenario": "kpi_alert_fanout",
            "ok": bool(report.results),
            "steps": steps,
            "warnings": [] if report.results else ["no notification was dispatched"],
            "dry_run": all(
                (r.get("structured") or {}).get("dry_run", True) for r in report.results
            ),
            "detection": detection,
            "alerts": report.results,
        }
    finally:
        service.close()


SCENARIOS: dict[str, dict[str, Any]] = {
    "review_insight_report": {
        "fn": _review_insight_report,
        "title": "Product insight từ review người chơi",
        "params": {"fixture": "samples/game_reviews.jsonl", "game_id": "demo-game"},
    },
    "campaign_content_sync": {
        "fn": _campaign_content_sync,
        "title": "Sinh nội dung chiến dịch và đẩy Airtable",
        "params": {"game_name": "Game Demo", "channels": "facebook, zalo"},
    },
    "kpi_alert_fanout": {
        "fn": _kpi_alert_fanout,
        "title": "Phát hiện tụt KPI và gửi cảnh báo đa kênh",
        "params": {"game_id": "demo-game", "recipient": "lead@game.example"},
    },
}


def describe_scenarios() -> list[dict[str, Any]]:
    """Machine-readable catalogue (used by the API and the docs)."""
    return [
        {"name": name, "title": meta["title"], "example_params": meta["params"]}
        for name, meta in SCENARIOS.items()
    ]


def run_scenario(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a scenario by name. An unknown name raises ``KeyError``."""
    meta = SCENARIOS.get(name)
    if meta is None:
        raise KeyError(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}")
    return meta["fn"]({**meta["params"], **(params or {})})


