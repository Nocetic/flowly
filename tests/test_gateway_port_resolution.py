"""`flowly gateway` port resolution: --port wins, else config.gateway.port.

The CLI used to hardcode 18790 while the desktop's local-bot health probe
reads gateway.port from config — a configured port that drifted from the
default made an externally-started gateway invisible to the dashboard.
"""

from __future__ import annotations

from types import SimpleNamespace

from flowly.cli.gateway_cmd import resolve_gateway_port


def _config(port: int) -> SimpleNamespace:
    return SimpleNamespace(gateway=SimpleNamespace(port=port))


def test_unset_cli_port_falls_back_to_config():
    assert resolve_gateway_port(0, _config(18650)) == 18650


def test_explicit_cli_port_wins_over_config():
    assert resolve_gateway_port(19000, _config(18650)) == 19000


def test_default_config_resolves_to_legacy_default():
    # A fresh config carries the schema default (18790) — behaviour for
    # untouched installs is byte-for-byte what the old hardcoded flag did.
    from flowly.config.schema import GatewayConfig
    assert resolve_gateway_port(0, SimpleNamespace(gateway=GatewayConfig())) == 18790
