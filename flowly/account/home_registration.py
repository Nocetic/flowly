"""A self-hosted Core's home on the Flowly server (server/PROTOCOL.md B6).

A Core on a Mac or a VPS registers with the account's Firebase ID token and
gets its own agent secret; ``channels.flowlyServer`` then replaces the relay
as its ``web`` channel. The server keeps only the secret's hash, so this
machine's config is the one place it lives.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from flowly.account import audit_log
from flowly.config.loader import load_config, save_config
from flowly.config.schema import FLOWLY_SERVER_URL_PREFIXES

REGISTER_PATH = "/v1/homes/register"


class HomeRegistrationError(RuntimeError):
    """The server refused the registration; ``code`` is its error code."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class HomeRegistration:
    agent_host_id: str
    agent_secret: str
    created: bool


def hosting() -> str:
    """``mac`` on a Mac; any other Core is a self-hosted server (``vps``)."""
    return "mac" if sys.platform == "darwin" else "vps"


def http_origin(server_url: str) -> str:
    """``wss://host/v1/agent`` → ``https://host`` (``ws://`` only to this machine)."""
    url = server_url.strip()
    if not url.startswith(FLOWLY_SERVER_URL_PREFIXES):
        raise HomeRegistrationError("INVALID_SERVER_URL", "the Flowly server URL must use wss://")
    parsed = urlparse(url.replace("wss://", "https://", 1).replace("ws://", "http://", 1))
    return f"{parsed.scheme}://{parsed.netloc}"


async def register_home(
    id_token: str,
    *,
    server_url: str,
    name: str,
    host_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> HomeRegistration:
    """Register (``host_id``: re-register) this Core; the secret is answered once."""
    body: dict[str, str] = {"name": name[:60], "hosting": hosting()}
    if host_id:
        body["hostId"] = host_id
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await client.post(
            http_origin(server_url) + REGISTER_PATH,
            headers={"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"},
            json=body,
        )
    finally:
        if own:
            await client.aclose()
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code not in (200, 201):
        code = str(data.get("error") or f"HTTP_{response.status_code}")
        audit_log.error("home.register.failed", status=response.status_code, error=code)
        raise HomeRegistrationError(code, str(data.get("message") or code))
    host, secret = str(data.get("agentHostId") or ""), str(data.get("agentSecret") or "")
    if not host or not secret:
        raise HomeRegistrationError("MALFORMED", "the server answered without a host id or secret")
    audit_log.info("home.register.success", host=host, created=bool(data.get("created")))
    return HomeRegistration(agent_host_id=host, agent_secret=secret, created=bool(data.get("created")))


def wire_flowly_server(registration: HomeRegistration, server_url: str) -> None:
    """Turn ``channels.flowlyServer`` on with this registration.

    The relay settings stay as they are: while this channel is on the relay is
    not used, and ``unwire_flowly_server`` returns to it.
    """
    config = load_config()
    channel = config.channels.flowly_server
    channel.enabled = True
    channel.server_url = server_url
    channel.agent_secret = registration.agent_secret
    channel.agent_host_id = registration.agent_host_id
    save_config(config)
    audit_log.info("home.wired", host=registration.agent_host_id)


def unwire_flowly_server() -> bool:
    """Turn ``channels.flowlyServer`` off (the relay, if set, takes over again)."""
    config = load_config()
    channel = config.channels.flowly_server
    if not (channel.enabled or channel.agent_secret):
        return False
    channel.enabled = False
    channel.agent_secret = ""
    save_config(config)
    audit_log.info("home.unwired", host=channel.agent_host_id)
    return True
