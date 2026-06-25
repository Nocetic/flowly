"""Server registration with the Flowly backend.

Mirrors the desktop client's pattern: POST /api/servers with provider
``desktop`` (the only enum value that bypasses cloud-provisioning and
plan checks) and ``machineId`` so the same physical machine de-dupes
to one Firestore server entry across desktop + TUI installs.

If a server already exists for this machineId, the endpoint returns
``existing: true`` and reuses the same ``gatewayAuthToken`` — we never
create duplicates.
"""

from __future__ import annotations

import platform
import socket
from dataclasses import dataclass
from typing import Any

import httpx

from flowly.account import audit_log
from flowly.account.auth import FLOWLY_API_BASE
from flowly.account.fingerprint import machine_id, machine_name

SERVERS_URL = f"{FLOWLY_API_BASE}/api/servers"


def _primary_local_ip() -> str:
    """Best-effort local IP of the primary outbound interface.

    Uses the standard UDP-socket trick: opening a datagram socket toward a
    public address makes the OS pick the egress interface without sending a
    single packet, so we can read its bound address. On a typical VPS this is
    the reachable (often public) IP; behind NAT it's the private LAN address.
    Returns "" on any failure — IP capture is purely informational.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except Exception:  # noqa: BLE001 - never let IP detection break login
        return ""


class ServerRegistrationError(Exception):
    pass


@dataclass
class RegisteredServer:
    server_id: str
    name: str
    status: str
    gateway_auth_token: str
    jwt_secret: str
    existing: bool


async def register_machine(id_token: str) -> RegisteredServer:
    """Get-or-create the Firestore server for the current machine.

    Idempotent: same ``machineId`` → same ``server_id`` every time. Safe
    to call on every login (the backend dedups). The returned
    ``gateway_auth_token`` is the bearer credential for
    ``/api/v1/chat/completions`` proxy calls.
    """
    mid = machine_id()
    name = machine_name()
    plat = platform.system().lower()  # 'darwin' | 'linux' | 'windows'
    audit_log.info("server.register.start", machine_id=mid, machine_name=name, platform=plat)

    payload: dict[str, Any] = {
        "name": name,
        "provider": "desktop",   # bypasses cloud + plan checks
        "machineId": mid,
        # Lets the dashboard label the machine accurately (Desktop vs Server)
        # instead of calling every relay-backed install "Desktop".
        "platform": plat,
        # This registration always comes from the Flowly bot itself.
        "botType": "flowly",
    }
    # Capture the reachable IP for headless/server installs only. Desktop apps
    # (macOS / Windows) are personal machines — we deliberately do NOT record
    # their local IP. Linux installs are the self-hosted VPS/server case the
    # dashboard used to require the user to type in by hand.
    if plat == "linux":
        ip = _primary_local_ip()
        if ip:
            payload["ipAddress"] = ip

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            SERVERS_URL,
            headers={
                "Authorization": f"Bearer {id_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if r.status_code not in (200, 201):
        try:
            err = r.json().get("error", "")
        except Exception:
            err = r.text[:200]
        audit_log.error("server.register.failed", status=r.status_code, error=err)
        raise ServerRegistrationError(f"register failed: {r.status_code} {err}")

    body = r.json()
    srv = body.get("server") or {}
    existing = bool(body.get("existing"))
    result = RegisteredServer(
        server_id=str(srv.get("id", "")),
        name=str(srv.get("name", "")),
        status=str(srv.get("status", "")),
        gateway_auth_token=str(body.get("gatewayAuthToken", "")),
        jwt_secret=str(body.get("jwtSecret", "")),
        existing=existing,
    )
    if not result.server_id or not result.gateway_auth_token:
        audit_log.error("server.register.malformed", body_keys=list(body.keys()))
        raise ServerRegistrationError("register response missing serverId or token")

    audit_log.info(
        "server.register.success",
        server_id=result.server_id,
        existing=existing,
        gateway_token=audit_log.safe_token_summary(result.gateway_auth_token),
    )
    return result
