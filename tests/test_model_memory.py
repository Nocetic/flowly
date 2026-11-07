"""The model chosen on each provider comes back with it, and favourite models
live on the computer (model_memory.py, model_favorites.py).

Before (2026-11-07): switching provider replaced the agent's model with the
new provider's curated default, so coming back to a provider never brought
back the model chosen there; and there were no favourites. Now each agent
keeps the model it last chose on each provider, and the computer keeps one
list of starred models that every agent and app reads.
"""

from __future__ import annotations

import json
import threading

import pytest

import flowly.integrations.model_catalog as catalog
import flowly.profile as profiles
from flowly.channels import feature_rpc
from flowly.integrations import model_favorites
from flowly.integrations.active_provider import DEFAULT_MODELS, set_active_provider
from flowly.integrations.model_memory import model_on_switch, recall, remember, remembered
from flowly.profile_host import ProfileHost

GROK = "grok-4.7"
CLAUDE = "claude-opus-5-5"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A main agent signed in to xAI and Anthropic with API keys, on Grok."""
    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    (default / "config.json").write_text(json.dumps({
        "agents": {"defaults": {"model": GROK}},
        "providers": {"active": "xai", "xai": {"apiKey": "xai-test"}, "anthropic": {"apiKey": "sk-ant-test"}},
    }))
    monkeypatch.setattr(catalog, "_CACHE", {})

    async def reloaded():
        return {"ok": True}

    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", reloaded)
    return default


def _config(home) -> dict:
    return json.loads((home / "config.json").read_text())


def _model(home) -> str:
    return _config(home)["agents"]["defaults"]["model"]


# ── the main agent ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coming_back_to_a_provider_brings_its_model_back(home):
    set_active_provider("anthropic")
    assert _model(home) == DEFAULT_MODELS["anthropic"]  # nothing chosen there yet
    await feature_rpc.model_set({"model": CLAUDE})

    set_active_provider("xai")
    assert _model(home) == GROK  # the model chosen on xAI, not its default

    set_active_provider("anthropic")
    assert _model(home) == CLAUDE


@pytest.mark.asyncio
async def test_a_model_chosen_before_the_memory_existed_is_kept_on_the_way_out(home):
    """The agent ran Grok before this change: leaving xAI remembers it."""
    set_active_provider("anthropic")
    set_active_provider("xai")
    assert _model(home) == GROK


@pytest.mark.asyncio
async def test_model_set_with_a_provider_switches_both_at_once(home):
    result = await feature_rpc.model_set({"provider": "anthropic", "model": CLAUDE})
    cfg = _config(home)
    assert (cfg["providers"]["active"], cfg["agents"]["defaults"]["model"]) == ("anthropic", CLAUDE)
    assert result["provider"] == "anthropic" and result["model"] == CLAUDE
    with pytest.raises(feature_rpc.FeatureRpcError) as refused:
        await feature_rpc.model_set({"provider": "no-such", "model": CLAUDE})
    assert refused.value.code == "INVALID"


def test_provider_keys_survive_the_config_writer(home):
    """The writer turns dict keys to camelCase on disk (openaiCodex); the
    memory finds them either way."""
    from flowly.config.loader import load_config, save_config

    cfg = load_config()
    cfg.agents.defaults.models_by_provider = {"openai_codex": "gpt-5.5"}
    save_config(cfg)
    raw = _config(home)
    assert "openaiCodex" in raw["agents"]["defaults"]["modelsByProvider"]
    assert remembered(raw, "openai_codex") == "gpt-5.5"
    remember(raw, "openai_codex", "gpt-5.6-sol")
    assert list(raw["agents"]["defaults"]["modelsByProvider"].values()) == ["gpt-5.6-sol"]


def test_a_model_deleted_on_a_local_server_does_not_come_back(home, monkeypatch):
    raw = {"agents": {"defaults": {"modelsByProvider": {"vllm": "google/gemma-4-e2b"}}}}
    monkeypatch.setitem(catalog._CACHE, "vllm", [catalog.Model(id="prism-ml/bonsai-27b", name="Bonsai")])
    assert recall(raw, "vllm") is None
    monkeypatch.setitem(catalog._CACHE, "vllm", [catalog.Model(id="google/gemma-4-e2b", name="Gemma")])
    assert recall(raw, "vllm") == "google/gemma-4-e2b"


def test_a_remembered_model_the_provider_no_longer_fits_is_ignored():
    raw = {"agents": {"defaults": {"modelsByProvider": {"anthropic": "grok-4.7"}}}}
    assert model_on_switch(raw, "", "anthropic", "") is None


# ── named agents ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_named_agent_brings_its_own_model_back(home):
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    await host.configure({"name": "writer", "provider": "xai", "model": GROK})
    await host.configure({"name": "writer", "provider": "anthropic", "model": CLAUDE})

    back = await host.configure({"name": "writer", "provider": "xai"})
    assert back["settings"]["model"] == GROK
    again = await host.configure({"name": "writer", "provider": "anthropic"})
    assert again["settings"]["model"] == CLAUDE
    # The main agent's memory is its own.
    assert remembered(_config(home), "anthropic") == ""


# ── favourites ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_favourites_are_kept_in_order_and_starring_is_idempotent(home):
    feature_rpc.model_favorite({"provider": "xai", "model": GROK, "on": True})
    feature_rpc.model_favorite({"provider": "anthropic", "model": CLAUDE, "on": True})
    listed = feature_rpc.model_favorite({"provider": "xai", "model": GROK, "on": True})["favorites"]
    assert [(f["provider"], f["model"]) for f in listed] == [("xai", GROK), ("anthropic", CLAUDE)]
    assert listed[0]["providerName"] and listed[0]["available"] is True

    after = feature_rpc.model_favorite({"provider": "xai", "model": GROK, "on": False})["favorites"]
    assert [(f["provider"], f["model"]) for f in after] == [("anthropic", CLAUDE)]
    again = feature_rpc.model_favorite({"provider": "xai", "model": GROK, "on": False})["favorites"]
    assert again == after


@pytest.mark.asyncio
@pytest.mark.parametrize(("params", "code"), [
    ({"provider": "nope", "model": "m", "on": True}, "INVALID"),
    ({"provider": "xai", "model": "", "on": True}, "INVALID"),
    ({"provider": "xai", "model": "m", "on": "yes"}, "INVALID"),
])
async def test_a_bad_favourite_is_refused(home, params, code):
    with pytest.raises(feature_rpc.FeatureRpcError) as refused:
        feature_rpc.model_favorite(params)
    assert refused.value.code == code


def test_a_favourite_whose_provider_is_not_set_up_is_kept_and_marked(home):
    listed = feature_rpc.model_favorite({"provider": "groq", "model": "openai/gpt-oss-120b", "on": True})["favorites"]
    assert listed == [{"provider": "groq", "model": "openai/gpt-oss-120b", "name": "openai/gpt-oss-120b",
                       "providerName": listed[0]["providerName"], "available": False}]


def test_the_list_is_the_computers_shared_by_its_named_agents(home, monkeypatch):
    model_favorites.set_favorite("xai", GROK, True)
    path = home / "state" / "model-favorites.json"
    assert path.exists()
    # A named agent's process reads the main agent's home.
    monkeypatch.setattr(profiles, "current_profile_name", lambda: "writer")
    assert [(f["provider"], f["model"]) for f in model_favorites.favorites()] == [("xai", GROK)]


def test_two_agents_starring_at_once_both_land(home):
    models = [f"grok-{i}" for i in range(24)]
    threads = [threading.Thread(target=model_favorites.set_favorite, args=("xai", m, True)) for m in models]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(f["model"] for f in model_favorites.favorites()) == sorted(models)


def test_the_list_has_a_ceiling(home, monkeypatch):
    monkeypatch.setattr(model_favorites, "MAX_FAVORITES", 2)
    model_favorites.set_favorite("xai", "a", True)
    model_favorites.set_favorite("xai", "b", True)
    with pytest.raises(model_favorites.FavoritesError) as full:
        model_favorites.set_favorite("xai", "c", True)
    assert full.value.code == "FAVORITES_FULL"


def test_a_damaged_file_reads_as_no_favourites(home):
    path = home / "state" / "model-favorites.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert model_favorites.favorites() == []
    model_favorites.set_favorite("xai", GROK, True)
    assert [f["model"] for f in model_favorites.favorites()] == [GROK]


@pytest.mark.asyncio
async def test_the_model_list_says_which_models_are_starred(home, monkeypatch):
    monkeypatch.setitem(catalog._CACHE, "xai", [
        catalog.Model(id=GROK, name="Grok 4.7"), catalog.Model(id="grok-4.6", name="Grok 4.6")])
    model_favorites.set_favorite("xai", GROK, True)
    rows = {row["id"]: row for row in (await feature_rpc.model_list({}))["models"]}
    assert rows[GROK]["favorite"] is True and rows["grok-4.6"]["favorite"] is False
    # A catalogue that read the model names it.
    assert feature_rpc.model_favorites()["favorites"][0]["name"] == "Grok 4.7"
