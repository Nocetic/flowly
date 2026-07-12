"""The authoring loop: deterministic lint + synthetic preview.

Lint speaks to what the agent WROTE (advisory, warn-first); the preview resolves
the flowlet against edge-aware sample rows so the shape is visible before a user
sees it. Both are best-effort and must never raise.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flowly.flowlets.lint import lint_definition
from flowly.flowlets.synth import preview_values, synth_rows

UTC = timezone.utc
NOW = int(datetime(2026, 7, 12, 12, tzinfo=UTC).timestamp() * 1000)


def _ids(defn: dict) -> set[str]:
    return {f["id"] for f in lint_definition(defn)}


_EXPENSE_ITEM = {"title": "string", "amount": "number", "category": "string",
                 "date": "date"}


def _list_defn(repeater: dict, *, extra=None, state_extra=None) -> dict:
    state = {"expenses": {"type": "list", "item": _EXPENSE_ITEM}}
    state.update(state_extra or {})
    return {"catalog": 3, "name": "H", "state": state,
            "layout": [repeater, *(extra or [])]}


# ── L01 / L07 — hand-rolled + tall rows ───────────────────────────────────────

def test_l01_flags_a_hand_rolled_display_row():
    d = _list_defn({"type": "repeater", "source": "expenses", "empty": "boş",
                    "item": {"type": "row", "children": [
                        {"type": "text", "text": "{$.title}"},
                        {"type": "badge", "text": "{$.category}"}]}})
    assert "L01" in _ids(d)


def test_list_row_is_not_flagged_l01():
    d = _list_defn({"type": "repeater", "source": "expenses", "empty": "boş",
                    "item": {"type": "list_row", "title": "$.title",
                             "value": "{$.amount} ₺"}})
    assert "L01" not in _ids(d)


def test_a_row_with_a_control_is_not_l01():
    d = {"catalog": 1, "name": "T",
         "state": {"tasks": {"type": "list", "item": {"title": "string", "done": "bool"}}},
         "layout": [
            {"id": "add", "type": "input", "action": {"op": "item_add", "key": "tasks"}},
            {"type": "repeater", "source": "tasks", "empty": "-", "item": {
                "type": "row", "children": [
                    {"id": "t", "type": "toggle", "value": "$.done",
                     "action": {"op": "item_toggle", "key": "tasks", "field": "done"}},
                    {"type": "text", "text": "{$.title}"}]}}]}
    assert "L01" not in _ids(d)


def test_l07_flags_more_than_two_text_lines():
    d = _list_defn({"type": "repeater", "source": "expenses", "empty": "boş",
                    "item": {"type": "row", "children": [
                        {"type": "text", "text": "{$.title}"},
                        {"type": "text", "text": "{$.category}"},
                        {"type": "text", "text": "{$.date}"}]}})
    assert "L07" in _ids(d)


# ── L04 — no empty copy ───────────────────────────────────────────────────────

def test_l04_flags_a_repeater_without_empty():
    d = _list_defn({"type": "repeater", "source": "expenses",
                    "item": {"type": "list_row", "title": "$.title"}})
    assert "L04" in _ids(d)


# ── L06 — a list nobody can add to ────────────────────────────────────────────

def test_l06_flags_an_unaddable_shown_list():
    d = _list_defn({"type": "repeater", "source": "expenses", "empty": "boş",
                    "item": {"type": "list_row", "title": "$.title"}})
    assert "L06" in _ids(d)


def test_a_form_satisfies_l06():
    d = _list_defn(
        {"type": "repeater", "source": "expenses", "empty": "boş",
         "item": {"type": "list_row", "title": "$.title"}},
        extra=[{"type": "form", "id": "add", "into": "expenses",
                "fields": [{"field": "title"}]}])
    assert "L06" not in _ids(d)


# ── L02 — chart on a shadow series ────────────────────────────────────────────

def test_l02_flags_a_chart_on_a_shadow_series():
    d = {"catalog": 3, "name": "H", "series": {"spend": {}},
         "state": {"expenses": {"type": "list", "item": _EXPENSE_ITEM},
                   "dc": {"type": "string", "default": "Market"}},
         "layout": [
            {"id": "amt", "type": "number_input", "action": {"op": "batch", "ops": [
                {"op": "item_add", "key": "expenses", "fields": {"amount": "{value}"}},
                {"op": "log", "series": "spend", "value": "{value}"}]}},
            {"id": "c", "type": "chart", "kind": "bar",
             "data": {"series": "spend", "bucket": "day", "window": "30d"}}]}
    assert "L02" in _ids(d)


# ── L03 — undeclared token in display copy ────────────────────────────────────

def test_l03_flags_an_undeclared_display_token():
    d = {"catalog": 1, "name": "H", "state": {"goal": {"type": "number", "default": 8}},
         "layout": [{"type": "header", "text": "Hedef: {gaol}"}]}     # typo
    assert "L03" in _ids(d)


def test_a_declared_token_is_not_flagged():
    d = {"catalog": 1, "name": "H", "state": {"goal": {"type": "number", "default": 8}},
         "layout": [{"type": "header", "text": "Hedef: {goal}"}]}
    assert "L03" not in _ids(d)


def test_field_token_in_a_repeater_row_is_not_l03():
    d = _list_defn({"type": "repeater", "source": "expenses", "empty": "boş",
                    "item": {"type": "list_row", "title": "$.title"}},
                   extra=[{"type": "form", "id": "a", "into": "expenses",
                           "fields": [{"field": "title"}]}])
    # a `{$.field}` inside the row template must not trip L03
    d["layout"][0]["item"] = {"type": "row", "children": [
        {"type": "text", "text": "{$.title}"}]}
    assert "L03" not in _ids(d)


# ── L10 — unused state key ────────────────────────────────────────────────────

def test_l10_flags_an_unused_state_key():
    d = {"catalog": 1, "name": "H",
         "state": {"used": {"type": "number", "default": 1},
                   "orphan": {"type": "number", "default": 0}},
         "layout": [{"type": "stat", "value": "used"}]}
    ids = _ids(d)
    assert "L10" in ids
    assert any("orphan" in f["message"] for f in lint_definition(d))


def test_a_used_key_is_not_l10():
    d = {"catalog": 1, "name": "H", "state": {"g": {"type": "number", "default": 1}},
         "layout": [{"type": "stat", "value": "g"}]}
    assert "L10" not in _ids(d)


# ── L11 — unreachable screen ──────────────────────────────────────────────────

def test_l11_flags_an_unreachable_screen():
    d = _list_defn(
        {"type": "repeater", "source": "expenses", "empty": "boş",
         "item": {"type": "list_row", "title": "$.title"}},
        extra=[{"type": "form", "id": "a", "into": "expenses",
                "fields": [{"field": "title"}]}])
    d["screens"] = {"orphanScreen": {"layout": [{"type": "text", "text": "hi"}]}}
    assert "L11" in _ids(d)


def test_a_navigated_screen_is_not_l11():
    d = _list_defn(
        {"type": "repeater", "source": "expenses", "empty": "boş",
         "navigate": "detail",
         "item": {"type": "list_row", "title": "$.title"}},
        extra=[{"type": "form", "id": "a", "into": "expenses",
                "fields": [{"field": "title"}]}])
    d["screens"] = {"detail": {"layout": [{"type": "text", "text": "{$.title}"}]}}
    assert "L11" not in _ids(d)


def test_lint_never_raises_on_junk():
    assert lint_definition({}) == []
    assert lint_definition({"layout": "not a list"}) == []


# ── synthetic preview ─────────────────────────────────────────────────────────

def test_synth_rows_are_edge_aware():
    rows = synth_rows(_EXPENSE_ITEM, NOW, UTC)
    assert len(rows) == 3
    assert all("id" in r for r in rows)
    assert len(rows[0]["title"]) > 30                 # row 0: a long title
    assert rows[2]["category"] == ""                  # last row: empty optional
    # dates span today + past (windowing edges)
    assert rows[0]["date"] == "2026-07-12"
    assert rows[2]["date"] == "2026-06-02"
    assert isinstance(rows[0]["amount"], float)


def test_preview_injects_rows_into_empty_lists():
    d = _list_defn(
        {"type": "repeater", "source": "expenses", "empty": "boş",
         "item": {"type": "list_row", "title": "$.title"}},
        extra=[{"type": "tracker_card", "id": "sp", "list": "expenses",
                "field": "amount", "chart": "bar", "window": "all"}])
    vals = preview_values(d, NOW, UTC)
    assert isinstance(vals["expenses"], list) and len(vals["expenses"]) == 3
    # the tracker's injected computed aggregates the synthetic rows
    assert vals["sp__agg"] == 1234.5 + 200.0 + 0.0


def test_preview_never_raises_on_junk():
    assert preview_values({"state": "bad"}, NOW, UTC) == {}


# ── tool wiring: create/update return the review ──────────────────────────────

async def test_create_returns_lint_and_preview(store):
    import json

    from flowly.agent.tools.flowlet import FlowletTool

    tool = FlowletTool(store)
    # a hand-rolled row over an unaddable list → L01 + L06; the preview resolves
    # the synthetic rows.
    defn = _list_defn({"type": "repeater", "source": "expenses", "empty": "boş",
                       "item": {"type": "row", "children": [
                           {"type": "text", "text": "{$.title}"},
                           {"type": "badge", "text": "{$.category}"}]}})
    res = json.loads(await tool.execute("create", definition=defn))
    assert res["action"] == "create"
    rule_ids = {f["id"] for f in res["lint"]}
    assert "L01" in rule_ids and "L06" in rule_ids
    assert isinstance(res["preview"]["expenses"], list)


async def test_clean_flowlet_has_no_lint(store):
    import json

    from flowly.agent.tools.flowlet import FlowletTool

    tool = FlowletTool(store)
    defn = _list_defn(
        {"type": "repeater", "source": "expenses", "empty": "Henüz harcama yok",
         "item": {"type": "list_row", "title": "$.title", "value": "{$.amount} ₺"}},
        extra=[{"type": "form", "id": "add", "into": "expenses",
                "fields": [{"field": "title"}, {"field": "amount"}]}])
    res = json.loads(await tool.execute("create", definition=defn))
    assert "lint" not in res            # nothing to warn about
    assert "preview" in res
