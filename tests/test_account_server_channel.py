"""Health checks, doctor and login/logout know the Flowly server channel.

When ``channels.flowlyServer`` is enabled it replaces the relay
(flowly/channels/manager.py): the relay is not used, so it is never reported
as broken, login never registers or wires one, and the checks report the
Flowly server channel's own state instead.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

import flowly.cli.login_cmd as login_cmd
from flowly.account.account_key import AccountKeyChange
from flowly.account.health import check_flowly_server_state, check_relay_state
from flowly.account.runtime_apply import RuntimeApplyResult
from flowly.cli import logout_cmd
from flowly.config.loader import load_config, save_config
from flowly.config.schema import Config
from flowly.diagnostics.checks import CHECKS, check_channels, check_flowly_server, check_relay
from flowly.diagnostics.models import DoctorContext, Status

SERVER_ENV = ("FLOWLY_SERVER_URL", "FLOWLY_AGENT_SECRET", "FLOWLY_AGENT_HOST_ID")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-home"))
    for name in SERVER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *args, **kwargs: None)
    monkeypatch.setattr("flowly.account.audit_log.error", lambda *args, **kwargs: None)


def _server_config(*, secret: str = "agent-secret", host: str = "host-1", relay: bool = False) -> Config:
    config = Config()
    config.channels.flowly_server.enabled = True
    config.channels.flowly_server.agent_secret = secret
    config.channels.flowly_server.agent_host_id = host
    if relay:
        config.channels.web.enabled = True  # left over from the relay days, unused
    return config


# ── health ────────────────────────────────────────────────────────────────


def test_with_the_server_channel_the_relay_is_not_reported_broken():
    save_config(_server_config())
    state = check_relay_state()
    assert state.healthy is True and state.via == "flowlyServer"
    assert state.server_id == "host-1" and state.enabled is True


def test_an_incomplete_server_channel_is_reported_not_the_relay():
    save_config(_server_config(secret=""))
    state = check_relay_state()
    assert state.healthy is False and state.via == "flowlyServer"
    assert "agentSecret" in state.reason and "channels.web" not in state.reason
    server = check_flowly_server_state()
    assert server.enabled is True and server.healthy is False and server.missing == ("agentSecret",)


def test_server_credentials_may_come_from_the_environment(monkeypatch):
    save_config(_server_config(secret="", host=""))
    monkeypatch.setenv("FLOWLY_AGENT_SECRET", "from-env")
    monkeypatch.setenv("FLOWLY_AGENT_HOST_ID", "host-env")
    state = check_flowly_server_state()
    assert state.healthy is True and state.agent_host_id == "host-env"


def test_a_plain_server_url_to_another_machine_is_unhealthy():
    config = _server_config()
    config.channels.flowly_server.server_url = "ws://cloud.example.test/v1/agent"
    save_config(config)
    state = check_flowly_server_state()
    assert state.healthy is False and "wss://" in state.reason


def test_without_the_server_channel_the_relay_check_is_unchanged():
    save_config(Config())
    state = check_relay_state()
    assert state.via == "relay" and state.healthy is False
    assert state.reason == "channels.web.enabled is false"
    assert check_flowly_server_state().enabled is False


# ── doctor ────────────────────────────────────────────────────────────────


def _doctor(config: Config, tmp_path: Path) -> DoctorContext:
    return DoctorContext(config_path=tmp_path / "config.json", data_dir=tmp_path, config=config)


def test_doctor_skips_the_unused_relay_and_checks_the_server_channel(tmp_path):
    ctx = _doctor(_server_config(relay=True), tmp_path)
    check_relay(ctx)
    check_flowly_server(ctx)
    check_channels(ctx)
    by_name = {result.name: result for result in ctx.results}
    assert by_name["relay"].status == Status.SKIPPED
    assert by_name["flowly_server"].status == Status.OK
    assert all("web relay" not in result.detail for result in ctx.results)
    assert any(check.name == "flowly_server" for check in CHECKS)


def test_doctor_warns_when_the_server_credentials_are_not_visible(tmp_path):
    ctx = _doctor(_server_config(secret="", host=""), tmp_path)
    check_flowly_server(ctx)
    result = ctx.results[-1]
    assert result.status == Status.WARN
    assert "agentSecret" in result.message and "FLOWLY_AGENT_SECRET" in result.detail


def test_doctor_errors_on_an_insecure_server_url(tmp_path):
    config = _server_config()
    config.channels.flowly_server.server_url = "http://cloud.example.test"
    ctx = _doctor(config, tmp_path)
    check_flowly_server(ctx)
    assert ctx.results[-1].status == Status.ERROR


def test_doctor_skips_the_server_channel_when_it_is_off(tmp_path):
    ctx = _doctor(Config(), tmp_path)
    check_flowly_server(ctx)
    assert ctx.results[-1].status == Status.SKIPPED


# ── login ─────────────────────────────────────────────────────────────────


def _no_relay_registration(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def _register(_token):
        calls.append("register")
        raise AssertionError("no relay server may be registered")

    monkeypatch.setattr("flowly.account.server.register_machine", _register)
    monkeypatch.setattr("flowly.account.relay_config.wire_relay_credentials",
                        lambda srv: calls.append("wire"))
    return calls


def _applied(monkeypatch) -> list[tuple[bool, bool]]:
    applied: list[tuple[bool, bool]] = []
    monkeypatch.setattr(login_cmd, "_apply_runtime_changes", lambda **kwargs: applied.append(
        (kwargs["provider_reload_required"], kwargs["relay_restart_required"])) or True)
    return applied


def test_signed_in_with_the_server_channel_offers_no_relay(monkeypatch, capsys):
    save_config(_server_config())
    account = SimpleNamespace(user_id="uid", email="a@example.com", id_token="firebase")
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)
    monkeypatch.setattr(login_cmd, "_account_key_matches", lambda current: True)
    monkeypatch.setattr(login_cmd, "_check_provider_state", lambda: (True, "flowly"))
    monkeypatch.setattr(login_cmd, "_provision_account_key",
                        lambda current: AccountKeyChange(ready=True, changed=False))
    calls = _no_relay_registration(monkeypatch)
    applied = _applied(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        login_cmd.login(no_browser=True, repair=False, dry_run=False, key="", relay_opt=True)

    assert exc.value.exit_code == 0
    assert calls == [] and applied == [(False, False)]
    out = capsys.readouterr().out
    assert "Flowly server" in out and "host-1" in out


def test_signed_in_with_a_broken_server_channel_says_so(monkeypatch, capsys):
    save_config(_server_config(secret=""))
    account = SimpleNamespace(user_id="uid", email="a@example.com", id_token="firebase")
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)
    monkeypatch.setattr(login_cmd, "_account_key_matches", lambda current: True)
    monkeypatch.setattr(login_cmd, "_check_provider_state", lambda: (True, "flowly"))
    monkeypatch.setattr(login_cmd, "_provision_account_key",
                        lambda current: AccountKeyChange(ready=True, changed=False))
    calls = _no_relay_registration(monkeypatch)
    _applied(monkeypatch)

    with pytest.raises(typer.Exit):
        login_cmd.login(no_browser=True, repair=False, dry_run=False, key="", relay_opt=None)

    out = capsys.readouterr().out
    assert calls == []
    assert "agentSecret" in out and "relay" not in out.lower()


def test_repair_with_the_server_channel_never_wires_a_relay(monkeypatch, capsys):
    save_config(_server_config())
    account = SimpleNamespace(user_id="uid", email="a@example.com", id_token="firebase")

    async def _refresh():
        return account

    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)
    monkeypatch.setattr("flowly.account.auth.load_account_refreshing", _refresh)
    monkeypatch.setattr(login_cmd, "_check_provider_state", lambda: (True, "flowly"))
    monkeypatch.setattr(login_cmd, "_provision_account_key",
                        lambda current: AccountKeyChange(ready=True, changed=False))
    calls = _no_relay_registration(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        login_cmd.login(no_browser=True, repair=True, dry_run=False, key="", relay_opt=None)

    assert exc.value.exit_code == 3  # nothing to repair
    assert calls == []
    assert "relay config" not in capsys.readouterr().out.lower()


async def test_apply_repair_with_the_server_channel_only_sets_the_provider(monkeypatch):
    save_config(_server_config())
    calls = _no_relay_registration(monkeypatch)
    promoted: list[str] = []
    monkeypatch.setattr(login_cmd, "_check_provider_state", lambda: (False, ""))
    monkeypatch.setattr("flowly.integrations.active_provider.set_active_provider", promoted.append)

    result = await login_cmd._apply_repair(SimpleNamespace(id_token="firebase"), dry_run=False)

    assert calls == []
    assert result.relay_wired is False and result.needs_gateway_restart is False
    assert promoted == ["flowly"] and result.needs_provider_reload is True


def test_fresh_login_with_the_server_channel_registers_no_relay(monkeypatch, capsys):
    save_config(_server_config())
    account = SimpleNamespace(user_id="uid", email="a@example.com", id_token="firebase")

    async def _login_flow(**kwargs):
        return account

    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: None)
    monkeypatch.setattr("flowly.account.auth.run_login_flow", _login_flow)
    monkeypatch.setattr("flowly.account.auth.credential_storage_status", lambda: "test store")
    monkeypatch.setattr(login_cmd, "_provision_account_key",
                        lambda current: AccountKeyChange(ready=True, changed=True))
    monkeypatch.setattr(login_cmd, "_print_provider_verdict", lambda **kwargs: True)
    calls = _no_relay_registration(monkeypatch)
    applied = _applied(monkeypatch)

    # Non-interactive and no --relay/--no-relay: the relay default does not apply.
    login_cmd.login(no_browser=True, repair=False, dry_run=False, key="", relay_opt=None)

    assert calls == [] and applied == [(True, False)]
    assert "Flowly server" in capsys.readouterr().out


# ── logout ────────────────────────────────────────────────────────────────


def test_logout_leaves_the_machines_server_channel_on(monkeypatch, capsys):
    config = _server_config()
    config.providers.active = "flowly"
    save_config(config)
    account = SimpleNamespace(user_id="uid", email="a@example.com", id_token="firebase")
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)
    monkeypatch.setattr("flowly.account.auth.clear_account", lambda: None)
    monkeypatch.setattr("flowly.account.account_key.clear_account_key",
                        lambda current, revoke: SimpleNamespace(changed=False, revoked=None))
    applied: list[bool] = []

    async def _apply(**kwargs):
        applied.append(kwargs["relay_restart_required"])
        return RuntimeApplyResult(True, "provider_reloaded", "test")

    monkeypatch.setattr("flowly.account.runtime_apply.apply_account_runtime_change", _apply)

    logout_cmd.logout()

    server = load_config().channels.flowly_server
    assert server.enabled is True and server.agent_secret == "agent-secret"
    assert applied == [False]
    out = capsys.readouterr().out
    assert "Flowly server" in out and "relay" not in out.lower()
