"""A self-hosted Core's home on the Flowly server (server/PROTOCOL.md B6).

The account's ID token buys this machine its own agent secret; the config then
dials the Flowly server instead of the relay, and turning it off returns to
the relay untouched.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from flowly.account import home_registration
from flowly.account.home_registration import (
    HomeRegistration,
    HomeRegistrationError,
    http_origin,
    register_home,
    unwire_flowly_server,
    wire_flowly_server,
)
from flowly.config.loader import load_config, save_config

URL = "wss://cloud.useflowlyapp.com/v1/agent"


@pytest.fixture(autouse=True)
def fresh_channels():
    """Each test starts with neither the Flowly server channel nor the relay set."""
    from flowly.config.schema import FlowlyServerChannelConfig, WebChannelConfig

    config = load_config()
    config.channels.flowly_server = FlowlyServerChannelConfig()
    config.channels.web = WebChannelConfig()
    save_config(config)
    yield


def test_the_agent_url_gives_the_https_origin_and_plain_ws_only_to_this_machine():
    assert http_origin(URL) == "https://cloud.useflowlyapp.com"
    assert http_origin("ws://127.0.0.1:8787/v1/agent") == "http://127.0.0.1:8787"
    with pytest.raises(HomeRegistrationError) as refused:
        http_origin("ws://cloud.useflowlyapp.com/v1/agent")
    assert refused.value.code == "INVALID_SERVER_URL"


async def test_registration_sends_the_token_name_and_hosting_and_returns_the_secret(monkeypatch):
    seen = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"agentHostId": "h1", "agentSecret": "s" * 43, "created": True})

    monkeypatch.setattr(home_registration, "hosting", lambda: "mac")
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        result = await register_home("id-token", server_url=URL, name="Hakan Mac", host_id="old", client=client)
    assert result == HomeRegistration(agent_host_id="h1", agent_secret="s" * 43, created=True)
    assert seen == {
        "url": "https://cloud.useflowlyapp.com/v1/homes/register",
        "auth": "Bearer id-token",
        "body": {"name": "Hakan Mac", "hosting": "mac", "hostId": "old"},
    }


@pytest.mark.parametrize("status, body, code", [
    (409, {"error": "CLOUD_HOME"}, "CLOUD_HOME"),
    (401, {"error": "AUTH_EXPIRED"}, "AUTH_EXPIRED"),
    (502, "not json", "HTTP_502"),
    (201, {"agentHostId": "h1"}, "MALFORMED"),
])
async def test_refusals_carry_the_server_code(status, body, code):
    def answer(request):
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        with pytest.raises(HomeRegistrationError) as refused:
            await register_home("t", server_url=URL, name="x", client=client)
    assert refused.value.code == code


def test_wiring_turns_the_server_channel_on_and_unwiring_gives_the_relay_back():
    config = load_config()
    config.channels.web.enabled = True
    config.channels.web.server_id = "relay-id"
    config.channels.web.auth_token = "relay-token"
    save_config(config)

    wire_flowly_server(HomeRegistration("h1", "secret-1", True), URL)
    channel = load_config().channels.flowly_server
    assert (channel.enabled, channel.server_url, channel.agent_secret, channel.agent_host_id) == (True, URL, "secret-1", "h1")
    assert load_config().channels.web.auth_token == "relay-token", "the relay settings stay"

    from flowly.account.health import check_relay_state
    assert check_relay_state().via == "flowlyServer"

    assert unwire_flowly_server() is True
    channel = load_config().channels.flowly_server
    assert (channel.enabled, channel.agent_secret, channel.agent_host_id) == (False, "", "h1")
    assert check_relay_state().via == "relay"
    assert unwire_flowly_server() is False


class _Account:
    id_token = "id-token"
    server_id = "relay-era-id"


@pytest.mark.parametrize("args, existing, expected_host", [
    ([], "", None),                       # a new home
    (["--adopt"], "", "relay-era-id"),    # this machine's relay-era entry, with its conversations
    (["--adopt"], "h-known", "h-known"),  # a home it already has always wins
])
def test_connect_picks_the_home_to_register(monkeypatch, args, existing, expected_host):
    from flowly.cli import server_cmd

    if existing:
        config = load_config()
        config.channels.flowly_server.agent_host_id = existing
        save_config(config)
    calls = []

    async def account():
        return _Account()

    async def register(token, *, server_url, name, host_id=None):
        calls.append((token, server_url, name, host_id))
        return HomeRegistration(host_id or "h-new", "secret", host_id is None)

    monkeypatch.setattr("flowly.account.auth.load_account_refreshing", account)
    monkeypatch.setattr("flowly.account.home_registration.register_home", register)
    result = CliRunner().invoke(server_cmd.server_app, ["connect", "--name", "Test Mac", *args])
    assert result.exit_code == 0, result.output
    assert calls == [("id-token", URL, "Test Mac", expected_host)]
    assert load_config().channels.flowly_server.agent_host_id == (expected_host or "h-new")


def test_connect_needs_a_signed_in_account(monkeypatch):
    from flowly.cli import server_cmd

    async def nobody():
        return None

    monkeypatch.setattr("flowly.account.auth.load_account_refreshing", nobody)
    result = CliRunner().invoke(server_cmd.server_app, ["connect"])
    assert result.exit_code == 1 and "flowly login" in result.output
