"""User-visible creation contract: one call, no skill/file exploration."""

import copy
import json
from pathlib import Path

import pytest

from flowly.agent.tools.flowlet import FlowletTool
from flowly.flowlets.authoring import structural_errors
from flowly.flowlets.schema import validate_definition
from flowly.flowlets.templates import LANGS, TEMPLATES


@pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
@pytest.mark.parametrize("lang", LANGS)
async def test_one_call_creates_valid_localized_pinned_screen(store, template_id, lang):
    events = []

    async def broadcast(event, data):
        events.append((event, data))

    tool = FlowletTool(store, broadcast)
    tool.set_context("web", "chat-1")
    result = json.loads(await tool.execute("create", template_id=template_id, lang=lang, pinned=True))
    assert "error" not in result
    assert not result.get("lint")
    saved = store.get(result["flowlet"]["id"])
    validate_definition(saved["definition"])
    assert saved["pinned"] and saved["origin_session"] == "web:chat-1"
    assert not saved["definition"].get("watches")
    assert [e for e, _ in events] == ["flowlet.created"]
    for key, spec in saved["definition"].get("state", {}).items():
        if spec["type"] == "list":
            assert result["flowlet"]["values"][key] == []  # preview never seeds real data


async def test_meal_summary_created_atomically_with_screen(store):
    watch = {"id": "evening", "trigger": "schedule", "at": "21:00",
             "notify": {"title": "Günlük özet", "body": "{today_kcal} kcal; {meal_count} öğün"}}
    result = json.loads(await FlowletTool(store).execute(
        "create", template_id="meals", lang="tr", watches=[watch], name="Öğünlerim"))
    saved = store.get(result["flowlet"]["id"])
    assert saved["name"] == "Öğünlerim"
    assert saved["definition"]["watches"] == [watch]
    result = json.loads(await FlowletTool(store).execute(
        "create", template_id="meals", watches=[{**watch, "at": "25:00"}]))
    assert "error" in result
    assert len(store.list()) == 1


async def test_template_and_guide_are_read_only_and_fit_result_budget(store):
    tool = FlowletTool(store)
    guide = await tool.execute("guide")
    assert len(guide) < 8000
    assert "photo" in json.loads(guide)["components"]
    draft = json.loads(await tool.execute("template", template_id="meals", lang="tr"))
    assert draft["definition"]["name"] == "Kalori Takibim"
    assert json.loads(await tool.execute("validate", **draft))["valid"]
    assert store.list() == []


@pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
async def test_template_responses_never_require_followup_file_reads(store, template_id):
    tool = FlowletTool(store)
    for lang in LANGS:
        response = await tool.execute("template", template_id=template_id, lang=lang)
        assert len(response) < 8000
        assert "definition" in json.loads(response)


def test_oversized_draft_stops_before_detailed_preflight():
    errors = structural_errors({"layout": [{"type": "text", "text": "x" * 70000}]})
    assert len(errors) == 1 and "bytes" in errors[0]["message"]


async def test_reject_ambiguous_create_without_writing(store):
    tool = FlowletTool(store)
    for args in ({"template_id": "missing"},
                 {"template_id": "meals", "definition": {}},
                 {"definition": {}, "watches": []}):
        assert "error" in json.loads(await tool.execute("create", **args))
    assert not store.list()


async def test_screenshot_errors_reported_together_without_mutation(store):
    broken = {"catalog": "3", "name": "Calories", "lists": {},
              "state": {"meals": {"type": "list", "item": {"kcal": "number"}, "max": 1000}},
              "computed": {"remaining": {"expr": {"subtract": [2200, "total"]}}},
              "layout": [{"type": "header", "title": "Calories"},
                         {"type": "progress", "value": {"ref": "total"}},
                         {"type": "photo_input"}]}
    original = copy.deepcopy(broken)
    result = json.loads(await FlowletTool(store).execute("create", definition=broken))
    # Slips with one reading are repaired and reported, not bounced back …
    normalized = " | ".join(result["normalized"])
    for fixed in ("catalog: string → integer", "`title` → `text`",
                  "{ref: …} → key string", "'photo_input' → 'photo'"):
        assert fixed in normalized
    # … and every genuinely ambiguous problem comes back in the same reply.
    paths = {e.split(":", 1)[0] for e in result["errors"]}
    assert {"$.lists", "$.state.meals.max", "$.computed.remaining.expr"} <= paths
    assert broken == original
    assert not store.list()


def test_layout_object_is_reported_with_other_errors():
    errors = structural_errors({"name": "Broken", "layout": {"type": "header"}})
    assert len(errors) == 2


def test_quick_start_fits_even_file_read_budget():
    path = Path(__file__).parents[2] / "flowly/skills/flowlets/SKILL.md"
    body = path.read_text()
    assert len(body) < 4000
    assert "template_id" in body and "action=\"guide\"" in body
    assert (path.parent / "references/catalog.md").is_file()
