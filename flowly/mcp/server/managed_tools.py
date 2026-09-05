"""Ephemeral tool authority for one runtime-owned delegated turn."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from flowly.agent.tool_context import current_tool_origin
from flowly.mcp.server.tool_bridge import GRANT_ENV, register_tool_bridge_routes
from flowly.mcp.server.tool_runtime import RuntimeToolBridge, ToolBridgeError


@dataclass(frozen=True)
class ManagedToolLaunch:
    command: str
    args: list[str]
    env: dict[str, str] = field(repr=False)
    tools: tuple[str, ...] = ()
    withdraw: Callable[[], None] = field(default=lambda: None, repr=False, compare=False)


def _callback_command() -> tuple[str, list[str]]:
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        executable = Path(sys.argv[0]).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ToolBridgeError("The bundled tool callback executable is unavailable")
        return str(executable), ["mcp", "tools"]
    return sys.executable, ["-m", "flowly", "mcp", "tools"]


@asynccontextmanager
async def managed_tool_launch(owner: Any, session_key: str, *, allow_writes: bool, ttl: float = 660):
    """Capture the parent's exact authority; never consult a global active chat.

    A private loopback listener also works in the TUI or a managed profile
    without an advertised gateway. It wraps the existing registry and captures
    its profile-owner context; no second runtime or Board is constructed.
    """
    from aiohttp import web

    origin = current_tool_origin()
    if origin is None or origin.session_key != session_key or origin.allowed_tools is None:
        raise ToolBridgeError("Managed tools require an explicit owning session and tool permissions")
    bridge = getattr(owner, "_external_tool_bridge", None)
    if bridge is None:
        bridge = owner._external_tool_bridge = RuntimeToolBridge(owner)
    grant = bridge.create_grant(session_key, allow_writes=allow_writes, ttl=ttl, allow_empty=True)
    pending = []

    def withdraw():
        try:
            pending.extend(bridge.revoke(grant["token"]))
        except ToolBridgeError:  # Expiry or owner shutdown already withdrew it.
            pass

    app = web.Application()
    register_tool_bridge_routes(app, bridge, admin_token=None, close_on_shutdown=False)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    try:
        command, args = _callback_command()
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        import flowly
        from flowly.profile import get_flowly_home

        # Pin this installed package, independent of the delegated project cwd.
        package_root = str(Path(flowly.__file__).resolve().parent.parent)
        env = {
            "FLOWLY_QUIET": "1", "PYTHONPATH": package_root,
            "FLOWLY_HOME": str(get_flowly_home()),
            GRANT_ENV: json.dumps({"endpoint": f"http://127.0.0.1:{port}/api/mcp/tools", "token": grant["token"]}),
        }
        yield ManagedToolLaunch(command, args, env, tuple(grant["tools"]), withdraw)
    finally:
        withdraw()
        try:
            if pending:
                await asyncio.wait(pending, timeout=2)
        finally:
            await runner.cleanup()
