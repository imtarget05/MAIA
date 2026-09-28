"""MCP server core: tool registration + JSON-RPC message handling.

A server is deliberately transport-agnostic — ``handle()`` takes a decoded
message and returns a response dict (or ``None`` for notifications). The same
object is therefore driven by an in-process transport in unit tests, by a
subprocess stdio bridge in integration tests, and by the stdio bridge binary in
a real MCP client (Claude Desktop, Cursor, an agent runtime).

Behaviour decisions worth knowing:

* **Argument validation is enforced before the handler runs** (shared
  ``json_schema_lite``), because the caller is usually an LLM. A hallucinated
  argument name must never reach a side-effecting integration.
* **Handler exceptions become ``isError: true`` content**, never a crash and
  never a JSON-RPC error: the protocol succeeded, the tool failed. Unexpected
  exceptions are caught too — a tool bug must not take down the server loop.
* **``tools/list`` paginates** by a stable sort with a cursor, so the tool list
  is reproducible between calls (some clients diff it).
* **Notifications get no response** (JSON-RPC 2.0 rule); the loop must not write
  for them, otherwise a client's parser desynchronises.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..json_schema_lite import describe_errors, validate
from .protocol import (
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSIONS,
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_PING,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    MCPError,
    McpToolError,
    RPCErrorCode,
    ToolResult,
    ToolSpec,
    error_response,
    make_response,
)

__all__ = ["MCPHandler", "MCPServer", "ServerInfo"]

MCPHandler = Callable[[dict[str, Any]], ToolResult]

DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True)
class ServerInfo:
    name: str
    version: str = "1.0.0"
    title: str = ""
    instructions: str = ""

    def to_mcp(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.title:
            payload["title"] = self.title
        if self.instructions:
            payload["instructions"] = self.instructions
        return payload


class MCPServer:
    """A named set of tools exposed over MCP."""

    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        title: str = "",
        instructions: str = "",
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.info = ServerInfo(name=name, version=version, title=title, instructions=instructions)
        self.page_size = max(1, page_size)
        self._tools: dict[str, tuple[ToolSpec, MCPHandler]] = {}
        self._initialized = False
        self._client_protocol: str | None = None

    # ---- registration ----------------------------------------------------
    def tool(
        self,
        name: str,
        *,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        read_only: bool = False,
        destructive: bool = False,
        idempotent: bool = False,
    ) -> Callable[[MCPHandler], MCPHandler]:
        """Decorator form: ``@server.tool("x", ...)  def x(args): ...``"""

        def _register(handler: MCPHandler) -> MCPHandler:
            self.add_tool(
                ToolSpec(
                    name=name,
                    description=description or (handler.__doc__ or "").strip(),
                    input_schema=input_schema or {},
                    read_only=read_only,
                    destructive=destructive,
                    idempotent=idempotent,
                ),
                handler,
            )
            return handler

        return _register

    def add_tool(self, spec: ToolSpec, handler: MCPHandler) -> None:
        if not spec.name or not spec.name.strip():
            raise ValueError("tool name cannot be empty")
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name {spec.name!r} in server {self.info.name!r}")
        self._tools[spec.name] = (spec, handler)

    # ---- introspection ---------------------------------------------------
    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def tool_spec(self, name: str) -> ToolSpec | None:
        entry = self._tools.get(name)
        return entry[0] if entry else None

    def list_tools(self, cursor: str | None = None) -> dict[str, Any]:
        """Stable, paginated ``tools/list`` result."""
        names = self.tool_names
        start = 0
        if cursor:
            if cursor not in names:
                raise MCPError(
                    RPCErrorCode.INVALID_PARAMS, f"unknown cursor {cursor!r}"
                )
            start = names.index(cursor) + 1
        page = names[start : start + self.page_size]
        next_cursor = None
        if start + self.page_size < len(names):
            next_cursor = page[-1] if page else None
        result: dict[str, Any] = {
            "tools": [self._tools[n][0].to_mcp() for n in page]
        }
        if next_cursor:
            result["nextCursor"] = next_cursor
        return result

    # ---- protocol --------------------------------------------------------
    def handle(self, message: Any) -> dict[str, Any] | None:
        """Dispatch one decoded JSON-RPC message.

        Returns the response dict, or ``None`` when the message is a
        notification (JSON-RPC forbids responding to notifications).
        """
        if not isinstance(message, dict):
            return error_response(
                None, RPCErrorCode.INVALID_REQUEST, "request must be a JSON object"
            )

        has_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if message.get("jsonrpc") != JSONRPC_VERSION:
            return error_response(
                request_id,
                RPCErrorCode.INVALID_REQUEST,
                "jsonrpc must be exactly '2.0'",
            )
        if not isinstance(method, str) or not method:
            return error_response(
                request_id, RPCErrorCode.INVALID_REQUEST, "method is required"
            )
        if not isinstance(params, dict):
            return error_response(
                request_id, RPCErrorCode.INVALID_PARAMS, "params must be an object"
            )

        try:
            result = self._dispatch(method, params, has_id)
        except MCPError as exc:
            if not has_id:
                return None
            return error_response(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # never let a tool bug kill the loop
            if not has_id:
                return None
            return error_response(
                request_id,
                RPCErrorCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
            )

        if result is None:  # notification
            return None
        if not has_id:
            return None
        return make_response(request_id, result)

    def _dispatch(self, method: str, params: dict, has_id: bool) -> Any:
        if method == METHOD_INITIALIZE:
            return self._initialize(params)
        if method == METHOD_INITIALIZED:
            self._initialized = True
            return None
        if method == METHOD_PING:
            return {}
        if method == METHOD_TOOLS_LIST:
            return self.list_tools(params.get("cursor"))
        if method == METHOD_TOOLS_CALL:
            return self._call_tool(params)
        raise MCPError(RPCErrorCode.METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _initialize(self, params: dict) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        chosen = requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        self._client_protocol = chosen
        server_info = params.get("clientInfo")
        return {
            "protocolVersion": chosen,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self.info.to_mcp(),
            # Echoed so a client can log which version it actually negotiated,
            # including the case where it asked for something we do not support.
            "negotiated": {
                "requested": requested,
                "supported": list(MCP_PROTOCOL_VERSIONS),
                "client_info": server_info if isinstance(server_info, dict) else None,
            },
        }

    def _call_tool(self, params: dict) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise MCPError(RPCErrorCode.INVALID_PARAMS, "'name' is required")
        entry = self._tools.get(name)
        if entry is None:
            raise MCPError(
                RPCErrorCode.INVALID_PARAMS,
                f"unknown tool {name!r}",
                {"available": self.tool_names},
            )
        spec, handler = entry
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise MCPError(
                RPCErrorCode.INVALID_PARAMS, "'arguments' must be an object"
            )

        errors = validate(arguments, spec.input_schema)
        if errors:
            raise MCPError(
                RPCErrorCode.INVALID_PARAMS,
                f"invalid arguments for {name}: {describe_errors(errors)}",
                {"schema_errors": errors},
            )

        try:
            result = handler(arguments)
        except McpToolError as exc:
            return ToolResult.text(
                str(exc), structured={"error": str(exc)}, is_error=True
            ).to_mcp()
        except Exception as exc:  # a handler bug is a tool error, not a crash
            return ToolResult.text(
                f"tool {name} failed: {type(exc).__name__}: {exc}",
                structured={"error": f"{type(exc).__name__}: {exc}"},
                is_error=True,
            ).to_mcp()

        if isinstance(result, ToolResult):
            return result.to_mcp()
        if isinstance(result, dict) and "content" in result:
            return result  # handler already produced a wire-level payload
        return ToolResult.json(result if result is not None else {}).to_mcp()

