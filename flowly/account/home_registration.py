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
    #: A relay-era document of this machine was taken over (its conversations carry over).
    adopted: bool = False


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
    adopt_machine_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> HomeRegistration:
    """Register this Core; the secret is answered once.

    ``host_id`` re-registers that document. ``adopt_machine_id`` (without
    ``host_id``) lets the server find this machine's relay-era document by its
    fingerprint, so its conversations appear in the new apps.
    """
    body: dict[str, str] = {"name": name[:60], "hosting": hosting()}
    if host_id:
        body["hostId"] = host_id
    elif adopt_machine_id:
        body["adoptMachineId"] = adopt_machine_id
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
    audit_log.info("home.register.success", host=host, created=bool(data.get("created")), adopted=bool(data.get("adopted")))
    return HomeRegistration(agent_host_id=host, agent_secret=secret, created=bool(data.get("created")),
                            adopted=bool(data.get("adopted")))


RELEASE_PATH = "/v1/homes/release"


async def release_home(
    server_url: str, host_id: str, secret: str, *, client: httpx.AsyncClient | None = None,
) -> None:
    """Tell the server this machine lets go of its home (D53): the owner's apps drop it.

    Proven like the agent's socket: its secret and host id. The document and
    its conversations stay on the server; ``server connect`` brings it back.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await client.post(
            http_origin(server_url) + RELEASE_PATH,
            headers={"Authorization": f"Bearer {secret}", "X-Flowly-Agent-Host": host_id},
        )
    except httpx.HTTPError as error:
        raise HomeRegistrationError("UNREACHABLE", str(error)) from error
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200:
        try:
            code = str(response.json().get("error") or "")
        except ValueError:
            code = ""
        raise HomeRegistrationError(code or f"HTTP_{response.status_code}")
    audit_log.info("home.released", host=host_id)


def wire_flowly_server(
    registration: HomeRegistration, server_url: str, *, owner_uid: str = "", owner_email: str = "",
) -> None:
    """Turn ``channels.flowlyServer`` on with this registration, made by ``owner_uid``.

    The relay settings stay as they are: while this channel is on the relay is
    not used, and ``unwire_flowly_server`` returns to it.
    """
    config = load_config()
    channel = config.channels.flowly_server
    channel.enabled = True
    channel.server_url = server_url
    channel.agent_secret = registration.agent_secret
    channel.agent_host_id = registration.agent_host_id
    channel.owner_uid = owner_uid
    channel.owner_email = owner_email or ""
    save_config(config)
    audit_log.info("home.wired", host=registration.agent_host_id)


# ── Which account this agent belongs to ─────────────────────────────────────


class HomeLinkedElsewhere(RuntimeError):
    """A sign-in as another account while this agent is linked to its owner's."""

    def __init__(self, owner_email: str):
        self.owner_email = owner_email
        super().__init__(f"linked to another Flowly account ({owner_email or 'unknown'})")


@dataclass(frozen=True)
class LinkState:
    #: The apps of ``owner_uid`` reach this agent through the Flowly server.
    linked: bool
    host_id: str
    owner_uid: str
    #: Masked (``h***@example.com``): enough to recognise, not to read off a screen.
    owner_email: str


def mask_email(email: str | None) -> str:
    """``hakan@example.com`` → ``h***@example.com``."""
    if not email or "@" not in email:
        return ""
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"


def link_state() -> LinkState:
    """Whether this agent is linked, and to whom.

    A link made before the owner was kept names none; Desktop always signed in
    and connected as one account, so the account in the keychain is its owner.
    """
    channel = load_config().channels.flowly_server
    linked = bool(channel.enabled and channel.agent_secret and channel.agent_host_id)
    owner_uid, owner_email = channel.owner_uid, channel.owner_email
    if linked and not owner_uid:
        from flowly.account.auth import load_account_sync

        signed_in = load_account_sync()
        if signed_in is not None:
            owner_uid, owner_email = signed_in.user_id or "", signed_in.email or ""
    return LinkState(linked=linked, host_id=channel.agent_host_id, owner_uid=owner_uid or "",
                     owner_email=mask_email(owner_email))


def assert_may_sign_in(user_id: str) -> None:
    """Refuse a sign-in as anyone but the owner while the agent is linked.

    Otherwise one account's apps would reach the agent while another's credits
    paid for it, and its memory would answer the second person.
    """
    state = link_state()
    if state.linked and state.owner_uid and user_id and state.owner_uid != user_id:
        audit_log.info("home.signin.refused", host=state.host_id)
        raise HomeLinkedElsewhere(state.owner_email)


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


async def link_this_machine(
    account, *, server_url: str = "", name: str = "", host_id: str = "", adopt: bool = True,
) -> HomeRegistration:
    """Register this machine with the Flowly server as ``account`` and wire the channel.

    The one way an agent becomes reachable from the Flowly apps, for
    ``flowly server connect`` and ``flowly login`` alike. The entry this
    machine already has is asked for only when it is this account's (another
    account only ever got NOT_FOUND for it); else the one named, else (with
    ``adopt``) its relay-era entry found by fingerprint, so its conversations
    carry over, else a new one. Raises ``HomeLinkedElsewhere`` while the agent
    is linked to another account, and ``HomeRegistrationError`` when the
    server refuses.
    """
    from flowly.account.fingerprint import machine_id, machine_name

    user_id = getattr(account, "user_id", "") or ""
    assert_may_sign_in(user_id)
    channel = load_config().channels.flowly_server
    url = server_url or channel.server_url
    own_entry = channel.agent_host_id if channel.owner_uid in ("", user_id) else ""
    target = own_entry or host_id or None
    registration = await register_home(
        account.id_token, server_url=url, name=name or machine_name(), host_id=target,
        adopt_machine_id=machine_id() if adopt and not target else None,
    )
    wire_flowly_server(registration, url, owner_uid=user_id, owner_email=getattr(account, "email", "") or "")
    return registration
