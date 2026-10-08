"""A custom (non-template) screen should land in one call, or at worst one
retry: unambiguous slips are repaired and reported, and every independent
problem is returned together instead of one per paid round trip."""

from __future__ import annotations

import json

import pytest

from flowly.agent.tools.flowlet import FlowletTool
from flowly.flowlets.schema import FlowletValidationError, validate_definition
from flowly.flowlets.store import FlowletStore


@pytest.fixture
def store(tmp_path):
    return FlowletStore(tmp_path / "f.db")


def test_independent_semantic_problems_are_all_reported():
    defn = {
        "catalog": 3, "name": "Water",
        "state": {"ml": {"type": "number", "default": 0}, "bad": {"type": "nope"}},
        "layout": [
            {"type": "progress", "value": "ml", "max": "goal_ml"},       # unknown key
            {"type": "button", "id": "add", "text": "+250",
             "action": {"op": "increment", "key": "missing", "by": 250}},  # unknown key
            {"type": "card", "children": [
                {"type": "header"},                                        # missing text
                {"type": "text", "text": "x", "visibleWhen": "ml >"},      # bad grammar
            ]},
            {"type": "sparkle"},                                           # unknown type
        ],
    }
    with pytest.raises(FlowletValidationError) as info:
        validate_definition(defn)
    errors = info.value.errors
    joined = "\n".join(errors)
    assert len(errors) >= 6
    for needle in ("'bad'", "goal_ml", "missing", "`text`", "visibleWhen", "sparkle"):
        assert needle in joined, needle
    assert str(info.value) == joined  # one message, one line per problem


def test_reported_problems_are_bounded():
    defn = {"catalog": 3, "name": "Many",
            "layout": [{"type": f"nope{i}"} for i in range(40)]}
    with pytest.raises(FlowletValidationError) as info:
        validate_definition(defn)
    assert len(info.value.errors) == 12


async def test_a_typical_model_draft_with_slips_creates_in_one_call(store):
    draft = {
        "name": "Okuma Takibi", "icon": "book",
        "state": {"goal": {"type": "number", "default": 20},
                  "books": {"type": "list", "item": {"title": "string", "pages": "number",
                                                      "date": "date"}}},
        "computed": {"today_pages": {"list": "books", "field": "pages", "agg": "sum",
                                     "where": "days_since(date) == 0"},
                     "left": "max(0, goal - today_pages)"},
        "layout": {"type": "column", "children": [
            {"type": "heading", "title": "Bugün"},
            {"type": "progress_bar", "value": {"ref": "today_pages"}, "max": {"ref": "goal"}},
            {"type": "text", "label": "{left} sayfa kaldı"},
            {"type": "form", "id": "add", "into": "books", "fields": [
                {"field": "title"}, {"field": "pages"}, {"field": "date", "default": "today"}]},
        ]},
    }
    result = json.loads(await FlowletTool(store).execute("create", definition=draft, pinned=True))
    assert "error" not in result, result
    assert result["flowlet"]["pinned"] is True
    assert len(result["normalized"]) >= 6
    assert store.get(result["flowlet"]["id"])["definition"]["catalog"] == 3


async def test_the_contract_in_the_tool_description_is_itself_valid(store):
    """Every pattern the description teaches must create without a retry."""
    defn = {
        "catalog": 3, "name": "Meals", "icon": "camera", "locale": "en",
        "state": {"goal": {"type": "number", "default": 2000},
                  "rows": {"type": "list", "item": {"name": "string", "kcal": "number",
                                                     "date": "date", "shot": "image"}}},
        "series": {"water": {"unit": "ml"}},
        "computed": {
            "total": {"list": "rows", "field": "kcal", "agg": "sum", "where": "days_since(date)==0"},
            "left": {"expr": "max(0, goal-total)"},
            "mood": {"cases": [{"when": "total>goal", "text": "Over by {left}"}], "else": "On track"},
            "water_today": {"series": "water", "agg": "sum", "window": "today"},
        },
        "layout": [
            {"type": "stat", "value": "total"},
            {"type": "progress", "value": "total", "max": "goal"},
            {"type": "text", "text": "{left} left · {mood}"},
            {"type": "form", "id": "add", "into": "rows",
             "fields": [{"field": "name"}, {"field": "date", "default": "today"}]},
            {"type": "photo", "id": "snap", "label": "Photo",
             "action": {"op": "vision", "into": "rows", "prompt": "Estimate", "dateDefaults": ["date"]}},
            {"type": "button", "id": "more", "text": "+", "action": {"op": "increment", "key": "goal", "by": 100}},
            {"type": "button", "id": "drink", "text": "Drink", "action": {"op": "log", "series": "water", "value": 250}},
            {"type": "repeater", "source": "rows",
             "item": {"type": "list_row", "title": "$.name", "value": "{$.kcal} kcal", "thumb": "$.shot"}},
            {"type": "tracker_card", "id": "week", "list": "rows", "field": "kcal",
             "window": "7d", "chart": "bar"},
        ],
        "watches": [{"id": "evening", "trigger": "schedule", "at": "21:00", "when": "total>0",
                     "notify": {"title": "Today", "body": "{total} today"},
                     "otherwise": {"title": "Today", "body": "Nothing yet"}},
                    {"id": "nudge", "trigger": "condition", "when": "total < goal", "after": "18:00",
                     "notify": {"title": "Goal", "body": "{left} to go"}}],
    }
    result = json.loads(await FlowletTool(store).execute("create", definition=defn))
    assert "error" not in result, result
    assert "normalized" not in result
