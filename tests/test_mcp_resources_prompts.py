"""Resources + prompts over MCP: discovery, rendering, and error semantics.

Two levels are covered:

* :class:`MCPServer` mechanics — registration, dispatch, and the exact error
  code for an unknown uri / prompt / missing argument. These go through
  ``handle()`` so they are real JSON-RPC messages, not direct method calls.
* The ``prompts`` integration server — that it reads the PromptOps library
  (``prompts/**.yaml``) rather than a second copy of it, that it exposes the
  current release under each name, and that drafts stay unreachable.

The PromptOps behaviour under the render (guardrails, variable validation) is
already covered by ``test_prompt_library.py``; what is pinned here is the MCP
surface on top of it.
"""
from __future__ import annotations

import pytest

from maia.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    METHOD_INITIALIZE,
    METHOD_PROMPTS_GET,
    METHOD_PROMPTS_LIST,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    PromptArgument,
    PromptMessage,
    PromptSpecWire,
    ResourceSpec,
    RPCErrorCode,
    ToolResult,
)
from maia.mcp.server import MCPServer
from maia.mcp.servers import build_server
from maia.mcp.servers.prompts_server import PROMPT_URI_PREFIX, build_prompts_server


def _req(request_id: int, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method,
            "params": params or {}}


def _result(server: MCPServer, request_id: int, method: str, params=None) -> dict:
    """Call through the JSON-RPC entry point and return the result payload."""
    response = server.handle(_req(request_id, method, params))
    assert response is not None, f"{method} must produce a response"
    assert "error" not in response, f"{method} unexpectedly errored: {response['error']}"
    return response["result"]


def _error(server: MCPServer, request_id: int, method: str, params=None) -> dict:
    response = server.handle(_req(request_id, method, params))
    assert response is not None
    assert "error" in response, f"{method} unexpectedly succeeded: {response}"
    return response["error"]


def _server() -> MCPServer:
    server = MCPServer("doc-server", version="1.0.0")

    @server.tool("noop", description="Does nothing.", input_schema={}, read_only=True)
    def _noop(args):
        return ToolResult.text("ok")

    server.add_resource(
        ResourceSpec(
            uri="maia://docs/readme",
            name="readme",
            description="Project readme.",
            mime_type="text/markdown",
        ),
        "# Hello\n",
    )
    server.add_prompt(
        PromptSpecWire(
            name="greet",
            description="Greet someone.",
            arguments=(PromptArgument(name="who", description="Name", required=True),),
        ),
        lambda args: [
            PromptMessage(role="system", text="You greet people."),
            PromptMessage(role="user", text=f"Greets {args['who']}."),
        ],
    )
    return server


# --- resources ----------------------------------------------------------- #

def test_resources_list_returns_registered_spec():
    result = _result(_server(), 1, METHOD_RESOURCES_LIST)
    assert [r["uri"] for r in result["resources"]] == ["maia://docs/readme"]
    entry = result["resources"][0]
    assert entry["name"] == "readme"
    assert entry["mimeType"] == "text/markdown"
    # The listing advertises metadata only — body comes from resources/read.
    assert "text" not in entry


def test_resources_read_returns_the_body():
    result = _result(_server(), 1, METHOD_RESOURCES_READ, {"uri": "maia://docs/readme"})
    assert result["contents"][0]["text"] == "# Hello\n"
    assert result["contents"][0]["uri"] == "maia://docs/readme"
    assert result["contents"][0]["mimeType"] == "text/markdown"


def test_unknown_resource_uri_is_invalid_params():
    error = _error(_server(), 1, METHOD_RESOURCES_READ, {"uri": "maia://docs/nope"})
    assert error["code"] == RPCErrorCode.INVALID_PARAMS
    # The error lists what exists so a client can self-correct.
    assert "maia://docs/readme" in str(error.get("data"))




# --- prompts ------------------------------------------------------------- #

def test_prompts_list_advertises_arguments():
    result = _result(_server(), 1, METHOD_PROMPTS_LIST)
    assert [p["name"] for p in result["prompts"]] == ["greet"]
    (arg,) = result["prompts"][0]["arguments"]
    assert arg == {"name": "who", "description": "Name", "required": True}


def test_prompt_without_arguments_omits_the_key():
    server = MCPServer("bare")
    server.add_prompt(
        PromptSpecWire(name="static", description="No inputs."),
        lambda args: [PromptMessage(role="user", text="fixed")],
    )
    result = _result(server, 1, METHOD_PROMPTS_LIST)
    assert "arguments" not in result["prompts"][0]


def test_prompts_get_renders_messages():
    result = _result(_server(), 1, METHOD_PROMPTS_GET,
                     {"name": "greet", "arguments": {"who": "An"}})
    assert result["description"] == "Greet someone."
    assert [m["role"] for m in result["messages"]] == ["system", "user"]
    assert result["messages"][1]["content"] == {"type": "text", "text": "Greets An."}


def test_prompts_get_omitting_arguments_key_uses_an_empty_dict():
    """``arguments`` is optional in the spec, so its absence must not error."""
    server = MCPServer("noargs")
    server.add_prompt(
        PromptSpecWire(name="static", description="No inputs."),
        lambda args: [PromptMessage(role="user", text=f"saw {sorted(args)}")],
    )
    result = _result(server, 1, METHOD_PROMPTS_GET, {"name": "static"})
    assert result["messages"][0]["content"]["text"] == "saw []"


def test_unknown_prompt_is_invalid_params():
    error = _error(_server(), 1, METHOD_PROMPTS_GET, {"name": "nope"})
    assert error["code"] == RPCErrorCode.INVALID_PARAMS
    assert "greet" in str(error.get("data"))


def test_prompts_get_requires_the_name_param():
    error = _error(_server(), 1, METHOD_PROMPTS_GET, {})
    assert error["code"] == RPCErrorCode.INVALID_PARAMS


def test_renderer_value_error_becomes_invalid_params():
    def _strict(args):
        if "who" not in args:
            raise ValueError("missing 'who'")
        return [PromptMessage(role="user", text=args["who"])]

    server = MCPServer("strict")
    server.add_prompt(PromptSpecWire(name="greet"), _strict)
    error = _error(server, 1, METHOD_PROMPTS_GET, {"name": "greet", "arguments": {}})
    assert error["code"] == RPCErrorCode.INVALID_PARAMS
    assert "who" in error["message"]


def test_renderer_bug_surfaces_as_internal_error_not_invalid_params():
    """A renderer that raises anything but ValueError is our bug, not the caller's."""
    def _buggy(args):
        raise KeyError("who")

    server = MCPServer("buggy")
    server.add_prompt(PromptSpecWire(name="greet"), _buggy)
    error = _error(server, 1, METHOD_PROMPTS_GET, {"name": "greet", "arguments": {}})
    assert error["code"] == RPCErrorCode.INTERNAL_ERROR

def test_resources_read_requires_the_uri_param():
    for params in ({}, {"uri": ""}, {"uri": 42}):
        error = _error(_server(), 1, METHOD_RESOURCES_READ, params)
        assert error["code"] == RPCErrorCode.INVALID_PARAMS


# --- capabilities -------------------------------------------------------- #

def test_initialize_advertises_only_registered_capabilities():
    result = _result(_server(), 1, METHOD_INITIALIZE,
                     {"protocolVersion": MCP_PROTOCOL_VERSION})
    caps = result["capabilities"]
    assert set(caps) == {"tools", "resources", "prompts"}
    assert caps["resources"]["subscribe"] is False


def test_initialize_omits_capabilities_the_server_lacks():
    """Claiming a capability we would answer with an empty list is a lie."""
    server = MCPServer("bare")

    @server.tool("noop", description="x", input_schema={})
    def _noop(args):
        return ToolResult.text("ok")

    caps = _result(server, 1, METHOD_INITIALIZE,
                   {"protocolVersion": MCP_PROTOCOL_VERSION})["capabilities"]
    assert set(caps) == {"tools"}


# --- registration guards ------------------------------------------------- #

def test_duplicate_resource_uri_is_rejected():
    server = _server()
    with pytest.raises(ValueError, match="duplicate resource uri"):
        server.add_resource(ResourceSpec(uri="maia://docs/readme"), "x")


def test_duplicate_prompt_name_is_rejected():
    server = _server()
    with pytest.raises(ValueError, match="duplicate prompt name"):
        server.add_prompt(PromptSpecWire(name="greet"), lambda a: [])


def test_empty_names_are_rejected():
    server = MCPServer("bare")
    with pytest.raises(ValueError, match="uri cannot be empty"):
        server.add_resource(ResourceSpec(uri="  "), "x")
    with pytest.raises(ValueError, match="name cannot be empty"):
        server.add_prompt(PromptSpecWire(name=""), lambda a: [])


# --- the prompts integration server ------------------------------------- #

def test_prompts_server_is_registered_by_name():
    assert build_server("prompts").info.name == "prompts"


def test_prompts_server_exposes_the_library():
    server = build_prompts_server()
    names = [p["name"] for p in _result(server, 1, METHOD_PROMPTS_LIST)["prompts"]]
    # The four prompts that ship in prompts/ are all discoverable.
    assert set(names) == {
        "campaign_copywriter",
        "game_review_insight",
        "nl_to_sql",
        "retention_drop_briefing",
    }


def test_prompts_server_advertises_required_variables_as_arguments():
    server = build_prompts_server()
    listed = {p["name"]: p for p in _result(server, 1, METHOD_PROMPTS_LIST)["prompts"]}
    nl2sql = listed["nl_to_sql"]
    arg_names = {a["name"] for a in nl2sql["arguments"]}
    assert {"question", "schema_block", "dialect", "max_rows"} <= arg_names
    # Every advertised argument is required — render() refuses a partial fill.
    assert all(a["required"] is True for a in nl2sql["arguments"])


def test_prompts_server_renders_a_real_prompt():
    server = build_prompts_server()
    result = _result(server, 1, METHOD_PROMPTS_GET, {
        "name": "nl_to_sql",
        "arguments": {
            "question": "CPI bao nhiêu?",
            "schema_block": "TABLE marketing_metrics(cpi REAL)",
            "dialect": "sqlite",
            "max_rows": "200",
        },
    })
    roles = [m["role"] for m in result["messages"]]
    assert roles[0] == "system" and roles[-1] == "user"
    text = "\n".join(m["content"]["text"] for m in result["messages"])
    # The declared variables are interpolated, not left as literals.
    assert "CPI bao nhiêu?" in text
    assert "TABLE marketing_metrics(cpi REAL)" in text
    assert "{question}" not in text


def test_prompts_server_rejects_a_missing_argument():
    server = build_prompts_server()
    error = _error(server, 1, METHOD_PROMPTS_GET,
                   {"name": "nl_to_sql", "arguments": {"question": "q"}})
    assert error["code"] == RPCErrorCode.INVALID_PARAMS
    # The message names the variables to fix.
    assert "dialect" in error["message"]


def test_prompts_server_reads_every_active_version_as_a_resource():
    server = build_prompts_server()
    uris = [r["uri"] for r in _result(server, 1, METHOD_RESOURCES_LIST)["resources"]]
    # campaign_copywriter has two active versions; both are readable so a client
    # can diff, while prompts/get still returns only the current one.
    assert "maia://prompts/campaign_copywriter/1.0.0" in uris
    assert "maia://prompts/campaign_copywriter/1.1.0" in uris
    assert all(u.startswith(PROMPT_URI_PREFIX) for u in uris)


def test_prompts_get_returns_the_current_release_not_the_oldest():
    server = build_prompts_server()
    result = _result(server, 1, METHOD_PROMPTS_GET, {
        "name": "campaign_copywriter",
        "arguments": {
            "game_name": "G", "update_name": "U", "key_features": "F",
            "channels": "facebook", "tone": "vui", "audience": "A",
            "legal_notes": "none", "language": "Tiếng Việt",
        },
    })
    text = "\n".join(m["content"]["text"] for m in result["messages"])
    # v1.1.0 added Zalo to the channel enum; v1.0.0 did not have it.
    assert "zalo" in text


def test_prompts_resource_body_carries_parameters_and_guardrails():
    server = build_prompts_server()
    result = _result(server, 1, METHOD_RESOURCES_READ,
                     {"uri": "maia://prompts/nl_to_sql/1.0.0"})
    body = result["contents"][0]["text"]
    assert "## Parameters" in body
    assert "## Guardrails" in body
    assert "## System prompt" in body
    # The read-only guarantee is stated in the prompt itself.
    assert "SELECT" in body


def test_prompts_server_honours_an_injected_registry(tmp_path):
    """A candidate prompt directory is reviewable before it becomes the default."""
    source = tmp_path / "p"
    source.mkdir()
    (source / "tiny.v1.0.0.yaml").write_text(
        "name: tiny\nversion: 1.0.0\ntitle: Tiny\nstatus: active\n"
        "system_prompt: Be brief.\nuser_template: 'Say {thing}'\n",
        encoding="utf-8",
    )
    from maia.promptops import PromptRegistry

    server = build_prompts_server(registry=PromptRegistry(source))
    names = [p["name"] for p in _result(server, 1, METHOD_PROMPTS_LIST)["prompts"]]
    assert names == ["tiny"]
    uris = [r["uri"] for r in _result(server, 1, METHOD_RESOURCES_LIST)["resources"]]
    assert uris == ["maia://prompts/tiny/1.0.0"]


def test_draft_prompts_are_not_reachable(tmp_path):
    """A prompt under review must not be servable to a production client."""
    source = tmp_path / "p"
    source.mkdir()
    (source / "wip.v1.0.0.yaml").write_text(
        "name: wip\nversion: 1.0.0\ntitle: WIP\nstatus: draft\n"
        "system_prompt: Draft.\nuser_template: 'Hi {who}'\n",
        encoding="utf-8",
    )
    from maia.promptops import PromptRegistry

    server = build_prompts_server(registry=PromptRegistry(source))
    assert _result(server, 1, METHOD_PROMPTS_LIST)["prompts"] == []
    assert _result(server, 2, METHOD_RESOURCES_LIST)["resources"] == []
    error = _error(server, 3, METHOD_PROMPTS_GET, {"name": "wip"})
    assert error["code"] == RPCErrorCode.INVALID_PARAMS




def test_empty_resource_list_is_still_a_valid_result():
    result = _result(MCPServer("bare"), 1, METHOD_RESOURCES_LIST)
    assert result == {"resources": []}
