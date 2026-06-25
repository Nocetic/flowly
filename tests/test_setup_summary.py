"""Tests for the setup summary (flowly/cli/setup_summary.py).

Pure, hermetic: provider + gateway are injected so we never touch the real
~/.flowly account or shell out to the service helpers.
"""

from __future__ import annotations

import io

from rich.console import Console

from flowly.cli.setup_summary import (
    SetupSummary,
    _next_commands,
    collect_summary,
    render_summary,
)
from flowly.config.schema import Config
from flowly.integrations.active_provider import ActiveProvider


def _provider(key: str = "anthropic") -> ActiveProvider:
    return ActiveProvider(key=key, api_key="x", api_base=None, source=f"{key} key")


def _summary(config: Config, *, provider=None) -> SetupSummary:
    # Always inject gateway + provider so the result is a pure function of config.
    return collect_summary(
        config, provider=provider, gateway_installed=False, gateway_running=False
    )


def test_empty_config_has_no_provider_or_channels():
    s = _summary(Config(), provider=None)
    assert s.provider_ready is False
    assert s.provider_key is None
    assert s.configured_channels == []
    assert s.configured_tools == []


def test_missing_optional_includes_browser_and_mcp_on_empty():
    s = _summary(Config(), provider=None)
    assert "Browser extension" in s.missing
    assert "MCP servers" in s.missing


def test_injected_provider_and_model():
    c = Config()
    s = _summary(c, provider=_provider("anthropic"))
    assert s.provider_ready is True
    assert s.provider_key == "anthropic"
    # Model comes from config.agents.defaults.model (schema default here).
    assert s.model == "moonshotai/kimi-k2.5"


def test_enabled_channel_is_configured():
    c = Config()
    c.channels.telegram.enabled = True
    c.channels.telegram.token = "123:abc"
    s = _summary(c, provider=_provider())
    labels = [i.label for i in s.configured_channels]
    assert labels == ["Telegram"]
    assert s.channels  # full list still present


def test_whatsapp_schema_default_is_not_a_false_positive():
    """Regression: WhatsApp bridge_url defaults to a non-empty URL — that must
    NOT read as user-configured."""
    c = Config()  # untouched whatsapp: enabled=False, bridge_url at schema default
    s = _summary(c, provider=_provider())
    wa = next(i for i in s.channels if i.label == "WhatsApp")
    assert wa.configured is False


def test_credential_set_but_disabled_shows_detail():
    c = Config()
    c.channels.telegram.enabled = False
    c.channels.telegram.token = "123:abc"  # user pasted a token but left it off
    s = _summary(c, provider=_provider())
    tg = next(i for i in s.channels if i.label == "Telegram")
    assert tg.configured is True
    assert tg.detail == "set · disabled"


def test_tool_credentials_mark_configured():
    c = Config()
    c.integrations.trello.api_key = "k"
    c.integrations.trello.token = "t"
    s = _summary(c, provider=_provider())
    assert "Trello" in [i.label for i in s.configured_tools]


def test_gateway_state_is_reflected():
    c = Config()
    s = collect_summary(c, provider=_provider(), gateway_installed=True, gateway_running=True)
    assert s.gateway_installed is True
    assert s.gateway_running is True


def test_next_commands_without_provider_points_to_setup():
    s = _summary(Config(), provider=None)
    cmds = [c for c, _ in _next_commands(s)]
    assert cmds == ["flowly setup"]


def test_next_commands_with_provider_and_no_gateway_offers_install():
    s = _summary(Config(), provider=_provider())
    cmds = [c for c, _ in _next_commands(s)]
    assert "flowly service install --start" in cmds
    assert "flowly" in cmds


def test_render_does_not_raise_and_prints_title():
    c = Config()
    c.channels.telegram.enabled = True
    c.channels.telegram.token = "123:abc"
    s = _summary(c, provider=_provider())
    buf = io.StringIO()
    render_summary(s, console=Console(file=buf, width=80, force_terminal=False))
    out = buf.getvalue()
    assert "Flowly" in out
    assert "Telegram" in out
    assert "anthropic" in out
