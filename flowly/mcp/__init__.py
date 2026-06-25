"""MCP (Model Context Protocol) client integration.

Public API:
- :func:`discover_mcp_tools` — read ``Config.mcp_servers``, connect to each
  enabled server in parallel, register their tools into the agent's
  ``ToolRegistry``. Safe to call once at agent boot. Per-server failures
  are logged and do not block the agent.
- :func:`shutdown_mcp_servers` — graceful teardown of all live server
  tasks. Called from the agent loop shutdown path.

Faz 1 scope (this module set):
- stdio + HTTP transports
- discovery, ``include`` / ``exclude`` filtering
- schema normalization for provider-portable tool input schemas
- credential redaction in error messages
- prompt-injection scan on tool descriptions (log-only)
- ``${VAR}`` interpolation in env/args/headers
- shared subprocess stderr log at ``$FLOWLY_HOME/logs/mcp-stderr.log``

Faz 2+: SSE, OAuth, resources/prompts, image content, list_changed
notifications, circuit breaker, mTLS — see docs/MCP_PLAN.md.
"""

from __future__ import annotations

from flowly.mcp.client import (
    discover_mcp_tools,
    shutdown_mcp_servers,
)

__all__ = [
    "discover_mcp_tools",
    "shutdown_mcp_servers",
]
