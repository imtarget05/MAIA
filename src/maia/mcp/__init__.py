"""Model Context Protocol (MCP) implementation for MAIA.

Protocol-level, not "MCP-flavoured": JSON-RPC 2.0 framing, the
``initialize``/``notifications/initialized`` handshake with version negotiation,
``tools/list`` with cursor pagination, and ``tools/call`` with content blocks and
the spec's ``isError`` distinction. See :mod:`maia.mcp.protocol` for the wire
types and :mod:`maia.mcp.server` for dispatch.

Layers, each usable on its own:

* :class:`MCPServer` — transport-agnostic tool host.
* :class:`MCPClient` / :class:`InProcessTransport` / :class:`StdioTransport` —
  in-process (tests, API) and subprocess (real MCP clients) transports.
* :mod:`maia.mcp.servers` — the integration servers (Airtable, notifications,
  read-only SQL analytics, market insight).
* :mod:`maia.mcp.bridge` — ``python -m maia.mcp.bridge --server <name>`` so an
  external MCP client (Claude Desktop, Cursor, an agent runtime) can connect.

The older ``_archive/src/maia/mcp`` held REST connectors named "MCP-style" without
any protocol; it stays archived because those connectors are read-only GitHub/
Notion accessors, not MCP. See ``docs/adr/`` for the decision record.
"""
from __future__ import annotations

from .client import AuditSink, MCPClient, ToolInfo
from .protocol import (
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSIONS,
    MCPError,
    McpToolError,
    RPCErrorCode,
    ToolResult,
    ToolSpec,
)
from .server import MCPServer, ServerInfo
from .transport import (
    InProcessTransport,
    StdioTransport,
    Transport,
    TransportError,
    make_transport,
)

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCP_PROTOCOL_VERSIONS",
    "AuditSink",
    "InProcessTransport",
    "MCPClient",
    "MCPError",
    "MCPServer",
    "McpToolError",
    "RPCErrorCode",
    "ServerInfo",
    "StdioTransport",
    "ToolInfo",
    "ToolResult",
    "ToolSpec",
    "Transport",
    "TransportError",
    "make_transport",
]
