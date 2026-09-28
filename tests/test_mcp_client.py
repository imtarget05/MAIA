"""MCP client + transport tests, including a **real subprocess** round trip.

The subprocess test is the one that matters for credibility: it spawns
``python -m maia.mcp.bridge`` and speaks newline-delimited JSON-RPC over pipes,
so the framing, id-correlation and shutdown behaviour are exercised for real
rather than mocked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from maia.mcp.client import AuditSink, MCPClient
from maia.mcp.protocol import MCP_PROTOCOL_VERSION, MCPError
from maia.mcp.servers import SERVER_NAMES, build_server
from maia.mcp.transport import (
    InProcessTransport,
    StdioTransport,
    TransportError,
    make_transport,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _client(name: str = "airtable", **kwargs) -> MCPClient:
    return MCPClient(name, InProcessTransport(build_server(name)), **kwargs)


# --------------------------------------------------------------------------- #
# Handshake / discovery
# --------------------------------------------------------------------------- #
def test_connect_performs_handshake_and_caches_tools():
    client = _client()
    client.connect()
    assert client.connected is True
    assert client.protocol_version == MCP_PROTOCOL_VERSION
    assert client.server_info["name"] == "airtable"
    assert [t.name for t in client.tools] == [
        "create_marketing_task", "list_content_calendar", "update_task_status"
    ]
    assert client.tools[0].qualified_name == "airtable.create_marketing_task"


def test_connect_sends_the_initialized_notification():
    transport = InProcessTransport(build_server("airtable"))
    client = MCPClient("airtable", transport)
    client.connect()
    methods = [m.get("method") for m in transport.calls]
    assert methods[0] == "initialize"
    assert "notifications/initialized" in methods


def test_call_before_connect_is_a_protocol_error():
    with pytest.raises(MCPError, match="not connected"):
        _client().call_tool("list_content_calendar", {})


def test_every_known_server_builds_and_advertises_at_least_one_tool():
    for name in SERVER_NAMES:
        client = MCPClient(name, InProcessTransport(build_server(name)))
        client.connect()
        assert client.tools, f"{name} advertises no tools"
        assert all(t.input_schema for t in client.tools)


def test_unknown_server_name_raises_with_the_known_list():
    with pytest.raises(KeyError, match="known servers"):
        build_server("nope")


def test_tool_schemas_are_llm_ready():
    client = _client()
    client.connect()
    tool = client.tool("create_marketing_task").to_openai_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "airtable__create_marketing_task"
    assert tool["function"]["parameters"]["required"] == [
        "campaign_id", "channel", "content"
    ]


# --------------------------------------------------------------------------- #
# Calls, failures, audit
# --------------------------------------------------------------------------- #
def test_tool_call_returns_structured_content_and_duration():
    client = _client()
    client.connect()
    result = client.call_tool(
        "create_marketing_task",
        {"campaign_id": "mua-x", "channel": "facebook",
         "content": "Ban do moi da mo, moi chi huy tham gia ngay."},
    )
    assert result["ok"] is True
    assert result["tool"] == "airtable.create_marketing_task"
    assert result["structured"]["dry_run"] is True  # no Airtable key configured
    assert result["duration_ms"] >= 0


def test_invalid_arguments_surface_as_a_soft_failure():
    client = _client()
    client.connect()
    result = client.call_tool("create_marketing_task", {"channel": "facebook"})
    assert result["ok"] is False
    assert "invalid arguments" in result["error"]


def test_audit_records_hash_arguments_and_never_their_values(tmp_path):
    audit = AuditSink(tmp_path / "audit.jsonl")
    client = _client(audit=audit)
    client.connect()
    secret = "recipient-lead@game.example"
    client.call_tool("create_marketing_task",
                     {"campaign_id": "c", "channel": "zalo", "content": secret})

    assert [r["event"] for r in audit.records] == ["connect", "tool_call"]
    call = audit.records[-1]
    assert call["tool"] == "create_marketing_task"
    assert len(call["args_hash"]) == 16
    assert secret not in json.dumps(audit.records)

    on_disk = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "tool_call" in on_disk
    assert secret not in on_disk


def test_transport_failure_becomes_a_soft_failure():
    client = _client()
    client.connect()

    def _boom(message):
        raise TransportError("child died")

    client._transport.send = _boom  # simulate a dead subprocess
    result = client.call_tool("list_content_calendar", {})
    assert result["ok"] is False
    assert "transport_error" in result["error"]


def test_ping_reports_health():
    client = _client()
    client.connect()
    assert client.ping() is True
    client.close()
    assert client.connected is False


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
def test_inprocess_transport_refuses_use_after_close():
    transport = InProcessTransport(build_server("airtable"))
    transport.close()
    with pytest.raises(TransportError, match="closed"):
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_make_transport_rejects_unknown_mode():
    with pytest.raises(MCPError, match="unknown MCP transport mode"):
        make_transport("airtable", mode="carrier-pigeon")


def test_make_transport_builds_inprocess_by_default():
    assert isinstance(make_transport("airtable", mode="inprocess"), InProcessTransport)


# --------------------------------------------------------------------------- #
# Real stdio subprocess
# --------------------------------------------------------------------------- #
def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"src{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    return env


def _rpc(proc: subprocess.Popen, payload: dict) -> dict:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


@pytest.fixture
def bridge():
    proc = subprocess.Popen(
        [sys.executable, "-m", "maia.mcp.bridge", "--server", "airtable"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=REPO_ROOT, env=_child_env(),
    )
    yield proc
    try:
        proc.stdin.close()
        proc.wait(timeout=10)
    except Exception:  # pragma: no cover - best-effort cleanup
        proc.kill()


def test_stdio_bridge_handshake_and_tool_list(bridge):
    init = _rpc(bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": MCP_PROTOCOL_VERSION}})
    assert init["result"]["serverInfo"]["name"] == "airtable"

    bridge.stdin.write(json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    bridge.stdin.flush()

    listed = _rpc(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "create_marketing_task" in [t["name"] for t in listed["result"]["tools"]]


def test_stdio_bridge_tool_call_round_trip(bridge):
    _rpc(bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": MCP_PROTOCOL_VERSION}})
    called = _rpc(bridge, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "create_marketing_task",
                   "arguments": {"campaign_id": "stdio-demo", "channel": "zalo",
                                 "content": "Noi dung kiem chung qua stdio."}},
    })
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"]["campaign_id"] == "stdio-demo"


def test_stdio_bridge_reports_json_parse_errors(bridge):
    bridge.stdin.write("{not json}\n")
    bridge.stdin.flush()
    error = json.loads(bridge.stdout.readline())
    assert error["error"]["code"] == -32700


def test_stdio_bridge_reports_unknown_method(bridge):
    _rpc(bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": MCP_PROTOCOL_VERSION}})
    error = _rpc(bridge, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
    assert error["error"]["code"] == -32601


def test_stdio_transport_timeout_is_reported_not_hung(tmp_path):
    """A server that never answers must fail fast instead of blocking a turn."""
    script = tmp_path / "silent_server.py"
    script.write_text("import sys\nfor line in sys.stdin:\n    pass\n", encoding="utf-8")
    transport = StdioTransport([sys.executable, str(script)], timeout=1.0)
    try:
        with pytest.raises(TransportError, match="did not respond"):
            transport.send({"jsonrpc": "2.0", "id": "x", "method": "ping"})
    finally:
        transport.close()


def test_stdio_transport_reports_a_dead_child(tmp_path):
    script = tmp_path / "exit_server.py"
    script.write_text("raise SystemExit(3)\n", encoding="utf-8")
    transport = StdioTransport([sys.executable, str(script)], timeout=2.0)
    try:
        with pytest.raises(TransportError):
            transport.send({"jsonrpc": "2.0", "id": "x", "method": "ping"})
    finally:
        transport.close()

