"""Connect-once MCP probe — shared by the CLI (`mcp add --probe` / `mcp test`),
the OAuth login flow, and the feature-RPC ``mcp.test`` method.

The probe reuses the production client path (:class:`MCPServerTask`) so what it
reports matches exactly what the agent will see when it connects at boot. It
spins the server up, lists its tools, and tears it down — no registration, no
side effects on the running agent.
"""

from __future__ import annotations


def probe_tool_names(
    name: str, cfg_dump: dict, *, interactive: bool = False,
) -> tuple[bool, list[str], str]:
    """Connect once to a server config and return ``(ok, tool_names, error)``.

    ``cfg_dump`` is a snake_case server config dict (e.g. ``MCPServerConfig`` via
    ``model_dump()``) — env ``${VAR}`` placeholders are interpolated here.
    ``interactive`` lets an OAuth-configured server launch the browser flow.
    On failure returns ``(False, [], message)`` with credentials redacted.
    """
    try:
        from flowly.mcp.client import (
            _MCP_AVAILABLE,
            MCPServerTask,
            _ensure_loop,
        )
        from flowly.mcp.security import interpolate_env_vars, sanitize_error
        from flowly.mcp.env_loader import load_flowly_dotenv
    except ImportError as exc:
        return False, [], f"MCP runtime not importable: {exc}"

    if not _MCP_AVAILABLE:
        return False, [], "mcp SDK is not installed (`pip install mcp`)"

    load_flowly_dotenv()
    cfg = interpolate_env_vars(dict(cfg_dump))

    # An interactive OAuth flow blocks on a human authorizing in the
    # browser — give the connection (and the outer future) room for that.
    if interactive and cfg.get("auth") == "oauth":
        cfg["connect_timeout"] = max(float(cfg.get("connect_timeout", 60.0)), 300.0)

    loop = _ensure_loop()
    import asyncio

    async def _connect_and_list() -> list[str]:
        task = MCPServerTask(name)
        task.interactive = interactive
        await task.start(cfg)
        try:
            return [getattr(t, "name", "?") for t in task.tools]
        finally:
            await task.shutdown()

    future = asyncio.run_coroutine_threadsafe(_connect_and_list(), loop)
    try:
        tool_names = future.result(timeout=float(cfg.get("connect_timeout", 60.0)) + 30)
    except Exception as exc:
        return False, [], f"connect failed: {sanitize_error(str(exc) or repr(exc))}"
    return True, tool_names, ""


def probe_message(name: str, cfg_dump: dict, *, interactive: bool = False) -> tuple[bool, str]:
    """Connect once and return ``(ok, human_message)`` with a tool preview."""
    ok, tool_names, error = probe_tool_names(name, cfg_dump, interactive=interactive)
    if not ok:
        return False, error
    if not tool_names:
        return True, f"connected; server '{name}' reported no tools"
    preview = ", ".join(tool_names[:8])
    if len(tool_names) > 8:
        preview += f", … (+{len(tool_names) - 8} more)"
    return True, f"connected; {len(tool_names)} tool(s): {preview}"
