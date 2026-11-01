"""The share button's two calls (``agent.template.contents`` / ``.export``).

The agent answers from its own files, no model turn: what a template of it
could carry, and the recipe of what the owner chose. Memories never go from
here, and a key-like text is named, never shared.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import flowly.profile as profiles
from flowly.channels import feature_rpc
from flowly.channels.flowly_server import PROFILE_TEMPLATE_METHODS
from flowly.profile_host_contract import validate_profile_rpc
from flowly.templates.recipe import RecipeError, validate_recipe
from flowly.templates.share import share_contents, share_export


def _job(name: str, message: str, expr: str = "0 9 * * 1", kind: str = "agent_turn"):
    return SimpleNamespace(
        name=name, lifecycle="scheduled",
        payload=SimpleNamespace(kind=kind, message=message),
        schedule=SimpleNamespace(kind="cron", expr=expr, tz=None, every_ms=None),
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("research", display_name="Researcher", description="Finds sources.")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    workspace = home / "workspace"
    # As a real agent's config says (the gateway's AgentLoop uses the same path).
    config = home / "config.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    data.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = str(workspace)
    config.write_text(json.dumps(data))
    for slug, text in (("brief", "Write a brief."), ("digest", "Write a digest.")):
        (workspace / "skills" / slug).mkdir(parents=True)
        (workspace / "skills" / slug / "SKILL.md").write_text(f"---\ndescription: A {slug}.\n---\n{text}\n")
    (workspace / "SOUL.md").write_text("You read primary sources and cite every claim.")
    (workspace / "MEMORY.md").write_text("The owner's sister lives in Izmir.")
    jobs = [_job("Weekly digest", "Give me the weekly digest."), _job("Ping", "x", kind="system_event")]
    return workspace, jobs


def test_contents_lists_what_could_be_shared(agent):
    _, jobs = agent
    contents = share_contents(jobs)
    assert contents["refused"] is None
    assert (contents["name"], contents["description"]) == ("Researcher", "Finds sources.")
    assert [s["slug"] for s in contents["skills"]] == ["brief", "digest"]
    assert contents["skills"][0] == {"slug": "brief", "name": "brief", "description": "A brief."}
    # Only the agent's own turns are routines; the system event is not one.
    assert contents["routines"] == [{"name": "Weekly digest", "description": "", "schedule": {"kind": "cron", "expr": "0 9 * * 1"}}]
    assert "content" not in json.dumps(contents["skills"])


def test_contents_names_a_key_like_text_and_where_it_is(agent):
    workspace, jobs = agent
    (workspace / "SOUL.md").write_text("Use sk-" + "Q" * 30 + " for the search.")
    contents = share_contents(jobs)
    assert contents["refused"]["code"] == "CONTAINS_SECRET"
    assert contents["refused"]["field"] == "profile.soul"
    assert "Q" * 30 not in json.dumps(contents)
    assert contents["skills"] == []


def test_export_carries_only_what_was_chosen_and_never_memories(agent):
    _, jobs = agent
    result = share_export({"skills": ["digest"], "routines": [], "apps": False}, jobs)
    recipe = validate_recipe(result["recipe"])
    assert [s["slug"] for s in recipe["skills"]] == ["digest"]
    assert recipe["routines"] == [] and recipe["apps"] == [] and recipe["memories"] == []
    assert recipe["profile"]["soul"] == "You read primary sources and cite every claim."
    assert "Izmir" not in json.dumps(result)


def test_export_without_choices_carries_everything_of_its_own(agent):
    _, jobs = agent
    recipe = share_export({}, jobs)["recipe"]
    assert [s["slug"] for s in recipe["skills"]] == ["brief", "digest"]
    assert [r["name"] for r in recipe["routines"]] == ["Weekly digest"]
    assert recipe["memories"] == []


@pytest.mark.parametrize("params, field", [
    ({"memories": ["hi"]}, ""),
    ({"skills": "brief"}, "skills"),
    ({"skills": ["nope"]}, "skills"),
    ({"routines": ["Nope"]}, "routines"),
    ({"apps": "yes"}, "apps"),
])
def test_export_refuses_what_it_cannot_carry(agent, params, field):
    _, jobs = agent
    with pytest.raises(RecipeError) as refused:
        share_export(params, jobs)
    assert (refused.value.code, refused.value.field) == ("BAD_RECIPE", field)


async def test_rpc_answers_with_the_recipe_and_maps_a_refusal(agent, monkeypatch):
    _, jobs = agent
    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: SimpleNamespace(list_jobs=lambda include_disabled: jobs))
    result = await feature_rpc.agent_template_export({"skills": ["brief"]})
    assert [s["slug"] for s in result["recipe"]["skills"]] == ["brief"]
    with pytest.raises(feature_rpc.FeatureRpcError) as refused:
        await feature_rpc.agent_template_export({"skills": ["nope"]})
    assert refused.value.code == "BAD_RECIPE"
    assert [s["slug"] for s in (await feature_rpc.agent_template_contents())["skills"]] == ["brief", "digest"]


def test_both_reach_a_named_agent():
    for method in ("agent.template.contents", "agent.template.export"):
        assert method in feature_rpc._DISPATCH
        assert method in PROFILE_TEMPLATE_METHODS
        assert validate_profile_rpc(method, {})[0] == method
