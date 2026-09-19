"""Typed connector protocol and error taxonomy (S7).

Error mapping (plan S7, Jira/HTTP contract):
- 401/403            -> FAILED_CONFIGURATION (never hot-retried)
- 400/422            -> VALIDATION_FAILED
- 404                -> NOT_FOUND (for issue/comment reads)
- 429                -> RATE_LIMITED (carries Retry-After)
- 5xx, connect error -> RETRYABLE (reads/writes that definitely did not land)
- timeout after write-> handled at the delivery layer (T7 outcome_unknown)

Connectors never raise raw HTTP errors; callers branch on category only.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Protocol


class JiraErrorCategory(str, Enum):
    FAILED_CONFIGURATION = "failed_configuration"
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    RETRYABLE = "retryable"
    MALFORMED = "malformed"


class JiraError(Exception):
    def __init__(
        self,
        category: JiraErrorCategory,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status = status
        self.retry_after = retry_after


class JiraReads(Protocol):
    """Read surface used by projection/sync; writes live behind T6/T7."""

    def get_issue(self, key: str) -> dict[str, Any]: ...

    def list_issues(self, jql: str, next_page_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]: ...

    def list_comments(self, key: str) -> list[dict[str, Any]]: ...

    def get_metadata(self) -> dict[str, Any]: ...
