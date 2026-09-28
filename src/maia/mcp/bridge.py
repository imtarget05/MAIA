"""Stdio bridge: run any MAIA MCP server as a subprocess for a real MCP client.

Usage (the command an MCP client config would point at)::

    python -m maia.mcp.bridge --server airtable
    python -m maia.mcp.bridge --server notification --list-tools

Framing is the MCP stdio convention: one JSON-RPC message per line on stdin,
one JSON-RPC message per line on stdout, **nothing else on stdout** — a stray
``print`` would corrupt the stream for the client, so all diagnostics go to
stderr (and ``--log-file``).

This module is intentionally tiny: it only reads, delegates to
:meth:`MCPServer.handle`, and writes. All behaviour lives in the server, which is
what lets the same code be tested in-process *and* over a real pipe.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

from .protocol import JSONRPC_VERSION, RPCErrorCode, error_response
from .server import MCPServer
from .servers import SERVER_NAMES, build_server

__all__ = ["main", "serve_stdio"]


def _write(message: dict[str, Any], stream: TextIO) -> None:
    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    stream.flush()


def serve_stdio(
    server: MCPServer,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Serve MCP over stdio until EOF. Returns a process exit code."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    log = stderr or sys.stderr

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(
                error_response(
                    None, RPCErrorCode.PARSE_ERROR, f"invalid JSON: {exc}"
                ),
                sink,
            )
            continue

        if not isinstance(message, dict):
            _write(
                error_response(
                    None, RPCErrorCode.INVALID_REQUEST, "message must be a JSON object"
                ),
                sink,
            )
            continue

        try:
            response = server.handle(message)
        except Exception as exc:  # last-resort guard: the loop must survive
            log.write(f"[mcp-bridge] handler crashed: {type(exc).__name__}: {exc}\n")
            log.flush()
            if "id" in message:
                _write(
                    error_response(
                        message.get("id"),
                        RPCErrorCode.INTERNAL_ERROR,
                        f"{type(exc).__name__}: {exc}",
                    ),
                    sink,
                )
            continue

        if response is not None:
            _write(response, sink)

    log.write(f"[mcp-bridge] {server.info.name} stdin closed, shutting down\n")
    log.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m maia.mcp.bridge",
        description="Serve a MAIA MCP server over stdio (JSON-RPC 2.0).",
    )
    parser.add_argument(
        "--server", required=False, choices=list(SERVER_NAMES),
        help="which MAIA MCP server to expose",
    )
    parser.add_argument(
        "--list-tools", action="store_true",
        help="print the tool list as JSON and exit (handy for verifying a setup)",
    )
    args = parser.parse_args(argv)

    server_name = args.server or SERVER_NAMES[0]
    try:
        server = build_server(server_name)
    except KeyError as exc:
        sys.stderr.write(f"[mcp-bridge] {exc}\n")
        return 2

    if args.list_tools:
        # Emitted on stdout *because* the caller asked for a one-shot report (not a
        # protocol stream), so the "nothing else on stdout" rule does not apply.
        print(
            json.dumps(
                {"server": server.info.to_mcp(),
                 "tools": server.list_tools()["tools"],
                 "jsonrpc": JSONRPC_VERSION},
                ensure_ascii=False, indent=2,
            )
        )
        return 0

    if server.info.name != server_name:  # defensive: builder ignored the name
        sys.stderr.write(
            f"[mcp-bridge] requested {server_name!r} but got {server.info.name!r}\n"
        )
        return 2
    return serve_stdio(server)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
