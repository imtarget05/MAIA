"""Model Context Protocol (MCP) wire types — JSON-RPC 2.0 framing.

Implements the parts of the specification this integration actually uses, so the
behaviour is auditable rather than "MCP-ish":

* ``initialize`` / ``notifications/initialized`` handshake with protocol-version
  negotiation (we advertise the versions we implement and echo the client's
  choice when it is one of them, otherwise we answer with our newest).
* ``tools/list`` with cursor pagination and ``tools/call`` returning a list of
  content blocks plus the spec's ``isError`` flag (a *tool failure* is a
  successful RPC response with ``isError: true`` — it must not be confused with
  a transport/protocol error).
* ``ping`` for liveness.
* JSON-RPC error codes for the failure modes that are protocol-level:
  parse error, invalid request, method not found, invalid params, internal error.

Anything the spec leaves open is written down here as an explicit decision so a
reviewer can check it instead of guessing from behaviour.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "JSONRPC_VERSION",
    "MCP_PROTOCOL_VERSION",
    "MCP_PROTOCOL_VERSIONS",
    "METHOD_INITIALIZE",
    "METHOD_INITIALIZED",
    "METHOD_PING",
    "METHOD_TOOLS_CALL",
    "METHOD_TOOLS_LIST",
    "MCPError",
    "McpToolError",
    "RPCErrorCode",
    "ToolResult",
    "ToolSpec",
    "error_response",
    "make_notification",
    "make_request",
    "make_response",
    "result_response",
]

JSONRPC_VERSION = "2.0"

METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_PING = "ping"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"

# Newest first. A client sending any of these is answered with its own version;
# an unknown version is answered with MCP_PROTOCOL_VERSION so the client can
# decide whether to continue (the spec's negotiation rule).
MCP_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
MCP_PROTOCOL_VERSION = MCP_PROTOCOL_VERSIONS[0]

# JSON-RPC 2.0 reserved codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_TOOL_EXECUTION_ERROR = -32000  # server-defined range for tool handler failures


class RPCErrorCode:
    """Namespace for the numeric codes (kept out of the module top-level)."""

    PARSE_ERROR = _PARSE_ERROR
    INVALID_REQUEST = _INVALID_REQUEST
    METHOD_NOT_FOUND = _METHOD_NOT_FOUND
    INVALID_PARAMS = _INVALID_PARAMS
    INTERNAL_ERROR = _INTERNAL_ERROR
    TOOL_EXECUTION_ERROR = _TOOL_EXECUTION_ERROR


class MCPError(Exception):
    """Protocol-level error (maps to a JSON-RPC ``error`` object)."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


class McpToolError(Exception):
    """A tool handler failed in a way the caller should see inside ``tools/call``.

    Distinct from :class:`MCPError`: this becomes ``isError: true`` content, not a
    JSON-RPC error, because the *protocol* worked — the tool did not. Callers can
    therefore reason about "the integration said no" separately from "the server
    is broken".
    """


@dataclass(frozen=True)
class ToolSpec:
    """A callable exposed over MCP."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    # Annotations carry the safety contract to the client (and to a human
    # reviewing the tool list): read-only tools must say so.
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema or {"type": "object", "properties": {}},
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
            },
        }


@dataclass(frozen=True)
class ToolResult:
    """Outcome of ``tools/call``: content blocks + structured payload."""

    content: list[dict[str, Any]] = field(default_factory=list)
    structured: dict[str, Any] | None = None
    is_error: bool = False

    def to_mcp(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": self.content, "isError": self.is_error}
        if self.structured is not None:
            # MCP calls this field ``structuredContent``; clients that do not
            # understand it still get the JSON text block below.
            payload["structuredContent"] = self.structured
        return payload

    @classmethod
    def text(cls, text: str, *, structured: dict[str, Any] | None = None,
             is_error: bool = False) -> ToolResult:
        return cls(
            content=[{"type": "text", "text": text}],
            structured=structured,
            is_error=is_error,
        )

    @classmethod
    def json(cls, data: Any, *, is_error: bool = False) -> ToolResult:
        return cls.text(
            json.dumps(data, ensure_ascii=False, sort_keys=True),
            structured=data if isinstance(data, dict) else {"result": data},
            is_error=is_error,
        )


# --------------------------------------------------------------------------- #
# Message constructors. Kept as functions (not a class hierarchy) because every
# message is a plain dict on the wire and the transports serialise them directly.
# --------------------------------------------------------------------------- #
def make_request(request_id: Any, method: str, params: dict | None = None) -> dict:
    message: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": method,
    }
    if params is not None:
        message["params"] = params
    return message


def make_notification(method: str, params: dict | None = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def make_response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str,
                   data: Any | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def result_response(request_id: Any, result: Any) -> dict:
    """Alias kept for readability at call sites that build success replies."""
    return make_response(request_id, result)

