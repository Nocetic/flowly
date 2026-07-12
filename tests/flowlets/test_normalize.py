"""Serving-time photo-display normalization — a stored photo always shows."""

from __future__ import annotations

import copy

from flowly.flowlets.normalize import (
    ensure_chart_layout,
    ensure_editable_drill,
    ensure_photo_display,
)
from flowly.flowlets.schema import validate_definition


def _defn(*, item=None, tmpl=None, screens=None, navigate=None) -> dict:
    item = item or {"name": "string", "kcal": "number", "shot": "image"}
    rep = {"type": "repeater", "id": "list", "source": "meals",
           "item": tmpl or {"type": "text", "text": "{$.name}"}}
    if navigate:
        rep["navigate"] = navigate
    d = {
        "catalog": 2, "name": "Kalori",
        "state": {"meals": {"type": "list", "item": item}},
        "layout": [
            {"type": "photo", "id": "add",
             "action": {"op": "vision", "prompt": "meal", "into": "meals"}},
            rep,
        ],
    }
    if screens:
        d["screens"] = screens
    return d


def _find_repeater(d):
    return next(n for n in d["layout"] if n["type"] == "repeater")


def test_injects_thumbnail_wrapping_a_plain_template():
    d = _defn()
    out = ensure_photo_display(d)
    tmpl = _find_repeater(out)["item"]
    assert tmpl["type"] == "row"
    assert tmpl["children"][0] == {"type": "image", "src": "$.shot", "height": 44}
    assert tmpl["children"][1] == {"type": "text", "text": "{$.name}"}


def test_injects_thumbnail_into_an_existing_row():
    d = _defn(tmpl={"type": "row", "children": [{"type": "text", "text": "{$.name}"}]})
    out = ensure_photo_display(d)
    kids = _find_repeater(out)["item"]["children"]
    assert kids[0]["type"] == "image" and kids[0]["src"] == "$.shot"
    assert len(kids) == 2


def test_author_placed_image_wins():
    tmpl = {"type": "row", "children": [
        {"type": "image", "src": "$.shot", "height": 60},
        {"type": "text", "text": "{$.name}"},
    ]}
    d = _defn(tmpl=tmpl)
    out = ensure_photo_display(d)
    assert out is d  # nothing to add → the ORIGINAL object comes back


def test_no_image_field_is_a_cheap_noop():
    d = _defn(item={"name": "string", "kcal": "number"})
    assert ensure_photo_display(d) is d


def test_stored_definition_never_mutated():
    d = _defn()
    snapshot = copy.deepcopy(d)
    ensure_photo_display(d)
    assert d == snapshot


def test_screen_gets_a_full_photo():
    d = _defn(navigate="meal",
              screens={"meal": {"title": "{$.name}", "layout": [
                  {"type": "stat", "value": "$.kcal", "label": "kcal"}]}})
    out = ensure_photo_display(d)
    assert out["screens"]["meal"]["layout"][0] == {"type": "image", "src": "$.shot"}


def test_screen_with_its_own_photo_untouched():
    d = _defn(navigate="meal",
              screens={"meal": {"layout": [{"type": "image", "src": "$.shot"}]}})
    out = ensure_photo_display(d)
    assert len(out["screens"]["meal"]["layout"]) == 1


def test_normalized_definition_still_validates():
    d = _defn(navigate="meal",
              screens={"meal": {"layout": [
                  {"type": "stat", "value": "$.kcal", "label": "kcal"}]}})
    validate_definition(ensure_photo_display(d))


# ── editable drill guarantee ──────────────────────────────────────────────────


def _edit_ids(layout):
    return [n.get("id") for n in layout if isinstance(n, dict) and n.get("action")]


def test_injects_edit_inputs_into_existing_drill_screen():
    # A read-only drill screen (stat only) → editable name + kcal appended
    # (shot is an image, skipped).
    d = _defn(navigate="meal",
              screens={"meal": {"title": "{$.name}", "layout": [
                  {"type": "stat", "value": "$.kcal", "label": "kcal"}]}})
    out = ensure_editable_drill(d)
    layout = out["screens"]["meal"]["layout"]
    added = [n for n in layout if isinstance(n, dict) and n.get("id", "").startswith("edit_")]
    by_field = {n["action"]["field"]: n for n in added}
    assert set(by_field) == {"name", "kcal"}
    assert by_field["name"]["type"] == "input"
    assert by_field["name"]["value"] == "$.name"
    assert by_field["name"]["action"] == {"op": "item_update", "key": "meals", "field": "name"}
    assert by_field["kcal"]["type"] == "number_input"


def test_author_placed_edit_input_wins_only_gaps_filled():
    # Author already edits kcal → only `name` is injected, kcal is left alone.
    d = _defn(navigate="meal",
              screens={"meal": {"layout": [
                  {"type": "number_input", "id": "myKcal", "value": "$.kcal",
                   "action": {"op": "item_update", "key": "meals", "field": "kcal"}}]}})
    out = ensure_editable_drill(d)
    added = [n for n in out["screens"]["meal"]["layout"]
             if isinstance(n, dict) and n.get("id", "").startswith("edit_")]
    assert {n["action"]["field"] for n in added} == {"name"}


def test_fully_covered_screen_is_untouched():
    d = _defn(item={"name": "string", "kcal": "number"}, navigate="meal",
              screens={"meal": {"layout": [
                  {"type": "input", "id": "n", "action": {"op": "item_update", "key": "meals", "field": "name"}},
                  {"type": "number_input", "id": "k", "action": {"op": "item_update", "key": "meals", "field": "kcal"}}]}})
    assert ensure_editable_drill(d) is d


def test_synthesizes_a_drill_screen_when_none_exists():
    d = _defn()  # repeater has no `navigate`, no screens
    out = ensure_editable_drill(d)
    rep = _find_repeater(out)
    sid = rep["navigate"]
    assert sid == "edit_meals"
    screen = out["screens"][sid]
    assert screen["title"] == "{$.name}"          # first string field
    fields = {n["action"]["field"] for n in screen["layout"]}
    assert fields == {"name", "kcal"}             # shot (image) excluded


def test_bool_and_date_fields_get_the_right_controls():
    d = _defn(item={"title": "string", "done": "bool", "due": "date"})
    out = ensure_editable_drill(d)
    layout = out["screens"]["edit_meals"]["layout"]
    by_field = {n["action"]["field"]: n for n in layout}
    assert by_field["done"]["type"] == "toggle"
    assert by_field["done"]["action"]["op"] == "item_toggle"
    assert by_field["due"]["type"] == "date"
    assert by_field["due"]["action"]["op"] == "item_update"


def test_source_owned_list_is_never_made_editable():
    d = {
        "catalog": 2, "name": "Commits",
        "state": {"commits": {"type": "list", "source": True,
                              "item": {"title": "string"}}},
        "layout": [{"type": "repeater", "id": "l", "source": "commits",
                    "item": {"type": "text", "text": "{$.title}"}}],
    }
    assert ensure_editable_drill(d) is d


def test_no_mutable_list_is_a_cheap_noop():
    d = {"catalog": 2, "name": "x", "state": {"n": {"type": "number"}},
         "layout": [{"type": "stat", "value": "n"}]}
    assert ensure_editable_drill(d) is d


def test_stored_definition_never_mutated_by_editable():
    d = _defn(navigate="meal",
              screens={"meal": {"layout": [{"type": "stat", "value": "$.kcal"}]}})
    snapshot = copy.deepcopy(d)
    ensure_editable_drill(d)
    assert d == snapshot


def test_synthesized_screen_respects_max_screens():
    # 6 existing screens (the cap) → no synthesis, repeater stays nav-less.
    screens = {f"s{i}": {"layout": [{"type": "text", "text": "x"}]} for i in range(6)}
    d = _defn(item={"name": "string"}, screens=screens)
    out = ensure_editable_drill(d)
    # Unchanged (no room to synthesize) → original object back.
    assert out is d


def test_composed_editable_then_photo_validates_and_edits():
    # The real serve composition: edit guarantee + photo display, on a flowlet
    # with an image list and NO drill screen. Must validate and be editable.
    d = _defn()
    out = ensure_photo_display(ensure_editable_drill(d))
    validate_definition(out)
    rep = _find_repeater(out)
    screen = out["screens"][rep["navigate"]]
    # full photo at the top (from the photo pass) + edit inputs
    assert screen["layout"][0] == {"type": "image", "src": "$.shot"}
    assert any(n.get("action", {}).get("op") == "item_update" for n in screen["layout"])


# ── chart layout (chart grids go full-width) ──────────────────────────────────

def _grid(columns, *children):
    return {"catalog": 2, "name": "X",
            "layout": [{"type": "grid", "columns": columns, "children": list(children)}]}


def test_grid_with_a_chart_is_forced_to_one_column():
    d = _grid(2,
              {"type": "card", "children": [{"type": "chart", "id": "a", "kind": "bar",
                                             "data": {"series": "s"}}]},
              {"type": "card", "children": [{"type": "chart", "id": "b", "kind": "donut",
                                             "data": {"series": "s", "by": "category"}}]})
    out = ensure_chart_layout(d)
    assert out["layout"][0]["columns"] == 1


def test_grid_without_a_chart_is_untouched():
    d = _grid(2, {"type": "stat", "value": "x"}, {"type": "stat", "value": "y"})
    assert ensure_chart_layout(d) is d           # no chart → cheap no-op


def test_single_column_chart_grid_is_a_noop():
    d = _grid(1, {"type": "chart", "id": "a", "kind": "bar", "data": {"series": "s"}})
    assert ensure_chart_layout(d) is d


def test_chart_layout_is_idempotent_and_non_mutating():
    import copy as _c
    d = _grid(2, {"type": "chart", "id": "a", "kind": "bar", "data": {"series": "s"}})
    snap = _c.deepcopy(d)
    once = ensure_chart_layout(d)
    assert d == snap                             # original untouched
    assert ensure_chart_layout(once) is once     # already 1-col → no second copy
