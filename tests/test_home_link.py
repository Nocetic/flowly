"""Which Flowly account this agent belongs to (2026-11-09).

A Mac's agent is reachable from one account's apps, and that account's
credits are the ones it spends. Signing it in as somebody else while it is
still linked would split the two: the apps of one account, the credits of
another, and the first person's memory now answering the second. So the
agent says which account it is linked to, and a sign-in as another account
is refused until the owner unlinks it.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import flowly.account.auth as auth
from flowly.account.firebase_rest import TokenBundle
from flowly.account.home_registration import (
    HomeLinkedElsewhere,
    HomeRegistration,
    assert_may_sign_in,
    link_state,
    mask_email,
    unwire_flowly_server,
    wire_flowly_server,
)
from flowly.config.loader import load_config, save_config

URL = "wss://cloud.useflowlyapp.com/v1/agent"


@pytest.fixture(autouse=True)
def fresh_link(monkeypatch):
    from flowly.config.schema import FlowlyServerChannelConfig

    config = load_config()
    config.channels.flowly_server = FlowlyServerChannelConfig()
    save_config(config)
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: None)
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *a, **k: None)
    yield


def _link(owner_uid="uid-a", owner_email="hakan@example.com"):
    wire_flowly_server(HomeRegistration("h-a", "secret-a", True), URL, owner_uid=owner_uid, owner_email=owner_email)


def test_the_link_names_its_owner_and_hides_most_of_the_address():
    assert link_state().linked is False
    _link()
    state = link_state()
    assert (state.linked, state.host_id, state.owner_uid) == (True, "h-a", "uid-a")
    assert state.owner_email == "h***@example.com"
    assert mask_email("") == "" and mask_email("x@y.co") == "x***@y.co"


def test_signing_in_as_the_owner_is_fine_and_as_anyone_else_is_refused():
    _link()
    assert_may_sign_in("uid-a")
    with pytest.raises(HomeLinkedElsewhere) as refused:
        assert_may_sign_in("uid-b")
    assert refused.value.owner_email == "h***@example.com"


def test_a_link_made_before_owners_were_kept_belongs_to_the_signed_in_account(monkeypatch):
    # Desktop always signed in and connected as the same account, so the
    # account in the keychain is the one the server entry was made with.
    config = load_config()
    channel = config.channels.flowly_server
    channel.enabled, channel.agent_secret, channel.agent_host_id = True, "s", "h-old"
    save_config(config)

    class Signed:
        user_id, email = "uid-a", "hakan@example.com"

    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: Signed())
    assert (link_state().owner_uid, link_state().owner_email) == ("uid-a", "h***@example.com")
    with pytest.raises(HomeLinkedElsewhere):
        assert_may_sign_in("uid-b")


def test_once_unlinked_anyone_may_sign_in():
    _link()
    unwire_flowly_server()
    assert link_state().linked is False
    assert_may_sign_in("uid-b")


@pytest.mark.asyncio
async def test_a_handoff_for_another_account_saves_nothing(monkeypatch):
    _link()

    async def sign_in(token):
        return TokenBundle(id_token="id-b", refresh_token="r", expires_at=9e9, user_id="uid-b", email="b@example.com")

    async def lookup(id_token):
        return {"user_id": "uid-b", "email": "b@example.com"}

    saved = []
    monkeypatch.setattr(auth, "sign_in_with_custom_token", sign_in)
    monkeypatch.setattr(auth, "lookup_account", lookup)
    monkeypatch.setattr(auth, "save_account", saved.append)
    with pytest.raises(HomeLinkedElsewhere):
        await auth.sign_in_with_handoff("token")
    assert saved == []


def test_the_cli_says_which_account_holds_the_agent(monkeypatch):
    import flowly.cli.login_cmd as login_cmd
    import typer

    _link()

    async def refuse(token):
        raise HomeLinkedElsewhere("h***@example.com")

    monkeypatch.setattr(auth, "sign_in_with_handoff", refuse)
    app = typer.Typer()
    app.command()(login_cmd.login)
    result = CliRunner().invoke(app, ["--token-stdin"], input="token")
    assert result.exit_code == 5, result.output
    assert "linked to another Flowly account (h***@example.com)" in result.output
    assert "flowly server disconnect" in result.output


class _Account:
    id_token = "id-token"
    server_id = ""

    def __init__(self, user_id, email):
        self.user_id, self.email = user_id, email


def test_connect_keeps_the_owner_and_status_reports_it(monkeypatch):
    from flowly.cli import server_cmd

    async def account():
        return _Account("uid-a", "hakan@example.com")

    async def register(token, *, server_url, name, host_id=None, adopt_machine_id=None):
        return HomeRegistration("h-a", "secret", True)

    monkeypatch.setattr("flowly.account.auth.load_account_refreshing", account)
    monkeypatch.setattr("flowly.account.home_registration.register_home", register)
    assert CliRunner().invoke(server_cmd.server_app, ["connect"]).exit_code == 0
    status = CliRunner().invoke(server_cmd.server_app, ["status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output) == {
        "linked": True, "hostId": "h-a", "ownerUid": "uid-a", "ownerEmail": "h***@example.com",
        "signedInUid": None,
    }


def test_connect_as_another_account_is_refused_before_asking_the_server(monkeypatch):
    from flowly.cli import server_cmd

    _link()
    calls = []

    async def account():
        return _Account("uid-b", "b@example.com")

    async def register(*args, **kwargs):
        calls.append(kwargs)
        return HomeRegistration("h-b", "secret", True)

    monkeypatch.setattr("flowly.account.auth.load_account_refreshing", account)
    monkeypatch.setattr("flowly.account.home_registration.register_home", register)
    result = CliRunner().invoke(server_cmd.server_app, ["connect"])
    assert result.exit_code == 5 and calls == []
    assert "linked to another Flowly account (h***@example.com)" in result.output


def test_after_unlinking_another_account_gets_a_home_of_its_own(monkeypatch):
    # The old entry is the first account's: asking for it as another one
    # only ever answered NOT_FOUND.
    from flowly.cli import server_cmd

    _link()
    unwire_flowly_server()
    calls = []

    async def account():
        return _Account("uid-b", "b@example.com")

    async def register(token, *, server_url, name, host_id=None, adopt_machine_id=None):
        calls.append(host_id)
        return HomeRegistration("h-b", "secret", True)

    monkeypatch.setattr("flowly.account.auth.load_account_refreshing", account)
    monkeypatch.setattr("flowly.account.home_registration.register_home", register)
    result = CliRunner().invoke(server_cmd.server_app, ["connect"])
    assert result.exit_code == 0, result.output
    assert calls == [None]
    assert link_state().owner_uid == "uid-b"
