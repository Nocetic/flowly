"""One feature RPC to this machine's running gateway, from a CLI command.

A running gateway keeps state such as the cron schedule in memory and
writes it back on its own, so a CLI command that edits the file under it
is overwritten at the gateway's next save: ``flowly cron remove`` said
"archived" while the job kept firing. Commands that change such state ask
the running gateway to make the change, and only edit the files themselves
when no gateway is running.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from urllib.parse import quote


class GatewayRpcError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _serves_this_home(port: int) -> bool:
    """Whether the gateway on ``port`` is the one serving this Flowly home.

    Anything else there (the owner's own gateway while a test or another
    profile runs this command, an older gateway that does not say) is left
    alone, and the command edits its own files as before.
    """
    import json
    import urllib.request

    from flowly.gateway.identity import GATEWAY_SERVICE_ID, home_id

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as response:
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
    except Exception:  # noqa: BLE001 — nothing answering is "not running"
        return False
    return (
        isinstance(payload, dict)
        and payload.get("service_id") == GATEWAY_SERVICE_ID
        and payload.get("home_id") == home_id()
    )


def _target(port: int) -> tuple[int, str] | None:
    """The running gateway of the active profile: (port, token), or None."""
    from flowly.config.loader import load_config
    from flowly.profile import get_active_profile_name, get_flowly_home, read_runtime_lease

    if get_active_profile_name() not in ("default", "custom"):
        # A named agent listens on a port of its own, recorded in its lease.
        lease = read_runtime_lease(get_flowly_home())
        if not lease:
            return None
        try:
            port, token = int(lease.get("port") or 0), str(lease.get("authToken") or "")
        except (TypeError, ValueError):
            return None
    else:
        config = load_config()
        port, token = port or config.gateway.port, (config.gateway.token or "")
    if not 1 <= port <= 65_535 or not _serves_this_home(port):
        return None
    return port, token


async def _call(port: int, token: str, method: str, params: dict[str, Any], timeout: float) -> Any:
    import aiohttp

    url = f"ws://127.0.0.1:{port}/ws" + (f"?token={quote(token, safe='')}" if token else "")
    rpc_id = uuid.uuid4().hex
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_close=timeout)) as ws:
            await ws.send_json({"type": "rpc", "id": rpc_id, "method": method, "params": params})
            async with asyncio.timeout(timeout):
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    frame = message.json()
                    if not isinstance(frame, dict) or frame.get("type") != "rpc" or frame.get("id") != rpc_id:
                        continue
                    error = frame.get("error")
                    if isinstance(error, dict):
                        raise GatewayRpcError(str(error.get("code") or "ERROR"), str(error.get("message") or ""))
                    return frame.get("result")
    raise GatewayRpcError("DISCONNECTED", "The gateway closed the connection before answering.")


def call_running_gateway(
    method: str,
    params: dict[str, Any],
    *,
    port: int = 0,
    timeout: float = 15.0,
) -> tuple[bool, Any]:
    """Ask the running gateway. ``(False, None)`` when none is running.

    Raises :class:`GatewayRpcError` when a gateway answered with an error.
    """
    import aiohttp

    target = _target(port)
    if target is None:
        return False, None
    try:
        return True, asyncio.run(_call(target[0], target[1], method, params, timeout))
    except (aiohttp.ClientConnectorError, ConnectionRefusedError):
        return False, None
