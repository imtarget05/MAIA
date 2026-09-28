"""MCP protocol tests: JSON-RPC framing, handshake, tool dispatch, pagination.

These are the tests that distinguish "MCP" from "REST calls with an MCP-shaped
name": everything here is a JSON-RPC message going through
:meth:`MCPServer.handle`, including the error semantics (protocol error vs.
``isError`` tool result) that a client has to distinguish.
"""
from __future__ import annotations

import pytest

from maia.json_schema_lite import validate
from maia.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    MCPError,
    McpToolError,
    RPCErrorCode,
    ToolResult,
    ToolSpec,
    make_notification,
    make_request,
)
from maia.mcp.server import MCPServer

ECHO_SCHEMA = {
    "type": "object",
    "required": ["text"],
    "additionalProperties": False,
    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 100}},
}


def _server() -> MCPServer:
    server = MCPServer("test-server", version="9.9.9", instructions="be nice")

    @server.tool("echo", description="Echo the text back.", input_schema=ECHO_SCHEMA,
                 read_only=True, idempotent=True)
    def echo(args):
        return ToolResult.json({"echoed": args["text"], "length": len(args["text"])})

    @server.tool("boom", description="Always fails.")
    def boom(args):
        raise McpToolError("integration refused the request")

    @server.tool("crash", description="Raises an unexpected exception.")
    def crash(args):
        raise ZeroDivisionError("unhandled")

    @server.tool("raw", description="Returns a plain dict.")
    def raw(args):
        return {"value": 42}

    return server


# --------------------------------------------------------------------------- #
# Handshake
# --------------------------------------------------------------------------- #
def test_initialize_negotiates_and_reports_server_info():
    result = _server().handle(
        make_request(1, "initialize",
                     {"protocolVersion": MCP_PROTOCOL_VERSION,
                      "clientInfo": {"name": "t", "version": "1"}})
    )["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "test-server"
    assert result["serverInfo"]["version"] == "9.9.9"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["negotiated"]["requested"] == MCP_PROTOCOL_VERSION


def test_initialize_falls_back_for_unknown_protocol_version():
    result = _server().handle(
        make_request(1, "initialize", {"protocolVersion": "1999-01-01"})
    )["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["negotiated"]["requested"] == "1999-01-01"


def test_initialized_notification_gets_no_response():
    server = _server()
    assert server.handle(make_notification("notifications/initialized")) is None
    assert server.handle(make_notification("notifications/initialized", {"a": 1})) is None


def test_ping_returns_empty_result():
    assert _server().handle(make_request(7, "ping"))["result"] == {}


# --------------------------------------------------------------------------- #
# JSON-RPC error semantics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message,code",
    [
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, RPCErrorCode.INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1}, RPCErrorCode.INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": "nope"}, RPCErrorCode.METHOD_NOT_FOUND),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
         RPCErrorCode.INVALID_PARAMS),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ""}},
         RPCErrorCode.INVALID_PARAMS),
    ],
)
def test_protocol_errors_use_reserved_codes(message, code):
    response = _server().handle(message)
    assert response["error"]["code"] == code
    assert "result" not in response


def test_non_dict_message_is_invalid_request():
    assert _server().handle(["not", "a", "message"])["error"]["code"] == (
        RPCErrorCode.INVALID_REQUEST
    )


def test_unknown_tool_error_lists_available_tools():
    response = _server().handle(
        make_request(1, "tools/call", {"name": "missing", "arguments": {}})
    )
    assert response["error"]["code"] == RPCErrorCode.INVALID_PARAMS
    assert "echo" in response["error"]["data"]["available"]


# --------------------------------------------------------------------------- #
# tools/list
# --------------------------------------------------------------------------- #
def test_tools_list_exposes_schema_and_annotations():
    tools = _server().handle(make_request(1, "tools/list"))["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"echo", "boom", "crash", "raw"}
    assert by_name["echo"]["inputSchema"] == ECHO_SCHEMA
    assert by_name["echo"]["annotations"]["readOnlyHint"] is True
    # Deterministic order so a client can diff two listings.
    assert [t["name"] for t in tools] == sorted(by_name)


def test_tools_list_paginates_with_a_stable_cursor():
    server = MCPServer("paged", page_size=2)
    for i in range(5):
        server.add_tool(ToolSpec(name=f"tool_{i}", description="d"), lambda args: None)

    first = server.handle(make_request(1, "tools/list"))["result"]
    assert [t["name"] for t in first["tools"]] == ["tool_0", "tool_1"]
    assert first["nextCursor"] == "tool_1"

    second = server.handle(
        make_request(2, "tools/list", {"cursor": first["nextCursor"]})
    )["result"]
    assert [t["name"] for t in second["tools"]] == ["tool_2", "tool_3"]

    third = server.handle(
        make_request(3, "tools/list", {"cursor": second["nextCursor"]})
    )["result"]
    assert [t["name"] for t in third["tools"]] == ["tool_4"]
    assert "nextCursor" not in third


def test_unknown_cursor_is_invalid_params():
    response = _server().handle(make_request(1, "tools/list", {"cursor": "ghost"}))
    assert response["error"]["code"] == RPCErrorCode.INVALID_PARAMS


def test_duplicate_tool_registration_is_rejected():
    server = MCPServer("dupes")
    server.add_tool(ToolSpec(name="x", description="d"), lambda args: None)
    with pytest.raises(ValueError, match="duplicate tool name"):
        server.add_tool(ToolSpec(name="x", description="d"), lambda args: None)


# --------------------------------------------------------------------------- #
# tools/call
# --------------------------------------------------------------------------- #
def test_tool_call_returns_content_blocks_and_structured_content():
    result = _server().handle(
        make_request(1, "tools/call", {"name": "echo", "arguments": {"text": "hi"}})
    )["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"echoed": "hi", "length": 2}
    assert result["content"][0]["type"] == "text"
    assert '"echoed": "hi"' in result["content"][0]["text"]


def test_tool_arguments_are_validated_before_the_handler_runs():
    called = []
    server = MCPServer("guard")

    @server.tool("strict", description="d", input_schema=ECHO_SCHEMA)
    def strict(args):
        called.append(args)
        return ToolResult.json({})

    for bad_args in ({}, {"text": ""}, {"text": "x" * 200}, {"text": 5},
                     {"text": "ok", "extra": 1}):
        response = server.handle(
            make_request(1, "tools/call", {"name": "strict", "arguments": bad_args})
        )
        assert response["error"]["code"] == RPCErrorCode.INVALID_PARAMS, bad_args
    assert called == []  # the handler never ran with invalid input


def test_tool_error_is_reported_as_is_error_not_a_protocol_error():
    result = _server().handle(
        make_request(1, "tools/call", {"name": "boom", "arguments": {}})
    )["result"]
    assert result["isError"] is True
    assert "integration refused" in result["content"][0]["text"]
    assert result["structuredContent"]["error"]


def test_unexpected_handler_exception_becomes_a_tool_error():
    result = _server().handle(
        make_request(1, "tools/call", {"name": "crash", "arguments": {}})
    )["result"]
    assert result["isError"] is True
    assert "ZeroDivisionError" in result["content"][0]["text"]


def test_handler_returning_plain_dict_is_wrapped_as_structured_content():
    result = _server().handle(
        make_request(1, "tools/call", {"name": "raw", "arguments": {}})
    )["result"]
    # A dict payload becomes structuredContent as-is; a non-dict is nested under
    # "result" so the field is always an object (what the spec requires).
    assert result["structuredContent"] == {"value": 42}


def test_server_uses_the_shared_json_schema_validator():
    """The tool schema contract is the same validator PromptOps uses."""
    errors = validate({"text": 42}, ECHO_SCHEMA)
    assert errors and "expected string" in errors[0]


def test_tool_spec_to_mcp_defaults_to_empty_object_schema():
    spec = ToolSpec(name="x", description="d")
    assert spec.to_mcp()["inputSchema"] == {"type": "object", "properties": {}}
    assert spec.to_mcp()["annotations"] == {
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False
    }


def test_message_builders_produce_valid_envelopes():
    assert make_request(1, "m") == {"jsonrpc": "2.0", "id": 1, "method": "m"}
    assert make_notification("m") == {"jsonrpc": "2.0", "method": "m"}
    assert str(MCPError(RPCErrorCode.INVALID_PARAMS, "x")) == "[-32602] x"

