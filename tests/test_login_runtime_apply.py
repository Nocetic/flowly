"""All CLI login paths apply relay and provider changes by the right mechanism."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

import flowly.cli.login_cmd as login_cmd
from flowly.account.account_key import AccountKeyChange
from flowly.account.health import RelayState
from flowly.account.relay_config import WebChannelChange
from flowly.account.server import RegisteredServer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-home"))


@pytest.mark.asyncio
async def test_repair_propagates_relay_restart_without_provider_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = RegisteredServer(
        server_id="server-b",
        name="Machine B",
        status="active",
        gateway_auth_token="relay-token-b",
        jwt_secret="relay-secret-b",
        existing=False,
    )

    async def _register(_token: str):
        return server

    monkeypatch.setattr("flowly.account.server.register_machine", _register)
    monkeypatch.setattr("flowly.account.auth.save_account", lambda account: None)
    monkeypatch.setattr(
        "flowly.account.relay_config.wire_relay_credentials",
        lambda srv: WebChannelChange(
            enabled_was=False,
            server_id_was="",
            changed=True,
            needs_gateway_restart=True,
        ),
    )
    monkeypatch.setattr(login_cmd, "_check_provider_state", lambda: (True, "flowly"))
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *args, **kwargs: None)

    result = await login_cmd._apply_repair(
        SimpleNamespace(
            id_token="firebase-token",
            server_id="",
            server_name="",
            gateway_auth_token="",
        ),
        dry_run=False,
    )

    assert result.needs_gateway_restart is True
    assert result.needs_provider_reload is False


def test_fresh_login_without_relay_hot_reloads_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = SimpleNamespace(
        user_id="uid-b",
        email="b@example.com",
        id_token="firebase-b",
        machine_name="Machine B",
    )

    async def _login_flow(**kwargs):
        return account

    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: None)
    monkeypatch.setattr("flowly.account.auth.run_login_flow", _login_flow)
    monkeypatch.setattr("flowly.account.auth.credential_storage_status", lambda: "test store")
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        login_cmd,
        "_provision_account_key",
        lambda current: AccountKeyChange(ready=True, changed=True),
    )
    monkeypatch.setattr(login_cmd, "_print_provider_verdict", lambda **kwargs: True)
    applied: list[tuple[bool, bool, bool]] = []
    monkeypatch.setattr(
        login_cmd,
        "_apply_runtime_changes",
        lambda **kwargs: applied.append(
            (
                kwargs["provider_reload_required"],
                kwargs["relay_restart_required"],
                kwargs["security_sensitive"],
            )
        )
        or True,
    )

    login_cmd.login(
        no_browser=True,
        repair=False,
        dry_run=False,
        key="",
        relay_opt=False,
    )

    assert applied == [(True, False, False)]


def test_existing_account_relay_opt_in_restarts_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = SimpleNamespace(
        user_id="uid-b",
        email="b@example.com",
        id_token="firebase-b",
    )
    repair = login_cmd._RepairResult(
        relay_wired=True,
        relay_changed=True,
        server_name="Machine B",
        needs_gateway_restart=True,
    )

    async def _refresh():
        return account

    async def _repair(current, *, dry_run: bool):
        return repair

    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)
    monkeypatch.setattr("flowly.account.auth.load_account_refreshing", _refresh)
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *args, **kwargs: None)
    monkeypatch.setattr(login_cmd, "_account_key_matches", lambda current: True)
    monkeypatch.setattr(login_cmd, "_check_relay_state", lambda: RelayState(False, "disabled"))
    monkeypatch.setattr(login_cmd, "_check_provider_state", lambda: (True, "flowly"))
    monkeypatch.setattr(login_cmd, "check_provider_corruption", lambda: [])
    monkeypatch.setattr(
        login_cmd,
        "_provision_account_key",
        lambda current: AccountKeyChange(ready=True, changed=False),
    )
    monkeypatch.setattr(login_cmd, "_apply_repair", _repair)
    applied: list[tuple[bool, bool]] = []
    monkeypatch.setattr(
        login_cmd,
        "_apply_runtime_changes",
        lambda **kwargs: applied.append(
            (kwargs["provider_reload_required"], kwargs["relay_restart_required"])
        )
        or True,
    )

    with pytest.raises(typer.Exit) as exc:
        login_cmd.login(
            no_browser=True,
            repair=False,
            dry_run=False,
            key="",
            relay_opt=True,
        )

    assert exc.value.exit_code == 0
    assert applied == [(False, True)]


def test_account_key_login_uses_hot_reload_not_relay_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flowly.config.loader import save_config
    from flowly.config.schema import Config

    save_config(Config())
    monkeypatch.setattr("flowly.integrations.active_provider.set_active_provider", lambda key: None)
    monkeypatch.setattr("flowly.integrations.model_catalog.flush_cache", lambda: None)

    async def _reconcile(**kwargs):
        return None

    monkeypatch.setattr(
        "flowly.integrations.model_catalog.reconcile_flowly_model",
        _reconcile,
    )
    applied: list[tuple[bool, bool]] = []
    monkeypatch.setattr(
        login_cmd,
        "_apply_runtime_changes",
        lambda **kwargs: applied.append(
            (kwargs["provider_reload_required"], kwargs["relay_restart_required"])
        )
        or True,
    )

    login_cmd._login_with_account_key("flw_manual_key")

    assert applied == [(True, False)]


@pytest.mark.asyncio
async def test_provider_promotion_requests_hot_reload_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = RegisteredServer(
        server_id="server-b",
        name="Machine B",
        status="active",
        gateway_auth_token="relay-token-b",
        jwt_secret="relay-secret-b",
        existing=True,
    )

    async def _register(_token: str):
        return server

    monkeypatch.setattr("flowly.account.server.register_machine", _register)
    monkeypatch.setattr("flowly.account.auth.save_account", lambda account: None)
    monkeypatch.setattr(
        "flowly.account.relay_config.wire_relay_credentials",
        lambda srv: WebChannelChange(
            enabled_was=True,
            server_id_was="server-b",
            changed=False,
            needs_gateway_restart=False,
        ),
    )
    monkeypatch.setattr(login_cmd, "_check_provider_state", lambda: (False, ""))
    monkeypatch.setattr("flowly.integrations.active_provider.set_active_provider", lambda key: None)
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *args, **kwargs: None)

    result = await login_cmd._apply_repair(
        SimpleNamespace(
            id_token="firebase-token",
            server_id="",
            server_name="",
            gateway_auth_token="",
        ),
        dry_run=False,
    )

    assert result.needs_gateway_restart is False
    assert result.needs_provider_reload is True
