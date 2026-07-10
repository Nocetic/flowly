"""Serving-time photo-display normalization — a stored photo always shows."""

from __future__ import annotations

import copy

from flowly.flowlets.normalize import ensure_photo_display
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
