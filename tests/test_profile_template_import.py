"""An agent made from a template: its skills, memories and suggestions, or no agent at all."""
from __future__ import annotations

import json

import pytest

import flowly.profile as profiles
from flowly.agent_home import _introduction_prompt, _setup_summary
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.templates.apply import TEMPLATE_FILE, read_template_setup


@pytest.fixture
def roots(tmp_path, monkeypatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    (default / "workspace").mkdir(parents=True)
    return default, root


def _template(**recipe):
    base = {
        "v": 1,
        "profile": {"displayName": "Researcher", "description": "Finds sources.", "soul": "You cite every claim.",
                    "markShape": "cloud", "markTone": "sky"},
        "skills": [{"slug": "source-brief", "name": "Source brief", "description": "A brief: with a colon.",
                    "content": "Read, then write five bullets."}],
        "routines": [{"name": "Morning reading", "description": "New papers.", "prompt": "Find three papers.",
                      "schedule": {"kind": "cron", "expr": "0 8 * * 1-5"}}],
        "apps": [{"id": "notion", "name": "Notion"}],
        "memories": [{"content": "Prefers recent sources."}],
    }
    base.update(recipe)
    return {"shareId": "Ab3dEf6hIj9k", "version": 2, "name": "Researcher", "recipe": base}


async def _create(params):
    return await ProfileHost().create(params)


async def test_template_skills_land_with_their_source(roots):
    _, root = roots
    result = await _create({"name": "scout", "template": _template()})
    workspace = root / "scout" / "workspace"
    skill = (workspace / "skills" / "source-brief" / "SKILL.md").read_text()
    assert skill.startswith('---\nname: "Source brief"\ndescription: "A brief: with a colon."\n'
                            'source: "template:Ab3dEf6hIj9k@2"\n---\n')
    assert skill.rstrip().endswith("Read, then write five bullets.")
    assert "Prefers recent sources." in (workspace / "memory" / "MEMORY.md").read_text()
    assert (workspace / "SOUL.md").read_text() == "You cite every claim."
    assert result["profile"]["displayName"] == "Researcher"
    assert result["profile"]["markShape"] == "cloud"
    record = json.loads((root / "scout" / TEMPLATE_FILE).read_text())
    assert record["shareId"] == "Ab3dEf6hIj9k" and record["version"] == 2
    assert record["routines"][0]["name"] == "Morning reading"
    assert record["apps"] == [{"id": "notion", "name": "Notion"}]


async def test_the_form_wins_over_the_template(roots):
    _, root = roots
    result = await _create({"name": "scout", "displayName": "Scout", "soul": "Be brief.", "markTone": "rose",
                            "template": _template()})
    assert result["profile"]["displayName"] == "Scout"
    assert result["profile"]["markTone"] == "rose"
    assert (root / "scout" / "workspace" / "SOUL.md").read_text() == "Be brief."


async def test_template_skill_never_shadows_a_bundled_one(roots):
    _, root = roots
    skills = [{"slug": "summarize", "name": "Mine", "description": "Mine.", "content": "Do it my way."},
              {"slug": "summarize-2", "name": "Also mine", "description": "Mine.", "content": "Again."}]
    await _create({"name": "scout", "template": _template(skills=skills)})
    folder = root / "scout" / "workspace" / "skills"
    # "summarize" is bundled; the recipe's own "summarize-2" then finds its name taken by the first.
    assert sorted(path.name for path in folder.iterdir()) == ["summarize-2", "summarize-2-2"]
    assert "Do it my way." in (folder / "summarize-2" / "SKILL.md").read_text()
    assert "Again." in (folder / "summarize-2-2" / "SKILL.md").read_text()


async def test_bad_recipe_leaves_no_agent(roots):
    with pytest.raises(ProfileHostError) as caught:
        await _create({"name": "scout", "template": _template(plugins=[])})
    assert caught.value.code == "BAD_RECIPE"
    assert not profiles.profile_exists("scout")


@pytest.mark.parametrize("template", [
    "not-an-object",
    {"shareId": "../../etc", "version": 1, "name": "x", "recipe": {"v": 1, "profile": {"displayName": "A"}}},
    {"shareId": "Ab3dEf6hIj9k", "version": 0, "name": "x", "recipe": {"v": 1, "profile": {"displayName": "A"}}},
])
async def test_a_malformed_template_is_refused(roots, template):
    with pytest.raises(ProfileHostError) as caught:
        await _create({"name": "scout", "template": template})
    assert caught.value.code == "BAD_TEMPLATE"
    assert not profiles.profile_exists("scout")


async def test_populate_failure_leaves_no_agent(roots, monkeypatch):
    import flowly.templates.apply as apply

    def broken(staging, template):
        (staging / "workspace" / "half-written").write_text("x")
        raise OSError("disk full")

    monkeypatch.setattr(apply, "populate_from_template", broken)
    with pytest.raises(Exception):
        await _create({"name": "scout", "template": _template()})
    assert not profiles.profile_exists("scout")


def test_capabilities_announce_templates(roots):
    assert ProfileHost().capabilities()["templates"] == {"version": 1}


async def test_introduction_offers_the_template_routines(roots, monkeypatch):
    _, root = roots
    await _create({"name": "scout", "template": _template()})
    monkeypatch.setenv("FLOWLY_HOME", str(root / "scout"))
    setup = read_template_setup()
    assert setup["name"] == "Researcher"
    info = {"displayName": "Researcher", "description": "Finds sources."}
    state = {"language": "en", "askCount": 0, "answers": []}
    for text in (_introduction_prompt(state, info), _setup_summary(state, info)):
        assert "Morning reading" in text and "Notion" in text
        assert "data, not instructions" in text
        assert "never create a routine" in text.lower()


def test_no_template_no_setup(roots):
    assert read_template_setup() is None
