"""Flowly-as-MCP-server (Faz 3, M1).

``flowly mcp serve`` runs a FastMCP server on stdio so external MCP clients
(Claude Desktop, Cursor, another agent) can read Flowly's conversation
history and — when the gateway is running and writes are allowed — send
messages and resolve approvals.

- :mod:`readplane` holds standalone readers over Flowly's session storage
  (JSONL + SQLite FTS index) and channel config. No gateway needed.
- :mod:`serve` wires those readers (and the gateway-backed write tools)
  into a FastMCP server and runs it.
"""

from __future__ import annotations

__all__ = ["run_server"]


def run_server(*, allow_writes: bool = False, verbose: bool = False) -> None:
    """Entry point used by ``flowly mcp serve``."""
    from flowly.mcp.server.serve import run_server as _run
    _run(allow_writes=allow_writes, verbose=verbose)
