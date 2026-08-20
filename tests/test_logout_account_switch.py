"""Logout removes the previous account and applies that state to the gateway."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from flowly.account.runtime_apply import RuntimeApplyResult
from flowly.cli import logout_cmd
from flowly.config.loader import load_config, save_config
from flowly.config.schema import Config


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("DELETE", "https://example.test"),
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-home"))
    monkeypatch.setattr("flowly.account.audit_log.info", lambda *args, **kwargs: None)
    monkeypatch.setattr("flowly.account.audit_log.error", lambda *args, **kwargs: None)


def test_logout_clears_flowly_credentials_preserves_byok_and_restarts_for_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config()
    cfg.providers.active = "flowly"
    cfg.providers.flowly.account_key = "flw_account_a"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "key-a"
    cfg.providers.flowly.account_key_origin = "verified"
    cfg.providers.openai.api_key = "user-owned-byok"
    cfg.channels.web.enabled = True
    cfg.channels.web.server_id = "server-a"
    cfg.channels.web.auth_token = "relay-a"
    save_config(cfg)
    account = SimpleNamespace(
        user_id="uid-a",
        email="a@example.com",
        id_token="firebase-a",
    )
    cleared: list[bool] = []
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)
    monkeypatch.setattr("flowly.account.auth.clear_account", lambda: cleared.append(True))
    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *args, **kwargs: _response({"success": True}),
    )
    applied: list[tuple[bool, bool, bool]] = []

    async def _apply(**kwargs):
        applied.append(
            (
                kwargs["provider_reload_required"],
                kwargs["relay_restart_required"],
                kwargs["security_sensitive"],
            )
        )
        return RuntimeApplyResult(True, "gateway_restarted", "test restart")

    monkeypatch.setattr(
        "flowly.account.runtime_apply.apply_account_runtime_change",
        _apply,
    )

    logout_cmd.logout()
    reloaded = load_config()

    assert cleared == [True]
    assert applied == [(True, True, True)]
    assert reloaded.providers.flowly.account_key == ""
    assert reloaded.providers.flowly.account_key_owner_uid == ""
    assert reloaded.providers.flowly.account_key_id == ""
    assert reloaded.providers.flowly.account_key_origin == ""
    assert reloaded.channels.web.enabled is False
    assert reloaded.channels.web.server_id == ""
    assert reloaded.providers.active == ""
    assert reloaded.providers.openai.api_key == "user-owned-byok"


def test_logout_without_relay_uses_security_sensitive_provider_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config()
    cfg.providers.active = "flowly"
    cfg.providers.flowly.account_key = "flw_account_a"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "key-a"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)
    account = SimpleNamespace(
        user_id="uid-a",
        email="a@example.com",
        id_token="firebase-a",
    )
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)
    monkeypatch.setattr("flowly.account.auth.clear_account", lambda: None)
    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *args, **kwargs: _response({"success": True}),
    )
    applied: list[tuple[bool, bool, bool]] = []

    async def _apply(**kwargs):
        applied.append(
            (
                kwargs["provider_reload_required"],
                kwargs["relay_restart_required"],
                kwargs["security_sensitive"],
            )
        )
        return RuntimeApplyResult(True, "provider_reloaded", "openai")

    monkeypatch.setattr(
        "flowly.account.runtime_apply.apply_account_runtime_change",
        _apply,
    )

    logout_cmd.logout()

    assert applied == [(True, False, True)]
