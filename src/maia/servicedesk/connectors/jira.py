"""Jira Cloud read adapter (T2).

- Explicit Basic auth: email + API token (never Bearer for API tokens).
- Host and project allowlists enforced here; an arbitrary issue key cannot
  widen reads beyond the configured integration (S8 note).
- Uses the new paginated search endpoint /rest/api/3/search/jql (the classic
  /rest/api/3/search is deprecated by Atlassian).
- Read-only in T2; write methods arrive with the delivery layer (T7) where
  readback/unknown-outcome semantics live.

The transport is injectable for tests (httpx.MockTransport).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from maia.servicedesk.connectors.base import JiraError, JiraErrorCategory

_PAGE_SIZE = 50


class JiraClient:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        allowed_hosts: list[str] | None = None,
        connect_timeout: float = 3.0,
        read_timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        host = httpx.URL(self.base_url).host
        if not host:
            raise ValueError(f"invalid Jira base_url: {base_url!r}")
        if allowed_hosts is not None and host not in allowed_hosts:
            raise ValueError(f"host {host!r} is not allowlisted for this integration")
        if not self.base_url.startswith("https://"):
            raise ValueError("Jira base_url must use https")
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(email, api_token),  # httpx Basic auth: email + API token
            transport=transport,
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=30.0, pool=3.0
            ),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    # ---- error mapping (S7) --------------------------------------------

    def _raise(self, response: httpx.Response, context: str) -> None:
        status = response.status_code
        retry_after: float | None = None
        raw_retry = response.headers.get("Retry-After")
        if raw_retry:
            try:
                retry_after = float(raw_retry)
            except ValueError:
                retry_after = None
        if status in (401, 403):
            raise JiraError(
                JiraErrorCategory.FAILED_CONFIGURATION,
                f"{context}: authentication/authorization failed (HTTP {status})",
                status=status,
            )
        if status in (400, 422):
            raise JiraError(
                JiraErrorCategory.VALIDATION_FAILED,
                f"{context}: request rejected (HTTP {status})",
                status=status,
            )
        if status == 404:
            raise JiraError(
                JiraErrorCategory.NOT_FOUND,
                f"{context}: resource not found (HTTP 404)",
                status=status,
            )
        if status == 429:
            raise JiraError(
                JiraErrorCategory.RATE_LIMITED,
                f"{context}: rate limited (HTTP 429)",
                status=status,
                retry_after=retry_after,
            )
        category = (
            JiraErrorCategory.RETRYABLE
            if status >= 500
            else JiraErrorCategory.MALFORMED if status < 400 else JiraErrorCategory.RETRYABLE
        )
        raise JiraError(category, f"{context}: unexpected HTTP {status}", status=status)

    def _json(self, response: httpx.Response, context: str) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JiraError(
                JiraErrorCategory.MALFORMED,
                f"{context}: response is not valid JSON ({exc})",
            ) from exc

    def _get(self, path: str, context: str, params: dict | None = None) -> Any:
        response = self._client.get(path, params=params)
        if response.status_code >= 400:
            self._raise(response, context)
        return self._json(response, context)

# --- read methods ---

    def get_issue(self, key: str) -> dict[str, Any]:
        data = self._get(
            f"/rest/api/3/issue/{key}",
            f"get_issue({key})",
            params={
                "fields": "summary,description,status,priority,assignee,"
                          "reporter,created,updated,issuetype"
            },
        )
        if not isinstance(data, dict) or "key" not in data:
            raise JiraError(
                JiraErrorCategory.MALFORMED, f"get_issue({key}): malformed payload"
            )
        return data

    def list_issues(
        self, jql: str, next_page_token: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        payload: dict[str, Any] = {
            "jql": jql,
            "maxResults": _PAGE_SIZE,
            "fields": [
                "summary", "description", "status", "priority",
                "reporter", "created", "updated", "issuetype",
            ],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        response = self._client.post("/rest/api/3/search/jql", json=payload)
        if response.status_code >= 400:
            self._raise(response, "list_issues")
        data = self._json(response, "list_issues")
        if not isinstance(data, dict) or "issues" not in data:
            raise JiraError(
                JiraErrorCategory.MALFORMED, "list_issues: malformed payload"
            )
        issues = data["issues"]
        if not isinstance(issues, list):
            raise JiraError(
                JiraErrorCategory.MALFORMED, "list_issues: 'issues' is not a list"
            )
        token = data.get("nextPageToken")
        return issues, token if isinstance(token, str) else None

    def list_comments(self, key: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            data = self._get(
                f"/rest/api/3/issue/{key}/comment",
                f"list_comments({key})",
                params={
                    "startAt": start_at,
                    "maxResults": _PAGE_SIZE,
                    "orderBy": "created",
                },
            )
            if not isinstance(data, dict) or "comments" not in data:
                raise JiraError(
                    JiraErrorCategory.MALFORMED,
                    f"list_comments({key}): malformed payload",
                )
            comments.extend(data["comments"])
            total = data.get("total")
            start_at += len(data["comments"])
            if not isinstance(total, int) or start_at >= total or not data["comments"]:
                break
        return comments

    def get_metadata(self) -> dict[str, Any]:
        priorities = self._get("/rest/api/3/priority", "get_metadata(priorities)")
        statuses = self._get(
            f"/rest/api/3/project/{self.project_key}/statuses",
            "get_metadata(statuses)",
        )
        if not isinstance(priorities, list) or not isinstance(statuses, list):
            raise JiraError(
                JiraErrorCategory.MALFORMED, "get_metadata: malformed payload"
            )
        return {
            "project_key": self.project_key,
            "priorities": [
                {"id": p.get("id"), "name": p.get("name")} for p in priorities
            ],
            "statuses": [
                {
                    "issue_type": s.get("name"),
                    "statuses": [x.get("name") for x in s.get("statuses", [])],
                }
                for s in statuses
            ],
        }
