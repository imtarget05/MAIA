"""MCP integration servers for MAIA's marketing / product workflows.

Four servers ship today, each a thin, testable adapter over a real system:

| Server | Backs onto | Tools |
|---|---|---|
| ``airtable`` | Airtable REST, or a local JSON store when no key | create task, list content calendar, update status |
| ``notification`` | SMTP + Teams webhook + Zalo OA, or a local outbox | email report, Teams card, Zalo OA message |
| ``sql_analytics`` | the market warehouse (read-only SQLite) | list tables, describe table, run guarded SELECT, KPI summary |
| ``market_insight`` | the ingestion/analytics pipeline | ingest reviews/metrics, review insight, campaign performance |

``build_server`` is the single entry point used by the bridge, the API and the
agent dispatch layer, so the name→server mapping cannot diverge between them.
"""
from __future__ import annotations

from ..server import MCPServer

__all__ = ["SERVER_NAMES", "build_all_servers", "build_server", "is_known_server"]

SERVER_NAMES: tuple[str, ...] = (
    "airtable",
    "notification",
    "sql_analytics",
    "market_insight",
)


def is_known_server(name: str) -> bool:
    return name in SERVER_NAMES


def build_server(name: str, **kwargs) -> MCPServer:
    """Construct a server by name. Raises ``KeyError`` for an unknown name."""
    if name == "airtable":
        from .airtable_server import build_airtable_server

        return build_airtable_server(**kwargs)
    if name == "notification":
        from .notification_server import build_notification_server

        return build_notification_server(**kwargs)
    if name == "sql_analytics":
        from .sql_analytics_server import build_sql_analytics_server

        return build_sql_analytics_server(**kwargs)
    if name == "market_insight":
        from .market_insight_server import build_market_insight_server

        return build_market_insight_server(**kwargs)
    raise KeyError(
        f"unknown MCP server {name!r}; known servers: {list(SERVER_NAMES)}"
    )


def build_all_servers(
    names: list[str] | tuple[str, ...] | None = None, **kwargs
) -> dict[str, MCPServer]:
    """Construct several servers at once (used by the dispatch service)."""
    selected = tuple(names) if names else SERVER_NAMES
    return {name: build_server(name, **kwargs) for name in selected}
