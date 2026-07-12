"""Composite components → primitive expansion (catalog 3).

A composite states INTENT ("this field is the row's title", "add rows into this
list", "track this list's total") and the bot expands it to the v2 primitives
the renderers already draw, so the system — not the agent — owns layout and
wiring. This is the structural half of the "agent'a kalmasın" doctrine: the
agent can no longer mis-lay-out a list row, mis-wire a multi-field form, or
drift a tracker's chart from its list, because it never authors those internals.

Expansion is a pure, deterministic, idempotent transform on a deep copy. A
composite may inject its own top-level ``state``/``computed`` (a form's draft
keys, a tracker's aggregate) — collected while walking and merged at the end. A
definition with no composite is returned unchanged (no copy). It runs at serve
time INNERMOST, and at the top of value resolution + action application, so
every consumer sees plain primitives with all refs declared.

Invariant (asserted in tests): ``validate_definition(expand_composites(d))``
always passes — the expansion only ever emits declared refs.
"""

from __future__ import annotations

import copy
from typing import Any

from flowly.flowlets import catalog

_THUMB_HEIGHT = 44
#: ≤ this many options → a segmented pill row; more → a dropdown select.
_SEGMENTED_MAX = 4


# ── prop → template helpers ───────────────────────────────────────────────────


def _text_template(v: Any) -> str | None:
    """A composite prop → the string a ``text`` node should show.

    ``"$.title"`` → ``"{$.title}"`` (interpolated); ``"{$.amount} ₺"`` → as-is;
    a bare literal → itself. ``None``/non-string → None (sub-node omitted)."""
    if not isinstance(v, str) or not v:
        return None
    if "{" in v:
        return v
    if v.startswith("$."):
        return "{" + v + "}"
    return v


def _field_ref(v: Any) -> str | None:
    """A composite prop → an ``image`` ``src`` ref (a raw ``$.field``)."""
    if not isinstance(v, str) or not v:
        return None
    inner = v.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()
    return inner if inner.startswith("$.") else None


# ── list_row ──────────────────────────────────────────────────────────────────


def _expand_list_row(node: dict, injected: dict) -> dict:
    """A ``list_row`` → the canonical repeater-row primitive tree.

    ``[thumb]  title / subtitle(muted)  ……  [badge] value  ›``

    The leading text block is a greedy ``column`` (``grow: true``) so the
    trailing badge+value cluster pins right; the client's row renderer mutes the
    subtitle and appends the chevron when the repeater navigates. Absent props
    drop their sub-node — a title-only row collapses to a single greedy text.
    """
    children: list[dict] = []

    thumb = _field_ref(node.get("thumb"))
    if thumb:
        children.append({"type": "image", "src": thumb, "height": _THUMB_HEIGHT})

    title = _text_template(node.get("title"))
    subtitle = _text_template(node.get("subtitle"))
    lead_texts = [{"type": "text", "text": t} for t in (title, subtitle) if t]
    if len(lead_texts) == 1 and not thumb:
        children.append(lead_texts[0])
    elif lead_texts:
        children.append({
            "type": "column", "grow": True,
            "children": [{**t, "grow": False} for t in lead_texts],
        })

    badge = _text_template(node.get("badge"))
    if badge:
        children.append({"type": "badge", "text": badge})

    value = _text_template(node.get("value"))
    if value:
        children.append({"type": "text", "text": value, "grow": False})

    row: dict = {"type": "row", "children": children}
    if isinstance(node.get("visibleWhen"), str):
        row["visibleWhen"] = node["visibleWhen"]
    return row


# ── form ──────────────────────────────────────────────────────────────────────


def _draft_key(form_id: str, field: str) -> str:
    return f"{form_id}__{field}"


def _form_control(form_id: str, fspec: dict, ftype: str) -> dict:
    """One form field → its input control, bound to a per-form DRAFT state key
    (the client seeds a ``set`` input from its key). Options → segmented/select;
    else typed by the item field."""
    field = fspec["field"]
    draft = _draft_key(form_id, field)
    cid = f"{form_id}_{field}"
    label = fspec.get("label")
    base: dict = {"id": cid, "value": draft, "action": {"op": "set", "key": draft}}
    if label:
        base["label"] = label

    options = fspec.get("options")
    if isinstance(options, list) and options:
        base["options"] = options
        base["type"] = "segmented" if len(options) <= _SEGMENTED_MAX else "select"
        return base
    if ftype == "number":
        base["type"] = "number_input"
    elif ftype == "date":
        base["type"] = "date"
    elif ftype == "bool":
        base["type"] = "toggle"
        base["action"] = {"op": "toggle", "key": draft}
    else:
        base["type"] = "input"
    if fspec.get("placeholder"):
        base["placeholder"] = fspec["placeholder"]
    return base


def _expand_form(node: dict, injected: dict, item_schema: dict) -> dict:
    """A ``form`` → a card of inputs (each bound to a draft key) + a submit
    button that ``item_add``s the drafts into the list and resets them.

    Draft keys are injected into ``state`` (typed from the item schema). A date
    field defaulting to ``today`` seeds its draft with the literal ``"today"``,
    which ``item_add`` resolves to the current date at submit unless the user
    picked another (the draft then holds the pick). The reset after add readies
    the form for the next entry.
    """
    form_id = node["id"]
    into = node["into"]
    fields = node.get("fields") or []
    children: list[dict] = []

    if node.get("title"):
        children.append({"type": "header", "text": node["title"]})

    add_fields: dict[str, str] = {}
    resets: list[dict] = []
    for fspec in fields:
        field = fspec["field"]
        ftype = item_schema.get(field, "string")
        draft = _draft_key(form_id, field)
        # inject the draft state key (namespaced, typed)
        if ftype == "number":
            injected["state"][draft] = {"type": "number", "default": 0}
        elif ftype == "bool":
            injected["state"][draft] = {"type": "bool", "default": False}
        else:  # string / date drafts are strings
            default = ""
            if ftype == "date" and str(fspec.get("default", "")).lower() == "today":
                default = "today"
            elif isinstance(fspec.get("options"), list) and fspec["options"]:
                default = str(fspec["options"][0])  # first option, so nothing is unset
            injected["state"][draft] = {"type": "string", "default": default}
        children.append(_form_control(form_id, fspec, ftype))
        add_fields[field] = "{" + draft + "}"
        resets.append({"op": "reset", "key": draft})

    submit = node.get("submit") or {}
    children.append({
        "type": "button", "id": f"{form_id}_submit",
        "text": submit.get("label") or "Add", "style": "primary",
        "action": {"op": "batch", "ops": [
            {"op": "item_add", "key": into, "fields": add_fields},
            *resets,
        ]},
    })

    card: dict = {"type": "card", "children": children}
    if isinstance(node.get("visibleWhen"), str):
        card["visibleWhen"] = node["visibleWhen"]
    return card


# ── tracker_card ──────────────────────────────────────────────────────────────

#: Field names that read as a grouping category, best-first.
_CATEGORY_NAMES = ("category", "type", "kind", "tag", "status", "group", "label")


def _category_field(item_schema: dict) -> str | None:
    """The string field a breakdown should group by — a category-like name if
    present, else the first non-id string field."""
    strings = [f for f, t in item_schema.items() if t == "string" and f != "id"]
    for name in _CATEGORY_NAMES:
        if name in strings:
            return name
    return strings[0] if strings else None




def _expand_tracker_card(node: dict, injected: dict, item_schema: dict) -> dict:
    """A ``tracker_card`` → a card with an aggregate metric + a list-backed
    chart (+ an optional quick-add). The aggregate is injected as a ``computed``
    over the list, so the metric is a pure function of the rows."""
    tid = node["id"]
    list_key = node["list"]
    field = node.get("field")
    agg = node.get("agg") or ("sum" if field else "count")
    window = node.get("window", "30d")
    metric_key = f"{tid}__agg"

    comp: dict = {"list": list_key, "agg": agg, "window": window}
    if field:
        comp["field"] = field
    # window as a computed-list `where` over a date field, if one exists
    date_field = next((f for f, t in item_schema.items() if t == "date"), None)
    if date_field and window != "all":
        days = {"today": 1, "7d": 7, "30d": 31, "90d": 91}.get(window, 31)
        comp = {"list": list_key, "agg": agg,
                "where": f"days_since({date_field}) >= 0 and days_since({date_field}) < {days}"}
        if field:
            comp["field"] = field
    injected["computed"][metric_key] = comp

    children: list[dict] = [{
        "type": "metric", "value": metric_key,
        "label": node.get("title") or "",
    }]

    chart_kind = node.get("chart")
    if chart_kind:
        data: dict = {"list": list_key, "window": window}
        if field:
            data["field"] = field
        if chart_kind in ("pie", "donut"):
            cat = node.get("by") or _category_field(item_schema)
            if cat:
                data["by"] = cat
                data["agg"] = "sum" if field else "count"
        else:
            data["bucket"] = "day"
        children.append({"type": "chart", "id": f"{tid}_chart",
                         "kind": chart_kind, "data": data})

    card: dict = {"type": "card", "children": children}
    if isinstance(node.get("visibleWhen"), str):
        card["visibleWhen"] = node["visibleWhen"]
    return card


# ── walk / expand ─────────────────────────────────────────────────────────────

#: composite type → expander(node, injected, item_schema) -> primitive node.
#: item_schema is the enclosing repeater's fields (for list_row) or {} elsewhere.
_EXPANDERS = {
    "list_row": lambda n, inj, sch: _expand_list_row(n, inj),
    "form": lambda n, inj, sch: _expand_form(n, inj, sch),
    "tracker_card": lambda n, inj, sch: _expand_tracker_card(n, inj, sch),
}


def _has_composite(node: Any) -> bool:
    if isinstance(node, list):
        return any(_has_composite(n) for n in node)
    if not isinstance(node, dict):
        return False
    if node.get("type") in catalog.COMPOSITE_TYPES:
        return True
    if _has_composite(node.get("children")):
        return True
    item = node.get("item")
    return isinstance(item, dict) and _has_composite(item)


def _item_schema_of(node: dict, state: dict) -> dict:
    """The item field schema a form/tracker references (its `into`/`list`)."""
    key = node.get("into") or node.get("list")
    spec = state.get(key) if isinstance(key, str) else None
    return (spec.get("item") or {}) if isinstance(spec, dict) else {}


def _expand_node(node: Any, injected: dict, state: dict, item_schema: dict) -> Any:
    if isinstance(node, list):
        return [_expand_node(n, injected, state, item_schema) for n in node]
    if not isinstance(node, dict):
        return node
    expander = _EXPANDERS.get(node.get("type"))
    if expander is not None:
        schema = item_schema or _item_schema_of(node, state)
        node = expander(node, injected, schema)
    out = dict(node)
    if isinstance(out.get("children"), list):
        out["children"] = [_expand_node(c, injected, state, item_schema)
                           for c in out["children"]]
    item = out.get("item")
    if isinstance(item, dict):
        # a repeater opens the item scope for a list_row inside it
        src = out.get("source")
        sub_schema = (state.get(src, {}) or {}).get("item", {}) if isinstance(src, str) else {}
        out["item"] = _expand_node(item, injected, state, sub_schema)
    return out


def expand_composites(defn: dict) -> dict:
    """Return the definition with every composite expanded to primitives.

    Deterministic, idempotent, non-mutating. Returns the ORIGINAL object when
    the definition declares no composite (the common case — no copy, no walk).
    """
    if not _has_composite(defn.get("layout")) and not any(
        isinstance(s, dict) and _has_composite(s.get("layout"))
        for s in (defn.get("screens") or {}).values()
    ):
        return defn

    out = copy.deepcopy(defn)
    state = out.get("state") or {}
    injected: dict = {"state": {}, "computed": {}}

    if isinstance(out.get("layout"), list):
        out["layout"] = _expand_node(out["layout"], injected, state, {})
    for screen in (out.get("screens") or {}).values():
        if isinstance(screen, dict) and isinstance(screen.get("layout"), list):
            screen["layout"] = _expand_node(screen["layout"], injected, state, {})

    if injected["state"]:
        out.setdefault("state", {}).update(injected["state"])
    if injected["computed"]:
        out.setdefault("computed", {}).update(injected["computed"])
    return out
