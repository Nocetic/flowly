"""Bundled flowlet templates — every one must be a definition the system would
have accepted from the agent, in every language the apps ship.

A template ships with the product, so a broken one can't be fixed by rewording a
prompt: it has to be caught here.
"""

from __future__ import annotations

import pytest

from flowly.flowlets.composites import expand_composites
from flowly.flowlets.lint import lint_definition
from flowly.flowlets.normalize import assign_missing_ids
from flowly.flowlets.schema import validate_definition
from flowly.flowlets.templates import (
    LANGS,
    TEMPLATES,
    build_template,
    list_templates,
    normalize_lang,
)


@pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
@pytest.mark.parametrize("lang", LANGS)
def test_template_validates_expands_and_lints_clean(template_id, lang):
    defn = assign_missing_ids(build_template(template_id, lang))
    validate_definition(defn)
    # The serve-time transform must survive too — composites expand to v2
    # primitives before any client ever sees them.
    validate_definition(expand_composites(defn))
    assert lint_definition(defn) == []


@pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
def test_template_carries_its_card_metadata(template_id):
    for lang in LANGS:
        defn = build_template(template_id, lang)
        card = next(c for c in list_templates(lang) if c["id"] == template_id)
        # The card a user picks and the flowlet they get can't disagree.
        assert defn["name"] == card["title"]
        assert defn["icon"] == card["icon"]
        assert defn["accent"] == card["accent"]
        assert defn["catalog"] == 3


def test_icons_are_real_catalog_names():
    """`validate_definition` doesn't police icon names, and both clients fall
    back to a neutral dot for one they don't know — so a typo here ships as a
    blank card that nothing else would have caught."""
    from flowly.flowlets.catalog import ICON_NAMES

    for t in TEMPLATES:
        assert t.icon in ICON_NAMES, f"{t.id}: '{t.icon}' is not a catalog icon"


def test_ids_are_stable_and_unique():
    # Clients key their picker off these; renaming one is a breaking change, so
    # it should take a deliberate edit here.
    assert [t.id for t in TEMPLATES] == [
        "water", "habits", "expenses", "tasks", "sleep", "mood", "meals",
    ]


@pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
def test_template_is_a_furnished_screen(template_id):
    """A template is someone's first impression of what a flowlet can be, so a
    bare control and a line of text isn't good enough — it needs a headline to
    land on, something that reacts, and history underneath."""
    defn = build_template(template_id)

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("type"), str):
                yield node
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    kinds = [n["type"] for n in walk(defn["layout"])]
    assert len(kinds) >= 10, f"{template_id} is thin: {kinds}"
    # A number the eye lands on first…
    assert {"ring", "progress", "stat", "metric", "tracker_card"} & set(kinds)
    # …something that acts on it…
    assert {"button", "form", "photo", "rating", "checklist",
            "number_input", "toggle"} & set(kinds)
    # …and history under it.
    assert {"chart", "heatmap", "sparkline", "repeater", "tracker_card"} & set(kinds)
    # Derived values, not numbers the agent would have to keep in sync by hand.
    assert len(defn.get("computed") or {}) >= 3


def test_cards_are_localized_and_complete():
    for lang in LANGS:
        cards = list_templates(lang)
        assert len(cards) == len(TEMPLATES)
        for c in cards:
            assert c["title"] and c["description"]
            assert c["icon"] and c["accent"].startswith("#")
    # Not the same string in every language (a missing translation is a bug).
    titles = {lang: list_templates(lang)[0]["title"] for lang in LANGS}
    assert len(set(titles.values())) == len(LANGS)


def test_definition_text_follows_the_language():
    tr = build_template("water", "tr")
    en = build_template("water", "en")
    assert tr["name"] == "Su Takibi"
    assert en["name"] == "Water"
    assert tr != en


def test_normalize_lang_handles_client_locales():
    assert normalize_lang("tr-TR") == "tr"
    assert normalize_lang("ES") == "es"
    assert normalize_lang("en_US") == "en"
    for junk in (None, "", "de", "zz-ZZ"):
        assert normalize_lang(junk) == "en"


def test_unknown_template_raises():
    with pytest.raises(KeyError):
        build_template("nope")


def test_every_template_pie_groups_its_rows():
    """A pie/donut over a list must group by a field; one using an unknown
    `groupBy` resolved as a time series and showed "No data yet"."""
    from flowly.flowlets.queries import resolve_values
    from flowly.flowlets.synth import synth_rows
    from flowly.flowlets.templates import TEMPLATES, build_template

    def charts(node):
        if isinstance(node, list):
            for n in node:
                yield from charts(n)
        elif isinstance(node, dict):
            if node.get("type") == "chart" and node.get("kind") in ("pie", "donut"):
                yield node
            yield from charts(node.get("children"))

    for tpl in TEMPLATES:
        d = build_template(tpl.id, "en")
        for chart in charts(d["layout"]):
            data = chart["data"]
            assert "by" in data, (tpl.id, chart)
            lst = data.get("list")
            state = {lst: synth_rows(d["state"][lst]["item"], 1_791_460_800_000, None)} if lst else {}
            slices = resolve_values(d, state, [], 1_791_460_800_000, None)[chart["id"]]
            assert slices and all("k" in s for s in slices), (tpl.id, slices)


def test_group_by_alias_is_repaired_or_rejected():
    import pytest as _pytest

    from flowly.flowlets.authoring import repair_definition
    from flowly.flowlets.schema import FlowletValidationError, validate_definition
    d = {"catalog": 3, "name": "X",
         "state": {"rows": {"type": "list", "item": {"cat": "string", "n": "number"}}},
         "layout": [{"type": "chart", "id": "c", "kind": "pie",
                     "data": {"list": "rows", "field": "n", "groupBy": "cat"}}]}
    with _pytest.raises(FlowletValidationError, match="`data.by`"):
        validate_definition(d)
    fixed, notes = repair_definition(d)
    assert fixed["layout"][0]["data"]["by"] == "cat" and notes
    validate_definition(fixed)
