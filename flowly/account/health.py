"""State inspection helpers — single source of truth for "is this install healthy?"

Used by:
  * ``flowly login`` — gap detection on the "already signed in" path,
    pre-flight check before ``--repair`` short-circuits.
  * ``flowly onboard`` — token-state check when deciding whether an
    existing sign-in can be reused.

Every function in this module is **side-effect-free**. They read
``~/.flowly/config.json`` and the keychain / fallback credentials file,
never write. That guarantee is load-bearing: these checks must be safe
to run against any install without surprising the user. Mutation lives
in ``relay_config.py``, ``token_store.py``, ``active_provider.py``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass


# ── Flowly server channel state ─────────────────────────────────────


@dataclass
class FlowlyServerState:
    """Snapshot of ``channels.flowlyServer`` (server/PROTOCOL.md B).

    When enabled, this channel replaces the relay as the ``web`` channel
    (``flowly/channels/manager.py``). Its secret and host id may live only in
    the gateway's environment (the cloud image), so ``missing`` lists the
    fields neither the config nor this process's environment holds.
    """
    enabled: bool
    healthy: bool
    reason: str = ""              # populated only when healthy is False
    agent_host_id: str = ""
    server_url: str = ""
    missing: tuple[str, ...] = ()


def flowly_server_state(server, environ=None) -> FlowlyServerState:
    """State of a ``FlowlyServerChannelConfig``, with the channel's own env fallback."""
    from flowly.config.schema import FLOWLY_SERVER_URL_PREFIXES

    if not server.enabled:
        return FlowlyServerState(enabled=False, healthy=False,
                                 reason="channels.flowlyServer.enabled is false")
    url, secret, host = server.connection_settings(environ)
    missing = tuple(name for name, value in (("serverUrl", url), ("agentSecret", secret),
                                             ("agentHostId", host)) if not value)
    if missing:
        return FlowlyServerState(
            enabled=True, healthy=False, agent_host_id=host, server_url=url, missing=missing,
            reason=f"channels.flowlyServer is missing {', '.join(missing)} "
                   "(config or FLOWLY_SERVER_URL / FLOWLY_AGENT_SECRET / FLOWLY_AGENT_HOST_ID)",
        )
    if not url.startswith(FLOWLY_SERVER_URL_PREFIXES):
        return FlowlyServerState(enabled=True, healthy=False, agent_host_id=host, server_url=url,
                                 reason="channels.flowlyServer.serverUrl must use wss://")
    return FlowlyServerState(enabled=True, healthy=True, agent_host_id=host, server_url=url)


def check_flowly_server_state(environ=None) -> FlowlyServerState:
    """The Flowly server channel of ``~/.flowly/config.json``."""
    try:
        from flowly.config.loader import load_config
        server = load_config().channels.flowly_server
    except Exception as exc:  # noqa: BLE001
        return FlowlyServerState(enabled=False, healthy=False, reason=f"config unreadable: {exc}")
    return flowly_server_state(server, environ)


# ── Relay state ─────────────────────────────────────────────────────


@dataclass
class RelayState:
    """Snapshot of the remote channel from the gateway's perspective.

    ``via`` says which: ``"relay"`` (``channels.web``) or ``"flowlyServer"``
    when ``channels.flowlyServer`` is enabled and replaces the relay; then
    the fields describe that channel (``server_id`` is its agent host id).
    """
    healthy: bool
    reason: str = ""              # populated only when healthy is False
    server_id: str = ""
    enabled: bool = False
    via: str = "relay"


def check_relay_state() -> RelayState:
    """Healthy iff every field the gateway needs to dial the relay is set.

    Order of checks matches "first thing the WebSocket dial would
    notice broken" so the gap message points at the root cause. A
    missing ``relay_url`` is just as fatal as a missing
    ``auth_token`` — both cause the dial to fail silently.

    With the Flowly server channel enabled the relay is not used, so its
    fields do not matter: the server channel's state is reported instead.
    """
    try:
        from flowly.config.loader import load_config
        channels = load_config().channels
        web = channels.web
    except Exception as exc:  # noqa: BLE001
        return RelayState(healthy=False, reason=f"config unreadable: {exc}")

    server = flowly_server_state(channels.flowly_server)
    if server.enabled:
        return RelayState(healthy=server.healthy, reason=server.reason,
                          server_id=server.agent_host_id, enabled=True, via="flowlyServer")
    if not web.enabled:
        return RelayState(healthy=False, reason="channels.web.enabled is false",
                          server_id=web.server_id, enabled=False)
    if not web.server_id:
        return RelayState(healthy=False, reason="channels.web.server_id is empty",
                          enabled=True)
    if not web.auth_token:
        return RelayState(healthy=False, reason="channels.web.auth_token is empty",
                          server_id=web.server_id, enabled=True)
    if not (web.relay_url or "").strip():
        return RelayState(healthy=False, reason="channels.web.relay_url is empty",
                          server_id=web.server_id, enabled=True)
    return RelayState(healthy=True, server_id=web.server_id, enabled=True)


# ── Active provider state ───────────────────────────────────────────


def check_active_provider() -> tuple[bool, str]:
    """Return ``(is_set, slug)``. ``slug`` is empty when no explicit default.

    Note: empty ``providers.active`` is not automatically "broken" — the
    resolver still falls back to the BYOK cascade. Use this in tandem
    with corruption checks for a full picture.
    """
    try:
        from flowly.config.loader import load_config
        slug = (load_config().providers.active or "").strip()
        return bool(slug), slug
    except Exception:
        return False, ""


# ── Cross-slot corruption detection ─────────────────────────────────


# Flowly hosted bearer is exactly ``{serverId}:{gatewayAuthToken}`` where
# serverId is a Firestore-style alphanumeric/underscore/dash id (15-30
# chars in practice) and gatewayAuthToken is a 32-128 char hex string.
# Real provider keys never match this shape — OpenRouter is
# ``sk-or-v1-<hex>``, Anthropic ``sk-ant-<base64>``, OpenAI ``sk-<base64>``,
# Groq ``gsk_<base64>``. Hence the regex is a high-confidence signal that
# a Flowly bearer leaked into a BYOK slot (legacy desktop bug, partial
# restore, manual copy-paste mistake).
_FLOWLY_BEARER_RE = re.compile(r"^[A-Za-z0-9_-]{12,32}:[0-9a-f]{32,128}$")

# Slot keys we care about — provider entries in the ``providers`` block
# that take an external API key. ``flowly`` is the hosted slot itself,
# so we deliberately exclude it; a bearer in ``providers.flowly`` is
# correct, not corruption.
_BYOK_SLOTS = (
    "openrouter", "anthropic", "openai", "xai", "gemini", "groq", "kimi",
)

# Substring that flags a BYOK slot pointing at the Flowly proxy URL.
# Matches "useflowlyapp.com" anywhere in api_base. Same provenance as
# the bearer leak — legacy desktop wrote the proxy URL into the
# OpenRouter slot when the cascade was unified.
_FLOWLY_PROXY_HINT = "useflowlyapp.com"


@dataclass
class ProviderCorruption:
    slot: str
    issue: str                 # human-readable description
    field: str                 # "api_key" or "api_base"


def check_provider_corruption() -> list[ProviderCorruption]:
    """Detect cross-slot leaks: Flowly hosted credentials in BYOK slots.

    Returns a list of detected issues — empty when clean. Read-only;
    surfacing the corruption to the user is the caller's job.

    The two patterns we catch:

      1. ``providers.<byok>.api_key`` matches the Flowly bearer regex
         (``serverId:hexToken``). Real BYOK keys never have a colon
         followed by 32+ hex chars.

      2. ``providers.<byok>.api_base`` contains ``useflowlyapp.com``.
         Real BYOK bases point at the provider's own host
         (``openrouter.ai``, ``api.anthropic.com``, etc.).

    Either pattern means the slot will silently mis-route requests
    OR auth with the wrong bearer. The resolver works around it at
    runtime by ignoring saved ``api_base`` (see active_provider.py:135),
    but the data on disk is wrong and confusing to inspect.
    """
    issues: list[ProviderCorruption] = []
    try:
        from flowly.config.loader import load_config
        providers = load_config().providers
    except Exception:
        return issues  # can't inspect — fail closed (no false positives)

    for slot in _BYOK_SLOTS:
        cfg = getattr(providers, slot, None)
        if cfg is None:
            continue
        api_key = (getattr(cfg, "api_key", "") or "").strip()
        api_base = (getattr(cfg, "api_base", "") or "").strip()
        if api_key and _FLOWLY_BEARER_RE.match(api_key):
            issues.append(ProviderCorruption(
                slot=slot,
                field="api_key",
                issue="Flowly hosted bearer (serverId:token format) — "
                      "real BYOK keys never use this shape",
            ))
        if api_base and _FLOWLY_PROXY_HINT in api_base.lower():
            issues.append(ProviderCorruption(
                slot=slot,
                field="api_base",
                issue=f"points at the Flowly proxy ({_FLOWLY_PROXY_HINT}) "
                      "instead of the provider's canonical host",
            ))
    return issues


# ── Token freshness ─────────────────────────────────────────────────


@dataclass
class TokenState:
    has_account: bool
    healthy: bool                  # True when account exists AND not expired
    seconds_left: int = 0          # 0 when no account or already expired
    email: str = ""
    user_id: str = ""


def check_token_state() -> TokenState:
    """Read account from keychain / fallback file, report expiry."""
    try:
        from flowly.account.auth import load_account_sync
        account = load_account_sync()
    except Exception:
        return TokenState(has_account=False, healthy=False)
    if account is None:
        return TokenState(has_account=False, healthy=False)
    secs_left = max(0, int(account.expires_at - time.time()))
    return TokenState(
        has_account=True,
        healthy=secs_left > 0,
        seconds_left=secs_left,
        email=account.email or "",
        user_id=account.user_id or "",
    )
