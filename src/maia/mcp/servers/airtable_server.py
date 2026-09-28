"""Airtable MCP server — Content Calendar writes/reads for marketing campaigns.

Dual-mode by design: with ``AIRTABLE_API_KEY`` + ``AIRTABLE_BASE_ID`` configured
it calls the real Airtable REST API; without them it writes to a local JSON store
and marks every result ``dry_run: true``. Both modes return the same fields, so a
workflow built on the local store keeps working when credentials arrive.

Idempotency is the load-bearing property: a retried agent turn (or a user pressing
send twice) must not create two rows. Every task carries an ``external_key``; a
repeat call with the same key returns the *existing* record with
``deduplicated: true`` instead of writing again.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...config import settings
from ..protocol import ToolResult
from ..server import MCPServer
from ._common import content_id, now_iso, read_json_file, write_json_file

__all__ = [
    "AirtableBackend",
    "LocalAirtableBackend",
    "RemoteAirtableBackend",
    "build_airtable_server",
]

DEFAULT_STORE = "./storage/airtable_tasks.json"
_TASK_FIELDS = ("campaign_id", "channel", "content", "due_date", "owner", "status")
MAX_CONTENT_CHARS = 4000


class AirtableBackend:
    """Interface: create/update/list tasks. Implementations must be idempotent."""

    dry_run = True
    name = "base"

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def list_tasks(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        raise NotImplementedError

    def update_status(self, external_key: str, status: str) -> dict[str, Any] | None:
        raise NotImplementedError


class LocalAirtableBackend(AirtableBackend):
    """Durable local JSON store used when Airtable credentials are absent."""

    dry_run = True
    name = "local-json"

    def __init__(self, path: str | Path = DEFAULT_STORE) -> None:
        self.path = Path(path)

    def _load(self) -> list[dict]:
        data = read_json_file(self.path, [])
        return data if isinstance(data, list) else []

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        tasks = self._load()
        for existing in tasks:
            if existing.get("external_key") == task["external_key"]:
                return {**existing, "deduplicated": True}
        record = {**task, "created_at": now_iso(), "storage": "local"}
        tasks.append(record)
        write_json_file(self.path, tasks)
        return {**record, "deduplicated": False}

    def list_tasks(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        tasks = self._load()
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        return tasks[-max(1, limit):]

    def update_status(self, external_key: str, status: str) -> dict[str, Any] | None:
        tasks = self._load()
        updated: dict[str, Any] | None = None
        for task in tasks:
            if task.get("external_key") == external_key:
                task["status"] = status
                task["updated_at"] = now_iso()
                updated = task
                break
        if updated is not None:
            write_json_file(self.path, tasks)
        return updated


class RemoteAirtableBackend(AirtableBackend):
    """Real Airtable REST backend (``POST/PATCH/GET /v0/{base}/{table}``).

    The HTTP callables are injectable so integration tests drive the request
    shape (URL, auth header, body) with a fake client instead of the network.
    """

    dry_run = False
    name = "airtable-rest"

    def __init__(
        self,
        *,
        api_key: str,
        base_id: str,
        table: str,
        timeout: int = 10,
        http_post: Callable[..., Any] | None = None,
        http_get: Callable[..., Any] | None = None,
        http_patch: Callable[..., Any] | None = None,
    ) -> None:
        import httpx

        self.api_key = api_key
        self.base_id = base_id
        self.table = table
        self.timeout = timeout
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._post = http_post or (
            lambda url, **kw: httpx.post(url, headers=headers, timeout=timeout, **kw)
        )
        self._get = http_get or (
            lambda url, **kw: httpx.get(url, headers=headers, timeout=timeout, **kw)
        )
        self._patch = http_patch or (
            lambda url, **kw: httpx.patch(url, headers=headers, timeout=timeout, **kw)
        )

    @property
    def _base_url(self) -> str:
        from urllib.parse import quote

        return f"https://api.airtable.com/v0/{self.base_id}/{quote(self.table)}"

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in task.items() if k in _TASK_FIELDS and v is not None}
        fields["external_key"] = task["external_key"]
        response = self._post(self._base_url, json={"fields": fields})
        response.raise_for_status()
        payload = response.json() or {}
        return {
            **task,
            "airtable_id": payload.get("id"),
            "created_at": payload.get("createdTime") or now_iso(),
            "storage": "airtable",
            "deduplicated": False,
        }

    def list_tasks(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        params: dict[str, Any] = {"maxRecords": max(1, limit)}
        if status:
            params["filterByFormula"] = f"{{status}}='{status}'"
        response = self._get(self._base_url, params=params)
        response.raise_for_status()
        records = (response.json() or {}).get("records") or []
        return [
            {"airtable_id": r.get("id"), **(r.get("fields") or {})} for r in records
        ]

    def update_status(self, external_key: str, status: str) -> dict[str, Any] | None:
        found = [
            t for t in self.list_tasks(limit=100) if t.get("external_key") == external_key
        ]
        if not found:
            return None
        record_id = found[0].get("airtable_id")
        response = self._patch(
            f"{self._base_url}/{record_id}", json={"fields": {"status": status}}
        )
        response.raise_for_status()
        payload = response.json() or {}
        return {
            "airtable_id": payload.get("id"),
            "status": status,
            "updated_at": payload.get("createdTime") or now_iso(),
        }


def _external_key(args: dict[str, Any]) -> str:
    """Caller-supplied key when present, else a content hash (still idempotent)."""
    return str(args.get("external_key") or content_id("task", json.dumps(args, sort_keys=True)))


def build_airtable_server(
    backend: AirtableBackend | None = None, *, store_path: str | Path | None = None
) -> MCPServer:
    """Build the ``airtable`` server, choosing the backend from settings.

    Backend selection is explicit and visible in every result (``storage`` field
    plus ``dry_run``), so nobody can mistake a local dry-run row for a real
    Airtable record during a demo or an incident review.
    """
    if backend is None:
        if settings.AIRTABLE_API_KEY and settings.AIRTABLE_BASE_ID:
            backend = RemoteAirtableBackend(
                api_key=settings.AIRTABLE_API_KEY,
                base_id=settings.AIRTABLE_BASE_ID,
                table=settings.AIRTABLE_TABLE,
                timeout=settings.AIRTABLE_TIMEOUT_SEC,
            )
        else:
            backend = LocalAirtableBackend(
                store_path or Path(settings.STORAGE_DIR) / "airtable_tasks.json"
            )

    server = MCPServer(
        "airtable",
        title="Airtable content calendar",
        instructions=(
            "Create/track marketing content tasks. Writes are idempotent per "
            "external_key: repeating a key returns the existing task."
        ),
    )

    @server.tool(
        "create_marketing_task",
        description="Create one content-calendar task for a campaign channel.",
        read_only=False,
        destructive=False,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["campaign_id", "channel", "content"],
            "additionalProperties": False,
            "properties": {
                "campaign_id": {"type": "string", "minLength": 2, "maxLength": 80},
                "channel": {
                    "type": "string",
                    "enum": ["facebook", "tiktok", "email", "zalo"],
                },
                "content": {
                    "type": "string",
                    "minLength": 5,
                    "maxLength": MAX_CONTENT_CHARS,
                },
                "due_date": {"type": "string", "maxLength": 32},
                "owner": {"type": "string", "maxLength": 80},
                "status": {
                    "type": "string",
                    "enum": ["draft", "ready", "published", "blocked"],
                    "default": "draft",
                },
                "external_key": {"type": "string", "maxLength": 80},
            },
        },
    )
    def create_marketing_task(args: dict[str, Any]) -> ToolResult:
        task = {k: v for k, v in args.items() if k in _TASK_FIELDS}
        task["external_key"] = _external_key(args)
        record = backend.create_task(task)
        return ToolResult.json(
            {**record, "dry_run": backend.dry_run, "storage": backend.name}
        )

    @server.tool(
        "list_content_calendar",
        description="List recent content-calendar tasks, optionally filtered by status.",
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["draft", "ready", "published", "blocked"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    )
    def list_content_calendar(args: dict[str, Any]) -> ToolResult:
        tasks = backend.list_tasks(
            status=args.get("status"), limit=int(args.get("limit", 20))
        )
        return ToolResult.json(
            {"tasks": tasks, "count": len(tasks), "dry_run": backend.dry_run}
        )

    @server.tool(
        "update_task_status",
        description="Move a content-calendar task to another status.",
        read_only=False,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["external_key", "status"],
            "additionalProperties": False,
            "properties": {
                "external_key": {"type": "string", "minLength": 2, "maxLength": 80},
                "status": {
                    "type": "string",
                    "enum": ["draft", "ready", "published", "blocked"],
                },
            },
        },
    )
    def update_task_status(args: dict[str, Any]) -> ToolResult:
        updated = backend.update_status(args["external_key"], args["status"])
        if updated is None:
            return ToolResult.text(
                f"no task with external_key {args['external_key']!r}",
                structured={"error": "task_not_found", "external_key": args["external_key"]},
                is_error=True,
            )
        return ToolResult.json(
            {**updated, "dry_run": backend.dry_run, "storage": backend.name}
        )

    return server


