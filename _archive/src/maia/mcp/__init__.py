"""MCP-style external connectors (read-only, token-gated, never raising)."""
from .client import (
    MCP_TOOL_REGISTRY,
    github_get_readme,
    github_search_repos,
    notion_search,
    status,
)
from .servers import ServerConfig, get_server, list_servers

__all__ = ["MCP_TOOL_REGISTRY", "ServerConfig", "get_server", "github_get_readme",
           "github_search_repos", "list_servers", "notion_search", "status"]
