"""A named agent shows its owner what the main agent does (2026-11-08).

Desktop, iOS and Android reach a named agent through ``profiles.rpc``; a page
whose method the host did not allow answered METHOD_NOT_ALLOWED (Library,
meetings, memory settings, audit, logs, knowledge graph).
"""

from unittest.mock import AsyncMock

import pytest

from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import (
    PROFILE_RPC_TIMEOUTS,
    ProfileHostError,
    profile_config_view,
    validate_profile_rpc,
)


@pytest.fixture
def host(monkeypatch, tmp_path):
    import flowly.profile as profiles

    monkeypatch.setattr(profiles, "_DEFAULT_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", tmp_path / "profiles")
    return ProfileHost()


# Every page the apps open on an agent, by the method it loads with.
PAGES = {
    "knowledge graph": ["kg.graph", "kg.delete_entity"],
    "library: media": ["media.library.list", "media.library.stats", "media.library.star", "media.library.delete"],
    "library: artifacts, meetings": [
        "artifacts.list", "artifacts.get", "artifacts.versions",
        "artifacts.pin", "artifacts.update", "artifacts.delete",
    ],
    "settings: audit": ["audit.list", "audit.stats", "audit.clear"],
    "settings: logs": ["logs.events"],
    "settings: tool routing": ["tools.semantic.status", "tools.semantic.enable", "tools.semantic.disable"],
    "settings: memory": ["config.get", "config.set"],
}


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_of_the_main_agent_opens_on_a_named_agent(page):
    for method in PAGES[page]:
        assert method in PROFILE_RPC_TIMEOUTS, f"{page}: {method}"


def test_its_settings_page_reads_only_its_fields():
    config = {
        "providers": {"openai": {"apiKey": "sk-secret"}},
        "channels": {"telegram": {"token": "bot-secret"}},
        "notifications": {"preview": "minimal"},
        "tools": {"artifact": {"enabled": False}, "web": {"apiKey": "secret"}},
        "agents": {"defaults": {
            "memory_dreaming": {"enabled": False, "commitMode": "manual"},
            "skillImprovement": {"enabled": True},
            "persona": "calm",
            "llmTimeoutSeconds": 90,
            "model": "x",
        }},
    }
    assert profile_config_view(config) == {
        "notifications": {"preview": "minimal"},
        "tools": {"artifact": {"enabled": False}},
        "agents": {"defaults": {
            "memoryDreaming": {"enabled": False, "commitMode": "manual"},
            "skillImprovement": {"enabled": True},
            "persona": "calm",
            "llmTimeoutSeconds": 90,
        }},
    }
    assert "secret" not in repr(profile_config_view(config))
    assert profile_config_view(None) == {}
    # config.get takes nothing a caller could steer.
    assert validate_profile_rpc("config.get", {"path": "providers"}) == ("config.get", {})


def test_its_settings_page_changes_only_what_it_shows():
    for patch in (
        {"agents": {"defaults": {"memoryDreaming": {"enabled": False}}}},
        {"agents": {"defaults": {"memoryDreaming": {"commitMode": "aggressive", "autoConsolidate": True}}}},
        {"agents": {"defaults": {"skillImprovement": {"enabled": True}}}},
        {"tools": {"artifact": {"enabled": False}}},
        {"notifications": {"preview": "full"}},
    ):
        assert validate_profile_rpc("config.set", {"config": patch, "restart": True}) == (
            "config.set", {"config": patch, "restart": True})
    for params in (
        {"config": {"providers": {"openai": {"apiKey": "sk"}}}},
        {"config": {"agents": {"defaults": {"model": "other"}}}},
        {"config": {"agents": {"defaults": {"persona": "loud"}}}},
        {"config": {"agents": {"defaults": {"memoryDreaming": {"commitMode": "everything"}}}}},
        {"config": {"agents": {"defaults": {"memoryDreaming": {"enabled": "yes"}}}}},
        {"config": {"tools": {"artifact": {}}}},
        {"config": {}},
        {"patch": {"notifications": {"preview": "full"}}},
        {"config": {"notifications": {"preview": "full"}}, "restart": "now"},
        {"config": {"notifications": {"preview": "full"}}, "path": "x"},
    ):
        with pytest.raises(ProfileHostError) as raised:
            validate_profile_rpc("config.set", params)
        assert raised.value.code == "INVALID_PARAMS", params


@pytest.mark.asyncio
async def test_config_get_answers_the_fields_not_the_file(host):
    host._target_rpc = AsyncMock(return_value={"config": {
        "providers": {"openai": {"apiKey": "sk-secret"}},
        "notifications": {"preview": "minimal"},
    }})
    assert await host.rpc("writer", "config.get", {}) == {"config": {"notifications": {"preview": "minimal"}}}


@pytest.mark.asyncio
async def test_phones_read_its_knowledge_graph_without_pinning(host):
    # iOS and Android pin identities only for mcp/gmail/memory/skills/cron/subagents/agent.home.
    host._target_rpc = AsyncMock(return_value={"entities": [], "triples": []})
    assert await host.rpc("writer", "kg.graph", {}) == {"entities": [], "triples": []}
    host._target_rpc.assert_awaited_once()


def _artifact(artifact_id, creator):
    metadata = {"flowlyProvenance": {"createdByProfile": creator}} if creator else {}
    return {"id": artifact_id, "title": artifact_id, "metadata": metadata}


@pytest.mark.asyncio
async def test_its_artifacts_are_the_ones_it_made_in_the_shared_store(host):
    store = [_artifact(f"a{index}", "writer" if index % 3 == 0 else ("coder" if index % 3 == 1 else None))
             for index in range(450)]
    calls = []

    async def primary(method, params, timeout):
        calls.append((method, params))
        if method == "artifacts.list":
            start = params["offset"]
            return {"artifacts": store[start:start + params["limit"]]}
        if method == "artifacts.get":
            return {"artifact": next(item for item in store if item["id"] == params["id"])}
        return {"artifact": {"id": params["id"], "pinned": params.get("pinned")}}

    host._primary_rpc = primary
    host._target_rpc = AsyncMock()

    listed = await host.rpc("writer", "artifacts.list", {"limit": 100, "offset": 20})
    ids = [row["id"] for row in listed["artifacts"]]
    assert ids == [f"a{index}" for index in range(60, 360, 3)]
    # Pages were read past the first until this agent's page was full.
    assert [params["offset"] for method, params in calls if method == "artifacts.list"] == [0, 200]

    calls.clear()
    pinned = await host.rpc("writer", "artifacts.pin", {"id": "a3", "pinned": True})
    assert pinned == {"artifact": {"id": "a3", "pinned": True}}
    assert calls == [("artifacts.get", {"id": "a3", "limit": 1}), ("artifacts.pin", {"id": "a3", "pinned": True})]

    # Another agent's artifact, or the main agent's, is not this one's to read or change.
    for other in ("a1", "a2"):
        calls.clear()
        with pytest.raises(ProfileHostError) as raised:
            await host.rpc("writer", "artifacts.delete", {"id": other})
        assert raised.value.code == "NOT_FOUND"
        assert [method for method, _ in calls] == ["artifacts.get"]
    # Nothing went to the named agent's own runtime: artifacts live in the primary.
    host._target_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_main_agents_pin_never_reaches_the_primary(host):
    seen = []

    async def primary(method, params, timeout):
        seen.append(params)
        if method == "artifacts.get":
            return {"artifact": _artifact("a0", "writer")}
        return {"ok": True}

    host._primary_rpc = primary
    await host.rpc("writer", "artifacts.update", {"id": "a0", "title": "t", "expectedBotId": "bot-writer"})
    assert all("expectedBotId" not in params for params in seen)
