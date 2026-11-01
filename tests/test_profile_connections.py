"""A key entered once on the main agent reaches every named agent (flowly/profile_connections.py).

Before: a named agent got the main agent's keys only when it was made; a key
added later stayed on the main agent, and choosing that provider for the named
agent left it with no key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import flowly.profile as profiles
from flowly.profile_connections import RECORD_FILE, sync_provider_keys


@pytest.fixture
def homes(tmp_path, monkeypatch):
    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    (default / "config.json").write_text("{}")
    created = profiles.create_profile("writer", local_runtime=True)
    return default, created


def _set(path: Path, provider: str, value: str) -> None:
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("providers", {}).setdefault(provider, {})["apiKey"] = value
    path.write_text(json.dumps(config))


def _key(path: Path, provider: str) -> str:
    return json.loads(path.read_text()).get("providers", {}).get(provider, {}).get("apiKey", "")


def test_a_key_added_on_the_main_agent_reaches_the_named_one(homes):
    default, writer = homes
    _set(default / "config.json", "anthropic", "sk-ant-main")
    assert sync_provider_keys("writer") == ["anthropic"]
    assert _key(writer / "config.json", "anthropic") == "sk-ant-main"
    record = (writer / RECORD_FILE).read_text()
    assert "sk-ant-main" not in record  # only a digest is kept
    assert sync_provider_keys("writer") == []  # nothing new


def test_a_copied_key_follows_the_main_agent_and_leaves_with_it(homes):
    default, writer = homes
    _set(default / "config.json", "openrouter", "sk-or-1")
    sync_provider_keys("writer")
    _set(default / "config.json", "openrouter", "sk-or-2")
    assert sync_provider_keys("writer") == ["openrouter"]
    assert _key(writer / "config.json", "openrouter") == "sk-or-2"
    _set(default / "config.json", "openrouter", "")
    assert sync_provider_keys("writer") == ["openrouter"]
    assert _key(writer / "config.json", "openrouter") == ""


def test_the_agents_own_key_is_never_touched(homes):
    default, writer = homes
    _set(writer / "config.json", "openai", "sk-own")
    _set(default / "config.json", "openai", "sk-main")
    assert sync_provider_keys("writer") == []
    _set(default / "config.json", "openai", "")
    assert sync_provider_keys("writer") == []
    assert _key(writer / "config.json", "openai") == "sk-own"


def test_a_key_already_equal_to_the_main_agents_counts_as_copied(homes):
    default, writer = homes
    _set(default / "config.json", "gemini", "g-1")
    _set(writer / "config.json", "gemini", "g-1")  # an agent made by copying the main one
    assert sync_provider_keys("writer") == []
    _set(default / "config.json", "gemini", "g-2")
    assert sync_provider_keys("writer") == ["gemini"]
    assert _key(writer / "config.json", "gemini") == "g-2"


def test_the_main_agent_and_other_settings_are_left_alone(homes):
    default, writer = homes
    config = json.loads((writer / "config.json").read_text())
    config["agents"] = {"defaults": {"model": "own/model"}}
    (writer / "config.json").write_text(json.dumps(config))
    _set(default / "config.json", "groq", "gq")
    assert sync_provider_keys("default") == []
    sync_provider_keys("writer")
    assert json.loads((writer / "config.json").read_text())["agents"]["defaults"]["model"] == "own/model"


@pytest.mark.asyncio
async def test_the_profile_host_brings_the_keys_before_it_starts_an_agent(homes, monkeypatch):
    import asyncio

    from flowly.profile_host import ProfileHost

    default, writer = homes
    _set(default / "config.json", "anthropic", "sk-ant-main")
    seen: list[str] = []

    async def spawn(*_args, **_kwargs):
        seen.append(_key(writer / "config.json", "anthropic"))
        raise RuntimeError("stop before a real process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="stop before"):
        await ProfileHost()._start_runtime("writer")
    assert seen == ["sk-ant-main"]
