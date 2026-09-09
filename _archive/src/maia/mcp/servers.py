"""External connector registry (MCP-style, REST-based, no new deps).

MAIA talks to outside tools (GitHub, Notion, plain HTTP) through small
read-only connectors over `requests` (already a dependency) instead of
spawning MCP stdio servers (npx): same "tool" shape, zero extra runtime.

Every connector is disabled without its token and never raises to callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings


@dataclass
class ServerConfig:
    name: str                    # "github" | "notion" | ...
    base_url: str
    token: str = ""
    timeout_sec: int = 10
    enabled: bool = False
    note: str = ""

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.base_url and self.token)

    def headers(self) -> dict:
        return {"Accept": "application/vnd.github+json"} if self.name == "github" \
            else {"Content-Type": "application/json"}


def _github_config() -> ServerConfig:
    return ServerConfig(
        name="github", base_url="https://api.github.com",
        token=(settings.MCP_GITHUB_TOKEN or "").strip(),
        timeout_sec=settings.MCP_TIMEOUT_SEC,
        enabled=settings.MCP_GITHUB_ENABLED,
        note="Read-only: search repos, read files. Needs a fine-grained PAT.")


def _notion_config() -> ServerConfig:
    return ServerConfig(
        name="notion", base_url="https://api.notion.com/v1",
        token=(settings.MCP_NOTION_TOKEN or "").strip(),
        timeout_sec=settings.MCP_TIMEOUT_SEC,
        enabled=settings.MCP_NOTION_ENABLED,
        note="Read-only: search pages/databases. Needs an integration token.")


def list_servers() -> list[ServerConfig]:
    """All known connectors (active only with token + flag)."""
    return [_github_config(), _notion_config()]


def get_server(name: str) -> ServerConfig | None:
    for s in list_servers():
        if s.name == name:
            return s
    return None
