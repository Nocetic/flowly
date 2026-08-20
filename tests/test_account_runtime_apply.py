"""Account runtime changes preserve provider hot-reload and isolate relay restarts."""

from __future__ import annotations

import httpx
import pytest

from flowly.account import runtime_apply
from flowly.config.loader import save_config
from flowly.config.schema import Config
from flowly.integrations.service_control import RestartResult


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-home"))
    save_config(Config())


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "http://127.0.0.1/api/provider/reload"),
    )


@pytest.mark.asyncio
async def test_provider_change_hot_reloads_without_service_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_apply,
        "post_provider_reload",
        lambda **kwargs: _async_result(_response(200, {"ok": True, "key": "flowly"})),
    )
    monkeypatch.setattr(
        runtime_apply,
        "restart_gateway",
        lambda **kwargs: pytest.fail("provider-only change must not restart service"),
    )

    result = await runtime_apply.apply_account_runtime_change(
        provider_reload_required=True,
        relay_restart_required=False,
    )

    assert result.ok is True
    assert result.status == "provider_reloaded"


@pytest.mark.asyncio
async def test_relay_change_restarts_once_without_provider_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _restart(**kwargs):
        return RestartResult(True, "launchctl", "restarted via launchd")

    monkeypatch.setattr(runtime_apply, "restart_gateway", _restart)
    monkeypatch.setattr(
        runtime_apply,
        "post_provider_reload",
        lambda **kwargs: pytest.fail("service restart already reloads the provider"),
    )

    result = await runtime_apply.apply_account_runtime_change(
        provider_reload_required=True,
        relay_restart_required=True,
    )

    assert result.ok is True
    assert result.status == "gateway_restarted"


@pytest.mark.asyncio
async def test_manual_gateway_requires_manual_restart_for_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _restart(**kwargs):
        return RestartResult(False, "no_service", "service not installed")

    monkeypatch.setattr(runtime_apply, "restart_gateway", _restart)
    monkeypatch.setattr(runtime_apply, "gateway_is_listening", lambda **kwargs: True)

    result = await runtime_apply.apply_account_runtime_change(
        provider_reload_required=False,
        relay_restart_required=True,
    )

    assert result.ok is False
    assert result.status == "manual_restart"


@pytest.mark.asyncio
async def test_offline_gateway_applies_config_on_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _restart(**kwargs):
        return RestartResult(False, "no_service", "service not installed")

    monkeypatch.setattr(runtime_apply, "restart_gateway", _restart)
    monkeypatch.setattr(runtime_apply, "gateway_is_listening", lambda **kwargs: False)

    result = await runtime_apply.apply_account_runtime_change(
        provider_reload_required=False,
        relay_restart_required=True,
    )

    assert result.ok is True
    assert result.status == "next_start"


@pytest.mark.asyncio
async def test_security_sensitive_rejected_reload_falls_back_to_managed_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_apply,
        "post_provider_reload",
        lambda **kwargs: _async_result(_response(422, {"error": "no usable provider"})),
    )

    async def _restart(**kwargs):
        return RestartResult(True, "systemctl", "restarted via systemd")

    monkeypatch.setattr(runtime_apply, "restart_gateway", _restart)

    result = await runtime_apply.apply_account_runtime_change(
        provider_reload_required=True,
        relay_restart_required=False,
        security_sensitive=True,
    )

    assert result.ok is True
    assert result.status == "gateway_restarted"


@pytest.mark.asyncio
async def test_normal_rejected_provider_reload_does_not_restart_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_apply,
        "post_provider_reload",
        lambda **kwargs: _async_result(_response(422, {"error": "invalid key"})),
    )
    monkeypatch.setattr(
        runtime_apply,
        "restart_gateway",
        lambda **kwargs: pytest.fail("normal provider failure must not restart service"),
    )

    result = await runtime_apply.apply_account_runtime_change(
        provider_reload_required=True,
        relay_restart_required=False,
        security_sensitive=False,
    )

    assert result.ok is False
    assert result.status == "provider_reload_failed"
    assert result.detail == "invalid key"


@pytest.mark.asyncio
async def test_relay_restart_uses_custom_gateway_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config()
    cfg.gateway.port = 19991
    save_config(cfg)
    seen: list[int] = []

    async def _restart(**kwargs):
        seen.append(kwargs["health_check_port"])
        return RestartResult(True, "systemctl", "restarted")

    monkeypatch.setattr(runtime_apply, "restart_gateway", _restart)

    result = await runtime_apply.apply_account_runtime_change(
        provider_reload_required=False,
        relay_restart_required=True,
    )

    assert result.ok is True
    assert seen == [19991]


async def _async_result(value):
    return value
