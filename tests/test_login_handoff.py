"""Desktop signs the agent it carries in with a handed-over token (no browser)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import flowly.account.auth as auth
import flowly.cli.login_cmd as login_cmd
from flowly.account.account_key import AccountKeyChange
from flowly.account.firebase_rest import TokenBundle


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-home"))
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *a, **k: None)


def _app():
    import typer

    app = typer.Typer()
    app.command()(login_cmd.login)
    return app


@pytest.mark.asyncio
async def test_handoff_signs_in_with_the_token_and_saves_the_account(monkeypatch):
    seen: dict = {}

    async def _sign_in(token):
        seen["token"] = token
        return TokenBundle(id_token="id-1", refresh_token="refresh-1", expires_at=9e9, user_id="uid-1", email="")

    async def _lookup(id_token):
        return {"email": "owner@example.com"}

    monkeypatch.setattr(auth, "sign_in_with_custom_token", _sign_in)
    monkeypatch.setattr(auth, "lookup_account", _lookup)
    monkeypatch.setattr(auth, "save_account", lambda account: seen.setdefault("saved", account))

    account = await auth.sign_in_with_handoff("  custom-token\n")

    assert seen["token"] == "custom-token"
    assert seen["saved"] is account
    assert (account.user_id, account.email, account.refresh_token) == ("uid-1", "owner@example.com", "refresh-1")


@pytest.mark.asyncio
async def test_the_uid_comes_from_the_profile_when_the_exchange_has_none(monkeypatch):
    # The live /api/auth/exchange answers user_id "" (Google's
    # signInWithCustomToken has no localId); without a uid the account key is
    # never issued and the gateway has no provider (2026-11-01).
    async def _sign_in(token):
        return TokenBundle(id_token="id-1", refresh_token="refresh-1", expires_at=9e9, user_id="", email=None)

    async def _lookup(id_token):
        return {"email": "owner@example.com", "user_id": "uid-1"}

    monkeypatch.setattr(auth, "sign_in_with_custom_token", _sign_in)
    monkeypatch.setattr(auth, "lookup_account", _lookup)
    monkeypatch.setattr(auth, "save_account", lambda account: None)

    account = await auth.sign_in_with_handoff("custom-token")

    assert account.user_id == "uid-1"


@pytest.mark.asyncio
async def test_a_refresh_gives_an_account_saved_without_a_uid_its_uid(monkeypatch):
    saved = auth.Account(user_id="", email="owner@example.com", id_token="old", refresh_token="refresh-1",
                         expires_at=0, machine_id="m", machine_name="Mac")
    stored: dict = {}

    async def _refresh(refresh_token):
        return TokenBundle(id_token="id-2", refresh_token="refresh-2", expires_at=9e9, user_id="uid-1", email=None)

    monkeypatch.setattr(auth, "load_account_sync", lambda: saved)
    monkeypatch.setattr(auth, "refresh_id_token", _refresh)
    monkeypatch.setattr(auth, "save_account", lambda account: stored.setdefault("account", account))

    account = await auth.load_account_refreshing()

    assert account.user_id == "uid-1" and stored["account"].user_id == "uid-1"


@pytest.mark.asyncio
async def test_an_empty_handoff_token_is_refused_before_any_request(monkeypatch):
    async def _never(token):
        raise AssertionError("no request without a token")

    monkeypatch.setattr(auth, "sign_in_with_custom_token", _never)
    with pytest.raises(auth.FirebaseAuthError):
        await auth.sign_in_with_handoff("   ")


def _stub_login(monkeypatch, change: AccountKeyChange, calls: dict):
    async def _handoff(token):
        calls["token"] = token
        return SimpleNamespace(user_id="uid-1", email="owner@example.com")

    monkeypatch.setattr("flowly.account.auth.sign_in_with_handoff", _handoff)
    monkeypatch.setattr(login_cmd, "_provision_account_key", lambda account: change)
    monkeypatch.setattr(login_cmd, "_apply_runtime_changes", lambda **kw: calls.setdefault("runtime", kw) and True)
    monkeypatch.setattr(login_cmd, "_print_provider_verdict", lambda: False)


def test_token_stdin_reads_the_token_from_stdin_and_issues_the_key(monkeypatch):
    calls: dict = {}
    _stub_login(monkeypatch, AccountKeyChange(ready=True, changed=True), calls)

    result = CliRunner().invoke(_app(), ["--token-stdin"], input="custom-token\n")

    assert result.exit_code == 0, result.output
    assert calls["token"] == "custom-token\n"
    assert calls["runtime"]["provider_reload_required"] is True
    assert calls["runtime"]["relay_restart_required"] is False


def test_token_stdin_fails_with_2_when_the_key_did_not_land(monkeypatch):
    calls: dict = {}
    _stub_login(monkeypatch, AccountKeyChange(ready=False, changed=False), calls)

    result = CliRunner().invoke(_app(), ["--token-stdin"], input="custom-token")

    assert result.exit_code == 2


def test_token_stdin_fails_with_1_when_sign_in_fails(monkeypatch):
    async def _handoff(token):
        raise auth.FirebaseAuthError("INVALID_CUSTOM_TOKEN")

    monkeypatch.setattr("flowly.account.auth.sign_in_with_handoff", _handoff)
    monkeypatch.setattr(login_cmd, "_provision_account_key", lambda account: pytest.fail("no key without sign-in"))

    result = CliRunner().invoke(_app(), ["--token-stdin"], input="bad")

    assert result.exit_code == 1
