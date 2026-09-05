"""Launch-only settings for the session-scoped live tool callback."""

from __future__ import annotations

import asyncio

from flowly.codex.app_server import CodexProtocolError
from flowly.codex.tool_migration import _toml_value
from flowly.mcp.server.managed_tools import ManagedToolLaunch
from flowly.mcp.server.tool_bridge import GRANT_ENV

CATALOG_TIMEOUT_S = 30


def callback_server_config(launch: ManagedToolLaunch) -> dict:
    # No grant values in argv or persisted config. The child inherits only the
    # named variables from its host process. The client merges this with its
    # disk config; thread_tool_overrides separately disables other connections.
    return {
        "command": launch.command, "args": launch.args,
        "env_vars": sorted(launch.env), "enabled": True, "required": True,
        "env": {key: value for key, value in launch.env.items() if key != GRANT_ENV},
        "enabled_tools": list(launch.tools), "disabled_tools": [],
        "startup_timeout_sec": 30.0, "tool_timeout_sec": 600.0,
    }


def callback_overrides(launch: ManagedToolLaunch) -> list[str]:
    return [
        "mcp_servers=" + _toml_value({"flowly-tools": callback_server_config(launch)}),
        # Snapshotting the inherited shell environment would persist the lease.
        # The MCP child receives it via env_vars; ordinary shells do not need it.
        "features.shell_snapshot=false",
        # Account-backed apps and plugins can introduce MCP connections that
        # are absent from the static mcp_servers table.
        "features.apps=false",
        "features.plugins=false",
        f"shell_environment_policy.set.{GRANT_ENV}=\"\"",
    ]


async def thread_tool_overrides(client, cwd: str | None, server: dict) -> dict:
    response = await client.request("config/read", {"includeLayers": False, "cwd": cwd}, timeout=15)
    config = response.get("config")
    if not isinstance(config, dict):
        raise CodexProtocolError("Cannot verify the delegated client's tool configuration")
    overrides = {}
    for group in ("mcp_servers", "plugins"):
        entries = config.get(group) or {}
        if not isinstance(entries, dict) or len(entries) > 512:
            raise CodexProtocolError("Delegated client tool configuration exceeds the supported shape")
        selected = {}
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                raise CodexProtocolError("Cannot validate a delegated client tool configuration")
            if group == "mcp_servers" and name == "flowly-tools":
                # A disk value takes precedence over env_vars in some clients.
                # Never copy a persisted credential or silently use an old grant.
                env = entry.get("env") or {}
                if not isinstance(env, dict) or GRANT_ENV in env:
                    raise CodexProtocolError("Remove the persisted tool grant from the callback configuration")
                continue
            # Do not copy credentials or nullable SDK output fields back into
            # thread configuration. The nested override merges with disk data.
            selected[name] = {"enabled": False}
            if group == "mcp_servers":
                selected[name]["required"] = False
        if selected:
            overrides[group] = selected
    # A per-thread root override supersedes the corresponding launch override.
    # Include our owned entry explicitly, without credentials or nullable SDK fields.
    overrides.setdefault("mcp_servers", {})["flowly-tools"] = server
    return overrides


async def verify_thread_tools(client, thread_id: str, expected_tools: set[str]) -> None:
    async with asyncio.timeout(CATALOG_TIMEOUT_S):
        await _verify_thread_tools(client, thread_id, expected_tools)


async def _verify_thread_tools(client, thread_id: str, expected_tools: set[str]) -> None:
    cursor = None
    seen = set()
    found = False
    for _ in range(32):
        result = await client.request("mcpServerStatus/list", {
            "threadId": thread_id, "cursor": cursor, "limit": 100,
        }, timeout=30)
        entries = result.get("data")
        if not isinstance(entries, list) or len(entries) > 100:
            raise CodexProtocolError("Cannot verify the delegated MCP tool surface")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("tools"), dict):
                raise CodexProtocolError("Cannot verify the delegated MCP tool surface")
            if entry.get("name") != "flowly-tools":
                if entry["tools"]:
                    raise CodexProtocolError("Delegated MCP connections exceed the parent runtime's grant")
                continue
            if found or set(entry["tools"]) != expected_tools:
                raise CodexProtocolError("Delegated callback tools do not match the parent runtime's grant")
            found = True
        cursor = result.get("nextCursor")
        if not cursor:
            if not found:
                raise CodexProtocolError("The delegated callback connection did not initialize")
            return
        if not isinstance(cursor, str) or cursor in seen:
            break
        seen.add(cursor)
    raise CodexProtocolError("Delegated MCP discovery did not finish within its bounds")
