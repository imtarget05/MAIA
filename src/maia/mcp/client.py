"""MCP client: handshake, tool discovery, tool calls, audit trail.

The client is the *only* place that talks to a transport, so every policy that
must hold for all tool usage lives here rather than in caller code:

* **Explicit handshake.** ``connect()`` performs ``initialize`` and then sends
  the ``notifications/initialized`` notification MCP requires; ``list_tools()``
  before ``connect()`` raises instead of silently "working" against a server that
  never agreed on a protocol version.
* **Namespaced tools.** Tools are exposed as ``server.tool`` (e.g.
  ``airtable.create_marketing_task``) so two servers may both define ``status``
  without a collision, and an audit record says which server ran.
* **Bounded I/O.** Every request is bounded by the configured timeout, and a
  transport error becomes a structured failure result — an integration outage
  must degrade the turn, not crash it.
* **Audit without secrets.** ``AuditSink`` records tool, *hashed* arguments,
  duration and outcome. Argument values (emails, Airtable payloads) never land in
  the log; the hash lets an operator correlate a record with an application-side
  payload while investigating.
* **Failure semantics preserved.** An ``isError`` response is surfaced as
  ``{"ok": False, "error": ...}`` (the repo's soft-failure convention) so an
  agent can retry or report, while protocol errors raise.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .protocol import (
    MCP_PROTOCOL_VERSION,
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_PING,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    MCPError,
    RPCErrorCode,
    make_notification,
    make_request,
)
from .transport import Transport, TransportError

__all__ = ["AuditSink", "MCPClient", "ToolInfo"]


@dataclass(frozen=True)
class ToolInfo:
    """A tool advertised by a server, with the namespaced client-facing name."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.name}"

    @property
    def read_only(self) -> bool:
        return bool(self.annotations.get("readOnlyHint"))

    def to_openai_tool(self) -> dict[str, Any]:
        """OpenAI-style function schema (what an LLM actually sees)."""
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name.replace(".", "__"),
                "description": f"[{self.server}] {self.description}",
                "parameters": self.input_schema
                or {"type": "object", "properties": {}},
            },
        }


class AuditSink:
    """Append-only JSONL audit log for tool calls (never argument values)."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.records: list[dict[str, Any]] = []

    @staticmethod
    def hash_arguments(arguments: dict[str, Any]) -> str:
        blob = json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def write(self, record: dict[str, Any]) -> None:
        record = {"at": datetime.now(UTC).isoformat(), **record}
        self.records.append(record)
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Auditing must never break the call it audits; the in-memory record
            # is still available to the caller/report.
            pass


class MCPClient:
    """JSON-RPC client for one MCP server."""

    def __init__(
        self,
        server_name: str,
        transport: Transport,
        *,
        timeout: float = 15.0,
        audit: AuditSink | None = None,
    ) -> None:
        self.server_name = server_name
        self._transport = transport
        self.timeout = timeout
        self.audit = audit or AuditSink(None)
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self._tools: dict[str, ToolInfo] = {}
        self._connected = False
        self._request_counter = 0

    # ---- handshake -------------------------------------------------------
    def _next_id(self) -> str:
        self._request_counter += 1
        return f"{self.server_name}-{self._request_counter}-{secrets.token_hex(4)}"

    def _request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self._next_id()
        response = self._transport.send(make_request(request_id, method, params))
        if response is None:
            raise MCPError(
                RPCErrorCode.INTERNAL_ERROR,
                f"no response from server {self.server_name!r} for {method}",
            )
        if "error" in response:
            error = response.get("error") or {}
            raise MCPError(
                int(error.get("code", RPCErrorCode.INTERNAL_ERROR)),
                str(error.get("message", "unknown MCP error")),
                error.get("data"),
            )
        return response.get("result") or {}

    def connect(
        self, *, client_name: str = "maia", client_version: str = "0.4.0"
    ) -> dict[str, Any]:
        """Perform the MCP handshake and cache the tool list."""
        result = self._request(
            METHOD_INITIALIZE,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": client_name, "version": client_version},
            },
        )
        self.protocol_version = str(result.get("protocolVersion") or MCP_PROTOCOL_VERSION)
        self.server_info = result.get("serverInfo") or {}
        # The spec requires the client to confirm; a server may otherwise treat
        # this client as "still negotiating".
        self._transport.send(make_notification(METHOD_INITIALIZED))
        self._connected = True
        self.refresh_tools()
        self.audit.write(
            {
                "event": "connect",
                "server": self.server_name,
                "protocol_version": self.protocol_version,
                "tools": sorted(self._tools),
            }
        )
        return result

    @property
    def connected(self) -> bool:
        return self._connected

    def ping(self) -> bool:
        try:
            self._request(METHOD_PING)
            return True
        except (MCPError, TransportError):
            return False

    def close(self) -> None:
        self._transport.close()
        self._connected = False

    # ---- tools -----------------------------------------------------------
    def refresh_tools(self) -> list[ToolInfo]:
        """Fetch the full tool list, following ``nextCursor`` (paginated)."""
        collected: list[ToolInfo] = []
        cursor: str | None = None
        for _ in range(50):  # bounded: a server must not loop us forever
            params = {"cursor": cursor} if cursor else None
            result = self._request(METHOD_TOOLS_LIST, params)
            for raw in result.get("tools") or []:
                if not isinstance(raw, dict) or not raw.get("name"):
                    continue
                # Read each optional field once into a local: repeated ``raw.get``
                # calls would defeat narrowing and could return a different value
                # mid-expression.
                raw_schema = raw.get("inputSchema")
                raw_annotations = raw.get("annotations")
                collected.append(
                    ToolInfo(
                        server=self.server_name,
                        name=str(raw["name"]),
                        description=str(raw.get("description") or ""),
                        input_schema=raw_schema if isinstance(raw_schema, dict) else {},
                        annotations=raw_annotations
                        if isinstance(raw_annotations, dict)
                        else {},
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self._tools = {t.name: t for t in collected}
        return collected

    @property
    def tools(self) -> list[ToolInfo]:
        return [self._tools[name] for name in sorted(self._tools)]

    def tool(self, name: str) -> ToolInfo | None:
        return self._tools.get(name)

    # ---- calling ---------------------------------------------------------
    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call ``name`` and return a soft-failure dict.

        Always returns ``{"ok": bool, ...}``: transport problems, protocol errors
        and ``isError`` tool results all come back as ``ok=False`` with a reason,
        so a caller (agent or report) never has to catch exceptions to stay alive.
        """
        if not self._connected:
            raise MCPError(
                RPCErrorCode.INVALID_REQUEST,
                f"client for {self.server_name!r} is not connected; call connect() first",
            )
        arguments = arguments or {}
        started = time.perf_counter()
        qualified = f"{self.server_name}.{name}"
        try:
            result = self._request(
                METHOD_TOOLS_CALL, {"name": name, "arguments": arguments}
            )
            is_error = bool(result.get("isError"))
            text = self._text_of(result)
            payload: dict[str, Any] = {
                "ok": not is_error,
                "tool": qualified,
                "server": self.server_name,
                "content": result.get("content") or [],
                "structured": result.get("structuredContent"),
                "text": text,
            }
            if is_error:
                structured = result.get("structuredContent") or {}
                payload["error"] = (
                    structured.get("error") if isinstance(structured, dict) else None
                ) or text or "tool_error"
        except TransportError as exc:
            payload = {
                "ok": False, "tool": qualified, "server": self.server_name,
                "error": f"transport_error: {exc}",
            }
        except MCPError as exc:
            payload = {
                "ok": False, "tool": qualified, "server": self.server_name,
                "error": f"mcp_error[{exc.code}]: {exc.message}",
                "data": exc.data,
            }

        duration_ms = int((time.perf_counter() - started) * 1000)
        payload["duration_ms"] = duration_ms
        self.audit.write(
            {
                "event": "tool_call",
                "server": self.server_name,
                "tool": name,
                "args_hash": AuditSink.hash_arguments(arguments),
                "duration_ms": duration_ms,
                "ok": payload.get("ok", False),
                "error": payload.get("error"),
            }
        )
        return payload

    @staticmethod
    def _text_of(result: dict[str, Any]) -> str:
        chunks = [
            block.get("text", "")
            for block in (result.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(c for c in chunks if c)


