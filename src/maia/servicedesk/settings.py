"""Service Desk settings — standalone, SD_-prefixed, no legacy fallbacks.

T0 contract (plan-20260919-1820, section T0):
- ``mode`` gates offline demo vs integrated live behaviour.
- ``database_url`` is required and must be PostgreSQL for integrated mode.
- Integrated mode must never silently fall back to mock/hash embedding or
  mock LLM: construction raises ``ValidationError`` (a ``ValueError``
  subclass) instead of degrading.
- Missing external credentials in integrated mode are surfaced through
  ``readiness_errors()`` (fail readiness, no fallback) — see T11.
- All secrets are referenced (env var / secret store name), never inlined.

Env vars map ``SD_<FIELD>`` (case-insensitive), e.g. ``SD_MODE``.
"""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_POSTGRES_SCHEMES = ("postgresql://", "postgresql+psycopg://")


class Settings(BaseSettings):
    """Flat, SD_-prefixed settings for the Service Desk deployment."""

    model_config = SettingsConfigDict(
        env_prefix="SD_",
        env_file=".env.servicedesk",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime mode gates (Global Constraints: demo offline vs integrated are
    # labelled and gated separately; no silent mock switch).
    mode: Literal["offline", "integrated"] = "offline"
    embedding_mode: Literal["cloudflare", "hash"] = "cloudflare"
    llm_mode: Literal["cloudflare", "mock"] = "cloudflare"

    # Persistence (S4: PostgreSQL is the source of truth for approval, job,
    # membership, ACL and audit; no /tmp storage anywhere).
    database_url: str = ""
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "servicedesk_knowledge"

    # Jira integration (S7: explicit auth mode; secrets by reference only).
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token_ref: str = ""
    jira_project_key: str = ""
    jira_webhook_secret_ref: str = ""

    # Worker / delivery tuning (S4, S7 defaults: lease 60s + heartbeat 15s,
    # connect 3s / read 15s, initial worker concurrency 2).
    worker_concurrency: int = 2
    http_connect_timeout_sec: float = 3.0
    http_read_timeout_sec: float = 15.0
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    read_retry_max_attempts: int = 5

    @model_validator(mode="after")
    def _enforce_mode_gates(self) -> "Settings":
        if self.mode == "integrated":
            if self.embedding_mode == "hash":
                raise ValueError(
                    "integrated mode disallows mock/hash embeddings: "
                    "quality reported from a hash mock is forbidden "
                    "(Global Constraints, S6/S9)"
                )
            if self.llm_mode == "mock":
                raise ValueError(
                    "integrated mode disallows the mock LLM provider"
                )
            if not self.database_url.startswith(_POSTGRES_SCHEMES):
                raise ValueError(
                    "integrated mode requires a PostgreSQL database_url "
                    "(postgresql:// or postgresql+psycopg://), got: "
                    f"{self.database_url or '<empty>'!r}"
                )
        return self

    def readiness_errors(self) -> list[str]:
        """Credential/config gaps that block integrated readiness.

        Called at startup (T11). Returns human-readable problems; an empty
        list means the current configuration is complete for ``mode``.
        """
        problems: list[str] = []
        if not self.database_url.startswith(_POSTGRES_SCHEMES):
            problems.append("database_url must be PostgreSQL (all modes)")
        if self.mode == "integrated":
            if not self.jira_base_url:
                problems.append("jira_base_url is required in integrated mode")
            if not self.jira_email:
                problems.append("jira_email is required in integrated mode")
            if not self.jira_api_token_ref:
                problems.append(
                    "jira_api_token_ref is required in integrated mode "
                    "(secret reference, not the raw token)"
                )
            if not self.jira_project_key:
                problems.append("jira_project_key is required in integrated mode")
        return problems
