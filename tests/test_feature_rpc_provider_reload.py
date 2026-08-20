"""Provider/model mutations must not mistake a failed live reload for success."""

from types import SimpleNamespace

import pytest

from flowly.channels import feature_rpc


@pytest.mark.asyncio
async def test_model_set_requests_restart_when_reload_reports_failure(monkeypatch):
    cfg = SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(model="old-model")),
    )
    saved = []

    monkeypatch.setattr("flowly.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("flowly.config.loader.save_config", saved.append)

    async def _failed_reload():
        return {"ok": False, "error": "provider build failed"}

    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", _failed_reload)

    result = await feature_rpc.model_set({"model": "new-model"})

    assert cfg.agents.defaults.model == "new-model"
    assert saved == [cfg]
    assert result == {"ok": True, "model": "new-model", "willRestart": True}


@pytest.mark.asyncio
async def test_model_set_stays_live_when_reload_succeeds(monkeypatch):
    cfg = SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(model="old-model")),
    )
    monkeypatch.setattr("flowly.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("flowly.config.loader.save_config", lambda _cfg: None)

    async def _successful_reload():
        return {"ok": True, "model": "new-model"}

    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", _successful_reload)

    result = await feature_rpc.model_set({"model": "new-model"})

    assert result == {"ok": True, "model": "new-model", "willRestart": False}


@pytest.mark.asyncio
async def test_provider_set_requests_restart_when_reload_reports_failure(monkeypatch):
    monkeypatch.setattr(
        "flowly.integrations.active_provider.set_active_provider",
        lambda _key: "provider-default-model",
    )
    monkeypatch.setattr("flowly.integrations.model_catalog.flush_cache", lambda: None)

    async def _failed_reload():
        return {"ok": False, "error": "provider build failed"}

    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", _failed_reload)

    result = await feature_rpc.provider_set({"key": "flowly"})

    assert result == {
        "ok": True,
        "key": "flowly",
        "model": "provider-default-model",
        "willRestart": True,
    }
