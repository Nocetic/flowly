"""Wire registered-server credentials into the local gateway's web channel.

The Flowly Python gateway already knows how to connect to the cloud relay
(``flowly/channels/web.py``). All it needs is the credentials in
``~/.flowly/config.json`` → ``channels.web``. Once those are present and
``enabled=true``, the gateway dials ``wss://relay.useflowlyapp.com/relay``
on next startup, and conversations flow through the relay → Firestore
pipeline that desktop already uses.

This module is the bridge between the login flow (which obtains the
credentials) and the on-disk gateway config (which the gateway reads on
start).
"""

from __future__ import annotations

from dataclasses import dataclass

from flowly.account import audit_log
from flowly.account.server import RegisteredServer
from flowly.config.loader import load_config, save_config
from flowly.config.schema import WebChannelConfig

# Canonical relay endpoint. Repair / wire paths backfill this when
# ``channels.web.relay_url`` is empty (fresh config, manual edit, …).
#
# NB: this used to read the schema field default — but that default IS the
# empty string ("self-host: leave empty"), so the "backfill" wrote "" and the
# web channel silently failed to connect (web.py builds the dial URL straight
# from relay_url, no runtime fallback). Prefer the schema default only when
# it's actually non-empty, else the hardcoded canonical URL.
_CANONICAL_RELAY_URL = "wss://relay.useflowlyapp.com/relay"
_DEFAULT_RELAY_URL = (
    (WebChannelConfig.model_fields["relay_url"].default or "").strip()
    or _CANONICAL_RELAY_URL
)


@dataclass
class WebChannelChange:
    """Diff between previous web channel config and the one we just wrote."""
    enabled_was: bool
    server_id_was: str
    changed: bool   # True if any field actually changed
    needs_gateway_restart: bool  # True if a running gateway must restart to pick up


def wire_relay_credentials(server: RegisteredServer) -> WebChannelChange:
    """Persist relay credentials from a freshly-registered server.

    Idempotent: writing the same credentials twice is a no-op (no
    needless gateway restart). Returns a diff so the caller can decide
    whether to prompt for a restart.
    """
    cfg = load_config()
    web = cfg.channels.web

    prev_enabled = web.enabled
    prev_server_id = web.server_id

    # When ``relay_url`` is empty (user deleted the field manually,
    # config was constructed by hand, partial restore from backup, …)
    # backfill the canonical default. Without this we'd happily leave
    # the gateway with ``relay_url=""`` after a "successful" repair
    # and the web channel would silently fail to connect.
    needs_relay_url_backfill = not (web.relay_url or "").strip()

    changed = (
        web.enabled is not True
        or web.server_id != server.server_id
        or web.auth_token != server.gateway_auth_token
        or (server.jwt_secret and web.jwt_secret != server.jwt_secret)
        or needs_relay_url_backfill
    )

    if not changed:
        audit_log.info("relay.config.unchanged", server_id=server.server_id)
        return WebChannelChange(
            enabled_was=prev_enabled,
            server_id_was=prev_server_id,
            changed=False,
            needs_gateway_restart=False,
        )

    web.enabled = True
    web.server_id = server.server_id
    web.auth_token = server.gateway_auth_token
    if server.jwt_secret:
        web.jwt_secret = server.jwt_secret
    if needs_relay_url_backfill:
        # Only touch the field when it was missing — a user who set a
        # custom ``relay_url`` (self-hosted relay, regional endpoint)
        # keeps that value across logins.
        web.relay_url = _DEFAULT_RELAY_URL

    save_config(cfg)
    audit_log.info(
        "relay.config.wired",
        server_id=server.server_id,
        previously_enabled=prev_enabled,
        previous_server_id=prev_server_id,
        auth_token=audit_log.safe_token_summary(server.gateway_auth_token),
    )

    # Restart needed if the gateway was already running with different
    # credentials. Detecting "running" reliably is out of scope here —
    # the TUI does it via the /health probe and surfaces the message.
    return WebChannelChange(
        enabled_was=prev_enabled,
        server_id_was=prev_server_id,
        changed=True,
        needs_gateway_restart=True,
    )


def clear_relay_credentials() -> None:
    """Disable the web channel — called from /logout."""
    cfg = load_config()
    web = cfg.channels.web
    if not (web.enabled or web.server_id or web.auth_token):
        return
    web.enabled = False
    web.server_id = ""
    web.auth_token = ""
    web.jwt_secret = ""
    save_config(cfg)
    audit_log.info("relay.config.cleared")
