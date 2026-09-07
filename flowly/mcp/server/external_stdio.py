"""Standard stdio adapter for clients that cannot connect to MCP HTTP directly.

Reads only the explicitly supplied endpoint/key, not gateway admin credentials,
the user's config, or session archives. Authority remains on the remote gateway.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from urllib.parse import urlsplit

from flowly.mcp.external_access import ExternalAccessError

ENDPOINT_ENV = "FLOWLY_MCP_ENDPOINT"
ACCESS_KEY_ENV = "FLOWLY_MCP_ACCESS_KEY"


def validate_connection(endpoint: str, token: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        valid = (
            bool(host) and (parsed.scheme == "https" or (parsed.scheme == "http" and local))
            and parsed.path == "/mcp" and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and (parsed.port is None or parsed.port > 0)
            and not any(char.isspace() or char == "\\" for char in endpoint)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ExternalAccessError("FLOWLY_MCP_ENDPOINT must be an HTTPS or loopback MCP address ending in /mcp")
    if not isinstance(token, str) or not re.fullmatch(r"flm_[a-f0-9]{64}", token):
        raise ExternalAccessError("FLOWLY_MCP_ACCESS_KEY must contain the scoped key created in Desktop")


async def run_external_stdio() -> None:
    import httpx
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.shared.exceptions import MCPError

    endpoint, token = os.environ.get(ENDPOINT_ENV, ""), os.environ.get(ACCESS_KEY_ENV, "")
    validate_connection(endpoint, token)
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(650, connect=5),
            follow_redirects=False, trust_env=False,
        ) as http:
            async with streamable_http_client(endpoint, http_client=http) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()

                    async def listing(_context, _params):
                        try:
                            return await client.list_tools()
                        except Exception:
                            raise MCPError(code=-32000, message="Flowly MCP access is unavailable or expired") from None

                    async def call(_context, params):
                        try:
                            return await client.call_tool(params.name, params.arguments or {})
                        except Exception:
                            return types.CallToolResult(
                                content=[types.TextContent(text="Flowly MCP call failed; do not automatically retry an uncertain write")],
                                isError=True,
                            )

                    server = Server("flowly", title="Flowly", on_list_tools=listing, on_call_tool=call)
                    async with stdio_server() as (incoming, outgoing):
                        await server.run(incoming, outgoing, server.create_initialization_options())
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ExternalAccessError("Flowly MCP connection ended; check the endpoint and scoped access key") from None
