"""Consent, recommendation, and RPC lifecycle for semantic tool routing."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from flowly.agent.tools import semantic_feature
from flowly.channels import feature_rpc
from flowly.config.loader import load_config
from flowly.config.schema import Config


@pytest.fixture(autouse=True)
def isolated_feature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    semantic_feature.reset_install_state_for_tests()
    feature_rpc.set_semantic_tool_metrics_provider(None)
    yield
    feature_rpc.set_semantic_tool_metrics_provider(None)
    semantic_feature.reset_install_state_for_tests()


def _metrics(**overrides):
    value = {
        "eligible": True,
        "catalogReady": True,
        "toolCount": 26,
        "deferredToolCount": 18,
        "deferredSchemaTokens": 8_000,
        "originalSchemaTokens": 11_000,
        "disclosedSchemaTokens": 3_000,
        "routerState": "disabled",
    }
    value.update(overrides)
    return value


def test_fresh_config_never_enables_or_downloads_semantic_model() -> None:
    discovery = Config().tools.routing.discovery

    assert discovery.semantic_routing_enabled is False
    assert discovery.semantic_model_auto_download is False
    assert discovery.semantic_routing_consent == "undecided"


def test_nuitka_entry_preserves_lazy_runtime_and_fastembed_metadata() -> None:
    entry = Path(semantic_feature.__file__).parents[2] / "cli" / "entry.py"
    source = entry.read_text(encoding="utf-8")

    for package in ("fastembed", "huggingface_hub", "onnxruntime", "tokenizers"):
        assert f"# nuitka-project: --include-package={package}" in source
    assert "# nuitka-project: --include-distribution-metadata=fastembed" in source


def test_enabled_preference_with_missing_model_is_repairable(monkeypatch) -> None:
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(semantic_feature, "installed_model_dir", lambda cache_dir=None: None)
    semantic_feature.persist_semantic_preference(consent="enabled", enabled=True)

    status = semantic_feature.feature_status(_metrics())

    assert status["state"] == "needs_download"
    assert status["enabled"] is False
    assert status["configuredEnabled"] is True


def test_status_recommends_only_after_both_growth_thresholds(monkeypatch) -> None:
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(semantic_feature, "installed_model_dir", lambda cache_dir=None: None)

    assert semantic_feature.feature_status(_metrics())["state"] == "recommended"
    assert semantic_feature.feature_status(
        _metrics(deferredToolCount=11)
    )["state"] == "idle"
    assert semantic_feature.feature_status(
        _metrics(deferredSchemaTokens=2_499)
    )["state"] == "idle"


def test_dismissal_is_persisted_for_every_surface(monkeypatch) -> None:
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(semantic_feature, "installed_model_dir", lambda cache_dir=None: None)
    feature_rpc.set_semantic_tool_metrics_provider(_metrics)

    result = feature_rpc.semantic_tool_routing_dismiss({})

    assert result["state"] == "dismissed"
    assert result["willRestart"] is False
    discovery = load_config().tools.routing.discovery
    assert discovery.semantic_routing_consent == "dismissed"
    assert discovery.semantic_routing_enabled is False


def test_dismissal_resurfaces_only_after_material_catalog_growth(monkeypatch) -> None:
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(semantic_feature, "installed_model_dir", lambda cache_dir=None: None)
    semantic_feature.persist_semantic_preference(
        consent="dismissed",
        enabled=False,
        dismissed_tool_count=18,
        dismissed_schema_tokens=8_000,
    )

    assert semantic_feature.feature_status(_metrics())["state"] == "dismissed"
    grown = semantic_feature.feature_status(_metrics(
        deferredToolCount=26,
        deferredSchemaTokens=10_000,
    ))
    assert grown["state"] == "recommended"
    assert grown["recommendationResurfaced"] is True


def test_disable_records_current_catalog_and_does_not_immediately_reprompt(
    monkeypatch,
) -> None:
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(semantic_feature, "installed_model_dir", lambda cache_dir=None: None)
    semantic_feature.persist_semantic_preference(consent="enabled", enabled=True)
    feature_rpc.set_semantic_tool_metrics_provider(_metrics)

    result = feature_rpc.semantic_tool_routing_disable({})

    assert result["state"] == "dismissed"
    assert result["recommended"] is False
    discovery = load_config().tools.routing.discovery
    assert discovery.semantic_recommendation_dismissed_tool_count == 18
    assert discovery.semantic_recommendation_dismissed_schema_tokens == 8_000


def test_explicit_enable_verifies_before_enabling_and_requests_restart(
    monkeypatch,
) -> None:
    installed = []
    installed_dir: list[Path | None] = [None]
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(
        semantic_feature,
        "installed_model_dir",
        lambda cache_dir=None: installed_dir[0],
    )

    def install(cache_dir=None):
        installed.append(True)
        installed_dir[0] = Path("/verified/model")
        return installed_dir[0]

    monkeypatch.setattr(
        semantic_feature,
        "install_semantic_model",
        install,
    )
    feature_rpc.set_semantic_tool_metrics_provider(_metrics)

    result, restart = asyncio.run(feature_rpc.dispatch(
        "tools.semantic.enable",
        {"restart": True},
    ))

    assert installed == [True]
    assert result["ok"] is True
    assert result["enabled"] is True
    assert result["state"] == "enabled"
    assert restart is True
    discovery = load_config().tools.routing.discovery
    assert discovery.semantic_routing_consent == "enabled"
    assert discovery.semantic_routing_enabled is True
    assert discovery.semantic_model_auto_download is False


def test_failed_enable_keeps_lexical_path_configured_and_exposes_retry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(semantic_feature, "installed_model_dir", lambda cache_dir=None: None)

    def fail(cache_dir=None):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(semantic_feature, "install_semantic_model", fail)
    feature_rpc.set_semantic_tool_metrics_provider(_metrics)

    with pytest.raises(feature_rpc.FeatureRpcError) as caught:
        asyncio.run(feature_rpc.dispatch("tools.semantic.enable", {}))

    assert caught.value.code == "INSTALL_FAILED"
    discovery = load_config().tools.routing.discovery
    assert discovery.semantic_routing_consent == "enabled"
    assert discovery.semantic_routing_enabled is False
    status = semantic_feature.feature_status(_metrics())
    assert status["state"] == "failed"
    assert "network unavailable" in status["installError"]


def test_model_install_survives_request_cancellation(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    installed_dir: list[Path | None] = [None]
    monkeypatch.setattr(semantic_feature, "semantic_runtime_available", lambda: True)
    monkeypatch.setattr(
        semantic_feature,
        "installed_model_dir",
        lambda cache_dir=None: installed_dir[0],
    )

    def install(cache_dir=None):
        started.set()
        assert release.wait(timeout=2)
        installed_dir[0] = Path("/verified/model")
        return installed_dir[0]

    monkeypatch.setattr(semantic_feature, "install_semantic_model", install)
    feature_rpc.set_semantic_tool_metrics_provider(_metrics)

    async def scenario() -> None:
        request = asyncio.create_task(
            feature_rpc.dispatch("tools.semantic.enable", {"restart": True})
        )
        assert await asyncio.to_thread(started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        for _attempt in range(100):
            if load_config().tools.routing.discovery.semantic_routing_enabled:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("detached semantic install did not finish")

    asyncio.run(scenario())
    assert semantic_feature.feature_status(_metrics())["state"] == "enabled"


def test_rpc_contract_is_registered_and_enable_is_long_running() -> None:
    expected = {
        "tools.semantic.status",
        "tools.semantic.enable",
        "tools.semantic.dismiss",
        "tools.semantic.disable",
    }
    assert expected <= feature_rpc.FEATURE_METHODS
    assert "tools.semantic.enable" in feature_rpc.LONG_RUNNING_METHODS


def test_persisted_shape_uses_camel_case_and_preserves_siblings() -> None:
    home = semantic_feature.get_flowly_home()
    home.mkdir(parents=True)
    config_path = home / "config.json"
    config_path.write_text(json.dumps({"tools": {"exec": {"enabled": True}}}))

    semantic_feature.persist_semantic_preference(consent="dismissed", enabled=False)

    raw = json.loads(config_path.read_text())
    discovery = raw["tools"]["routing"]["discovery"]
    assert discovery["semanticRoutingConsent"] == "dismissed"
    assert discovery["semanticRoutingEnabled"] is False
    assert raw["tools"]["exec"]["enabled"] is True
