"""Write Core's config for a Flowly cloud machine from its secrets, at every boot.

Run by flowly-cloud-setup as root before the gateway starts. Only the fields a
cloud machine owns are written; everything the user or Core changed stays.
Never prints a secret.

Environment (from Fly secrets / machine env, see cloud/image/README.md):
  FLOWLY_ACCOUNT_KEY    Flowly account key (flw_...): model access through the
                        Flowly proxy, no interactive `flowly login`.
  FLOWLY_GATEWAY_TOKEN  Static token for the gateway behind Fly's edge (screen
                        sockets, direct clients). Generated once if unset.
  FLOWLY_AGENT_SECRET   Present: enable the Flowly server channel
                        (channels.flowlyServer). Core reads the secret itself,
                        with FLOWLY_SERVER_URL and FLOWLY_AGENT_HOST_ID, from the
                        gateway's environment; they are not written to disk.
"""

from __future__ import annotations

import os
import subprocess
import sys

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 18790


def apply(config, environ) -> list[str]:
    """Mutate ``config`` for this machine; return the names of what was set."""
    from flowly.gateway.auth import generate_gateway_token

    changed = []
    config.gateway.host, config.gateway.port = GATEWAY_HOST, GATEWAY_PORT
    # Every internet client reaches the gateway through the loopback edge
    # relay, so loopback must not mean trusted here.
    config.gateway.require_auth = True
    token = environ.get("FLOWLY_GATEWAY_TOKEN", "").strip()
    if token:
        if config.gateway.token != token:
            config.gateway.token = token
            changed.append("gateway.token")
    elif not (config.gateway.token or "").strip():
        # Never run the gateway token-less behind a public edge.
        config.gateway.token = generate_gateway_token()
        changed.append("gateway.token (generated)")

    key = environ.get("FLOWLY_ACCOUNT_KEY", "").strip()
    if key:
        flowly = config.providers.flowly
        if not flowly.enabled or flowly.account_key != key:
            flowly.enabled, flowly.account_key = True, key
            changed.append("providers.flowly")

    if environ.get("FLOWLY_AGENT_SECRET", "").strip():
        server = getattr(config.channels, "flowly_server", None)
        if server is None:
            print("flowly-cloud-config: this Core has no channels.flowlyServer; "
                  "the agent will not dial the Flowly server", file=sys.stderr)
        elif not server.enabled:
            server.enabled = True
            changed.append("channels.flowlyServer.enabled")
    return changed


def main() -> int:
    from flowly.config.loader import load_config, save_config
    from flowly.integrations.active_provider import resolve_active_provider

    config = load_config()
    changed = apply(config, os.environ)
    save_config(config)
    print("flowly-cloud-config: " + (", ".join(changed) if changed else "config unchanged"))
    if resolve_active_provider(config) is None:
        print("flowly-cloud-config: no model provider (set FLOWLY_ACCOUNT_KEY); the gateway will not start",
              file=sys.stderr)
    # Workspace templates; never overwrites the user's files.
    subprocess.run(["/usr/local/bin/flowly", "bootstrap"], stdout=subprocess.DEVNULL, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
