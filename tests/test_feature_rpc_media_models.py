"""Model catalog over the shared RPC surface.

Desktop and iOS both pick models through this. The point of serving it from the
agent rather than letting clients call the provider directly is the API key: a
picker needs model names, not a credential, and shipping one to every device
turns every device into somewhere it can leak from.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.channels import feature_rpc
from flowly.media.catalog import MediaModel


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    from flowly import profile

    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    (home / "config.json").write_text(json.dumps({
        "tools": {"mediaGeneration": {"enabled": True, "apiKey": "fal-secret"}}
    }))
    yield home
    if hasattr(profile, "_cached_home"):
        profile._cached_home = None


@pytest.fixture
def stub_catalog(monkeypatch):
    models = [
        MediaModel("vendor/t2v", "Vendor T2V", "text-to-video"),
        MediaModel("vendor/t2i", "Vendor T2I", "text-to-image"),
    ]

    class FakeCatalog:
        last_force = None

        def __init__(self, **_kwargs):
            pass

        async def list_models(self, *, category=None, force=False):
            FakeCatalog.last_force = force
            return [m for m in models if not category or m.category == category]

        async def search(self, query, *, category=None, limit=50):
            return [m for m in models if query.lower() in m.endpoint_id.lower()][:limit]

        async def get(self, endpoint_id, *, with_schema=False):
            return next((m for m in models if m.endpoint_id == endpoint_id), None)

    import flowly.media.catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "ModelCatalog", FakeCatalog)
    return FakeCatalog


def _dispatch(method, params=None):
    return asyncio.run(feature_rpc.dispatch(method, params or {}))


def test_the_media_methods_are_served():
    for method in (
        "media.models.list",
        "media.models.search",
        "media.models.get",
        "media.models.refresh",
    ):
        assert method in feature_rpc.FEATURE_METHODS


def test_a_cold_catalog_sync_is_treated_as_long_running():
    assert "media.models.refresh" in feature_rpc.LONG_RUNNING_METHODS


def test_list_returns_models_and_the_configured_defaults(isolated_home, stub_catalog):
    result, _restart = _dispatch("media.models.list", {"category": "text-to-video"})

    assert [m["endpointId"] for m in result["models"]] == ["vendor/t2v"]
    assert result["category"] == "text-to-video"
    assert "textToVideo" in result["defaults"]


def test_the_api_key_never_crosses_the_wire(isolated_home, stub_catalog):
    for method, params in (
        ("media.models.list", {}),
        ("media.models.search", {"query": "vendor"}),
        ("media.models.get", {"endpointId": "vendor/t2v"}),
    ):
        result, _restart = _dispatch(method, params)
        assert "fal-secret" not in json.dumps(result), method


def test_search_narrows_the_list(isolated_home, stub_catalog):
    result, _restart = _dispatch("media.models.search", {"query": "t2i"})
    assert [m["endpointId"] for m in result["models"]] == ["vendor/t2i"]


def test_get_requires_an_endpoint_id(isolated_home, stub_catalog):
    with pytest.raises(feature_rpc.FeatureRpcError):
        _dispatch("media.models.get", {})


def test_get_reports_an_unknown_model(isolated_home, stub_catalog):
    with pytest.raises(feature_rpc.FeatureRpcError):
        _dispatch("media.models.get", {"endpointId": "nope/nope"})


def test_refresh_forces_a_sync(isolated_home, stub_catalog):
    result, _restart = _dispatch("media.models.refresh", {})
    assert result["count"] == 2
    assert stub_catalog.last_force is True


def test_none_of_these_ask_for_a_gateway_restart(isolated_home, stub_catalog):
    for method, params in (
        ("media.models.list", {}),
        ("media.models.refresh", {}),
    ):
        _result, restart = _dispatch(method, params)
        assert restart is False, method
