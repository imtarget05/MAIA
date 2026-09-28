"""Transports: in-process (tests/API) and stdio (real MCP client use).

Both implement the same two-method contract — ``send(message) -> response | None``
and ``close()`` — so :class:`~maia.mcp.client.MCPClient` never branches on which
one it holds.

``StdioTransport`` deserves the notes it carries: it speaks the actual MCP stdio
framing (newline-delimited JSON-RPC on the child's stdin/stdout), correlates
responses **by id** (a server may emit notifications between our request and its
reply), bounds every call with a timeout, and captures the child's stderr for
diagnostics. A hung or half-dead child becomes a transport error instead of
blocking the agent turn forever.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any, Protocol

from .protocol import JSONRPC_VERSION, MCPError, RPCErrorCode
from .server import MCPServer

__all__ = ["InProcessTransport", "StdioTransport", "Transport", "TransportError"]


class TransportError(RuntimeError):
    """The transport itself failed (spawn, pipe, timeout, shutdown)."""


class Transport(Protocol):  # pragma: no cover - typing only
    def send(self, message: dict[str, Any]) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class InProcessTransport:
    """Calls :meth:`MCPServer.handle` directly.

    Used by the API layer and unit tests: no subprocess, no serialisation
    boundary beyond the dicts themselves, fully deterministic.
    """

    def __init__(self, server: MCPServer) -> None:
        self._server = server
        self.closed = False
        self.calls: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if self.closed:
            raise TransportError("transport is closed")
        self.calls.append(message)
        return self._server.handle(message)

    def close(self) -> None:
        self.closed = True


class StdioTransport:
    """Newline-delimited JSON-RPC over a child process' stdin/stdout."""

    def __init__(
        self,
        argv: list[str],
        *,
        timeout: float = 15.0,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.argv = argv
        self.timeout = timeout
        self._lock = threading.Lock()
        self._stderr_lines: list[str] = []
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line buffered: MCP stdio framing is line-based
                cwd=cwd,
                env=env,
            )
        except OSError as exc:
            raise TransportError(f"cannot start MCP server {argv!r}: {exc}") from exc
        self._reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()

    @classmethod
    def for_server_module(
        cls,
        server_name: str,
        *,
        timeout: float = 15.0,
        python_executable: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> StdioTransport:
        """Spawn ``python -m maia.mcp.bridge --server <name>``.

        ``PYTHONPATH`` is extended so the child can import ``maia`` even when the
        parent runs from another working directory — otherwise the failure mode
        is "works in my shell, not from the API".
        """
        import os

        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        src_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing}" if existing else src_dir
        return cls(
            [python_executable or sys.executable, "-m", "maia.mcp.bridge",
             "--server", server_name],
            timeout=timeout,
            env=env,
        )

    # ---- transport contract ---------------------------------------------
    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if self._proc.poll() is not None:
                raise TransportError(
                    "MCP server process exited "
                    f"(code={self._proc.returncode}); stderr tail: {self.stderr_tail()}"
                )
            is_notification = "id" not in message
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(message) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise TransportError(
                    f"MCP server stdin closed: {exc}; stderr tail: {self.stderr_tail()}"
                ) from exc

            if is_notification:
                return None
            return self._read_response(message.get("id"))

    def _read_response(self, request_id: Any) -> dict[str, Any] | None:
        """Read until the reply with ``request_id`` arrives, skipping notifications."""
        for _ in range(10_000):  # guard against an endless notification stream
            line = self._readline_with_timeout()
            if line is None:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # A server that logs to stdout corrupts the framing; skip the line
                # (kept for diagnostics) instead of failing the whole call.
                self._stderr_lines.append(f"[non-json stdout] {line[:200]}")
                continue
            if not isinstance(payload, dict) or payload.get("id") != request_id:
                continue  # notification, or another request's reply
            if payload.get("jsonrpc") != JSONRPC_VERSION:
                raise TransportError("server reply is not JSON-RPC 2.0")
            return payload
        raise TransportError("MCP server did not answer the request")

    def _readline_with_timeout(self) -> str | None:
        """Read one line with a deadline; raises on timeout, not on EOF."""
        stdout = self._proc.stdout
        if stdout is None:  # pragma: no cover - only when Popen was built without pipes
            raise TransportError("MCP server process has no stdout pipe")
        result: list[str] = []

        def _read() -> None:
            try:
                result.append(stdout.readline())
            except Exception:  # pragma: no cover - platform dependent
                result.append("")

        worker = threading.Thread(target=_read, daemon=True)
        worker.start()
        worker.join(self.timeout)
        if worker.is_alive():
            raise TransportError(
                f"MCP server did not respond within {self.timeout}s; "
                f"stderr tail: {self.stderr_tail()}"
            )
        if not result:
            raise TransportError(
                f"MCP server closed stdout (exit={self._proc.poll()}); "
                f"stderr tail: {self.stderr_tail()}"
            )
        return result[0]

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr_lines.append(line.rstrip("\n"))
            if len(self._stderr_lines) > 200:  # bounded memory
                del self._stderr_lines[:50]

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def stderr_tail(self, lines: int = 5) -> str:
        return " | ".join(self._stderr_lines[-lines:]) or "(empty)"

    def close(self) -> None:
        proc = self._proc
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass


def make_transport(
    server_name: str,
    server: MCPServer | None = None,
    *,
    mode: str = "inprocess",
    timeout: float = 15.0,
    server_factory: Callable[[str], MCPServer] | None = None,
) -> Transport:
    """Build the transport named by ``MCP_BRIDGE_TRANSPORT``.

    One factory keeps the mode decision in a single place: callers (API, agent
    dispatch, tests) never carry ``if mode == ...`` branches.
    """
    if mode == "inprocess":
        if server is None:
            if server_factory is None:
                from .servers import build_server

                server_factory = build_server
            server = server_factory(server_name)
        return InProcessTransport(server)
    if mode == "stdio":
        return StdioTransport.for_server_module(server_name, timeout=timeout)
    raise MCPError(
        RPCErrorCode.INVALID_REQUEST,
        f"unknown MCP transport mode {mode!r} (expected 'inprocess' or 'stdio')",
    )


