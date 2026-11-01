"""An agent's template is built from what is its own, and never carries a secret."""
from __future__ import annotations

import json

import pytest

from flowly.cron.types import CronJob, CronPayload, CronSchedule
from flowly.secrets.redact import Redactor
from flowly.templates.export import build_recipe, check_secrets
from flowly.templates.recipe import RecipeError


@pytest.fixture
def agent(tmp_path):
    home = tmp_path / "profiles" / "research"
    workspace = home / "workspace"
    (workspace / "skills" / "source-brief").mkdir(parents=True)
    (workspace / "skills" / "source-brief" / "SKILL.md").write_text(
        "---\nname: Source brief\ndescription: A one-page brief with citations.\n---\n\nRead, then write five bullets.\n"
    )
    (workspace / "skills" / "Notes Helper").mkdir(parents=True)
    (workspace / "skills" / "Notes Helper" / "SKILL.md").write_text("Keep notes short.\n")
    (workspace / "SOUL.md").write_text("You cite every claim.")
    (home / "profile.json").write_text(json.dumps({
        "displayName": "Researcher", "description": "Finds sources.", "markShape": "cloud", "markTone": "sky",
    }))
    return home, workspace


def _jobs():
    return [
        CronJob(id="1", name="Morning reading", schedule=CronSchedule(kind="cron", expr="0 8 * * 1-5", tz="Europe/Istanbul"),
                payload=CronPayload(message="Find three new papers.")),
        CronJob(id="2", name="Hourly check", schedule=CronSchedule(kind="every", every_ms=3_600_000),
                payload=CronPayload(message="Check the feeds.")),
        CronJob(id="3", name="Once", schedule=CronSchedule(kind="at", at_ms=1), payload=CronPayload(message="Remind me.")),
        CronJob(id="4", name="Done", schedule=CronSchedule(kind="every", every_ms=60_000),
                payload=CronPayload(message="Old."), lifecycle="completed"),
        CronJob(id="5", name="Tool", schedule=CronSchedule(kind="every", every_ms=60_000),
                payload=CronPayload(kind="tool_call", tool_name="exec")),
    ]


def _build(agent, **overrides):
    home, workspace = agent
    args = dict(home=home, workspace=workspace, cron_jobs=_jobs(),
                mcp_servers={"notion": {"url": "https://mcp.notion.com", "headers": {"Authorization": "Bearer t0ken-value"}},
                             "github": {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_" + "a" * 36}}},
                name=None, description=None, skills=None, routines=None, include_apps=True, memories=[],
                redactor=Redactor({}))
    args.update(overrides)
    return build_recipe(**args)


def test_profile_soul_and_own_skills(agent):
    recipe, skipped = _build(agent)
    assert recipe["profile"] == {"displayName": "Researcher", "description": "Finds sources.",
                                 "soul": "You cite every claim.", "markShape": "cloud", "markTone": "sky"}
    by_slug = {skill["slug"]: skill for skill in recipe["skills"]}
    assert by_slug["source-brief"] == {"slug": "source-brief", "name": "Source brief",
                                       "description": "A one-page brief with citations.",
                                       "content": "Read, then write five bullets."}
    # A folder name that is no slug becomes one; a skill with no description uses its first line.
    assert by_slug["notes-helper"]["description"] == "Keep notes short."
    assert skipped == []


def test_routine_carries_prompt_and_schedule(agent):
    recipe, _ = _build(agent)
    assert recipe["routines"] == [
        {"name": "Morning reading", "description": "", "prompt": "Find three new papers.",
         "schedule": {"kind": "cron", "expr": "0 8 * * 1-5", "tz": "Europe/Istanbul"}},
        {"name": "Hourly check", "description": "", "prompt": "Check the feeds.",
         "schedule": {"kind": "every", "seconds": 3600}},
    ]


def test_app_values_never_exported(agent):
    recipe, _ = _build(agent)
    assert recipe["apps"] == [{"id": "github", "name": "github"}, {"id": "notion", "name": "notion"}]
    text = json.dumps(recipe)
    assert "mcp.notion.com" not in text and "t0ken-value" not in text and "ghp_" not in text
    assert _build(agent, include_apps=False)[0]["apps"] == []


def test_choosing_skills_and_routines(agent):
    recipe, _ = _build(agent, skills=["source-brief"], routines=["Hourly check"], name="Scout",
                       memories=["Prefers recent sources."])
    assert [s["slug"] for s in recipe["skills"]] == ["source-brief"]
    assert [r["name"] for r in recipe["routines"]] == ["Hourly check"]
    assert recipe["profile"]["displayName"] == "Scout"
    assert recipe["memories"] == [{"content": "Prefers recent sources."}]
    with pytest.raises(RecipeError) as caught:
        _build(agent, skills=["no-such-skill"])
    assert caught.value.field == "skills"
    with pytest.raises(RecipeError) as caught:
        _build(agent, routines=["No such routine"])
    assert caught.value.field == "routines"


def test_saved_secret_in_a_skill_is_refused(agent):
    home, workspace = agent
    token = "s3cr3t-value-of-the-owner"
    (workspace / "skills" / "source-brief" / "SKILL.md").write_text(f"Call the API with {token}.\n")
    with pytest.raises(RecipeError) as caught:
        _build(agent, redactor=Redactor({"RESEARCH_TOKEN": token}))
    assert caught.value.code == "CONTAINS_SECRET"
    assert caught.value.field.startswith("skills[")
    assert token not in str(caught.value)


@pytest.mark.parametrize("text", [
    "Use sk-" + "A" * 40 + " for the model.",
    "Token ghp_" + "b" * 36,
    "Slack xoxb-1234567890-abcdefghij",
    "Key AKIA" + "Q" * 16,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
])
def test_key_shaped_text_is_refused(agent, text):
    _, workspace = agent
    (workspace / "SOUL.md").write_text(text)
    with pytest.raises(RecipeError) as caught:
        _build(agent)
    assert (caught.value.code, caught.value.field) == ("CONTAINS_SECRET", "profile.soul")


def test_memories_are_checked_too():
    recipe = {"v": 1, "profile": {"displayName": "A", "description": "", "soul": "", "markShape": "", "markTone": ""},
              "skills": [], "routines": [], "apps": [], "memories": [{"content": "my key is sk-" + "z" * 30}]}
    with pytest.raises(RecipeError) as caught:
        check_secrets(recipe, Redactor({}))
    assert caught.value.field == "memories[0].content"


def test_a_skill_too_large_is_skipped_not_fatal(agent):
    _, workspace = agent
    (workspace / "skills" / "huge").mkdir()
    (workspace / "skills" / "huge" / "SKILL.md").write_text("---\ndescription: Big.\n---\n" + "x" * 70_000)
    recipe, skipped = _build(agent)
    assert "huge" not in [s["slug"] for s in recipe["skills"]]
    assert skipped == [{"kind": "skill", "name": "huge", "reason": "TOO_LARGE"}]
