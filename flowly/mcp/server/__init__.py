"""Flowly-as-MCP-server (Faz 3, M1).

``flowly mcp serve`` runs an MCP server on stdio or Streamable HTTP so external MCP clients
(Claude Desktop, Cursor, another agent) can read Flowly's conversation
history and — when the gateway is running and writes are allowed — send
messages and resolve approvals.

- :mod:`readplane` holds standalone readers over Flowly's session storage
  (JSONL + SQLite FTS index) and channel config. No gateway needed.
- :mod:`serve` wires those readers (and the gateway-backed write tools)
  into an MCP server and runs it.
"""

from __future__ import annotations

__all__ = ["run_server"]


def run_server(
    *,
    allow_writes: bool = False,
    verbose: bool = False,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    stateless: bool = False,
    auth_token: str = "",
    tls_cert: str = "",
    tls_key: str = "",
) -> None:
    """Entry point used by ``flowly mcp serve``."""
    from flowly.mcp.server.serve import run_server as _run
    _run(
        allow_writes=allow_writes,
        verbose=verbose,
        transport=transport,
        host=host,
        port=port,
        path=path,
        stateless=stateless,
        auth_token=auth_token,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )
