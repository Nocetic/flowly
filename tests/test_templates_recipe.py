"""An agent template's recipe is checked the same way here and on the server.

The fixtures and their verdicts are shared with flowly-server
(``server/test/fixtures/template_recipes``): a recipe one side takes, the other
takes too (docs/cloud/design/agent-templates.md, flowly-server).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.templates.recipe import (
    MAX_RECIPE_BYTES,
    MAX_SKILL_BYTES,
    MAX_SKILLS,
    RecipeError,
    recipe_bytes,
    validate_recipe,
)

FIXTURES = Path(__file__).parent / "fixtures" / "template_recipes"
VERDICTS = json.loads((FIXTURES / "verdicts.json").read_text())


@pytest.mark.parametrize("name", sorted(VERDICTS))
def test_fixture_verdicts(name):
    recipe = json.loads((FIXTURES / name).read_text())
    expected = VERDICTS[name]
    if expected is None:
        assert validate_recipe(recipe)["v"] == 1
        return
    with pytest.raises(RecipeError) as caught:
        validate_recipe(recipe)
    assert (caught.value.code, caught.value.field) == (expected["code"], expected["field"])


def _skill(i: int, content: str = "Do it.") -> dict:
    return {"slug": f"skill-{i}", "name": f"Skill {i}", "description": "A skill.", "content": content}


def test_every_fixture_has_a_verdict():
    assert sorted(p.name for p in FIXTURES.glob("*.json") if p.name != "verdicts.json") == sorted(VERDICTS)


def test_skill_count_boundary():
    base = {"v": 1, "profile": {"displayName": "A"}}
    assert len(validate_recipe({**base, "skills": [_skill(i) for i in range(MAX_SKILLS)]})["skills"]) == MAX_SKILLS
    with pytest.raises(RecipeError) as caught:
        validate_recipe({**base, "skills": [_skill(i) for i in range(MAX_SKILLS + 1)]})
    assert (caught.value.code, caught.value.field) == ("BAD_RECIPE", "skills")


def test_skill_size_boundary():
    base = {"v": 1, "profile": {"displayName": "A"}}
    validate_recipe({**base, "skills": [_skill(0, "x" * MAX_SKILL_BYTES)]})
    with pytest.raises(RecipeError) as caught:
        validate_recipe({**base, "skills": [_skill(0, "x" * (MAX_SKILL_BYTES + 1))]})
    assert caught.value.field == "skills[0].content"


def test_whole_recipe_size_boundary():
    # Twenty skills of 27 000 bytes each pass every per-field limit; together they
    # are over the recipe's limit.
    recipe = {"v": 1, "profile": {"displayName": "A"}, "skills": [_skill(i, "y" * 27_000) for i in range(MAX_SKILLS)]}
    assert len(recipe_bytes(recipe)) > MAX_RECIPE_BYTES
    with pytest.raises(RecipeError) as caught:
        validate_recipe(recipe)
    assert (caught.value.code, caught.value.field) == ("TOO_LARGE", "")

    fits = {"v": 1, "profile": {"displayName": "A"}, "skills": [_skill(i, "y" * 25_000) for i in range(MAX_SKILLS)]}
    assert len(recipe_bytes(fits)) <= MAX_RECIPE_BYTES
    validate_recipe(fits)


def test_text_is_trimmed_and_defaults_filled():
    recipe = validate_recipe({"v": 1, "profile": {"displayName": "  Researcher  ", "markTone": " SKY "}})
    assert recipe["profile"] == {"displayName": "Researcher", "description": "", "soul": "",
                                 "markShape": "", "markTone": "sky"}
    assert recipe["skills"] == recipe["routines"] == recipe["apps"] == recipe["memories"] == []


def test_not_an_object():
    for value in (None, [], "recipe", 1):
        with pytest.raises(RecipeError) as caught:
            validate_recipe(value)
        assert caught.value.field == ""
