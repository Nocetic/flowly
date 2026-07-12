"""Serving-time display normalization — a stored photo is ALWAYS visible.

If a list declares an ``image`` field, its photos must show regardless of what
the authoring agent remembered to include: when the definition has no ``image``
component bound to that field, we augment it ON SERVE (never persisted) —

* every repeater over that list gets a row thumbnail (``$.field``, 44 px), and
* that list's drill-down screen (if one is navigated to) gets a full-width
  photo at the top.

Deterministic, additive, idempotent; an author-placed ``image`` bound to the
field always wins (we only fill the gap). Data-bound tables are left alone —
they are columnar text by design.
"""

from __future__ import annotations

import copy
from typing import Any

from flowly.flowlets import catalog

_THUMB_HEIGHT = 44

#: Item-field types we can render an editable control for (image is shown, not
#: edited; the reserved `id` is never editable).
_EDITABLE_FIELD_TYPES = frozenset({"string", "number", "bool", "date"})


def _image_fields(defn: dict) -> dict[str, str]:
    """list state key → its first ``image``-typed item field."""
    out: dict[str, str] = {}
    for key, spec in (defn.get("state") or {}).items():
        if isinstance(spec, dict) and spec.get("type") == "list":
            for f, t in (spec.get("item") or {}).items():
                if t == "image":
                    out[key] = f
                    break
    return out


def _has_image_ref(node: Any, ref: str) -> bool:
    """Does this subtree contain an ``image`` bound to ``ref``?"""
    if not isinstance(node, dict):
        return False
    if node.get("type") == "image" and node.get("src") == ref:
        return True
    for child in node.get("children") or []:
        if _has_image_ref(child, ref):
            return True
    item = node.get("item")
    return isinstance(item, dict) and _has_image_ref(item, ref)


def ensure_photo_display(defn: dict) -> dict:
    """Return the definition, augmented so every image field is displayed.

    Works on a deep copy; the stored definition is never mutated. Returns the
    original object when the definition declares no image fields (the common
    case — no copy, no walk).
    """
    imgs = _image_fields(defn)
    if not imgs:
        return defn

    out = copy.deepcopy(defn)
    patched = False
    screen_lists: dict[str, str] = {}  # screenId → the list navigating to it

    def walk(nodes: Any) -> None:
        nonlocal patched
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            src = node.get("source")
            nav = node.get("navigate")
            if isinstance(nav, str) and isinstance(src, str) and src in imgs:
                screen_lists.setdefault(nav, src)
            if node.get("type") == "repeater" and isinstance(src, str):
                field = imgs.get(src)
                item = node.get("item")
                if field and isinstance(item, dict) and not _has_image_ref(item, f"$.{field}"):
                    thumb = {"type": "image", "src": f"$.{field}", "height": _THUMB_HEIGHT}
                    if item.get("type") == "row" and isinstance(item.get("children"), list):
                        item["children"].insert(0, thumb)
                    else:
                        node["item"] = {"type": "row", "children": [thumb, item]}
                    patched = True
            walk(node.get("children"))

    walk(out.get("layout"))

    for sid, screen in (out.get("screens") or {}).items():
        src = screen_lists.get(sid)
        field = imgs.get(src) if src else None
        layout = screen.get("layout") if isinstance(screen, dict) else None
        if field and isinstance(layout, list):
            if not any(_has_image_ref(n, f"$.{field}") for n in layout):
                layout.insert(0, {"type": "image", "src": f"$.{field}"})
                patched = True

    return out if patched else defn


# ── editable drill guarantee ──────────────────────────────────────────────────


def _mutable_list_fields(defn: dict) -> dict[str, dict]:
    """list state key → its item schema {field: type}, for USER-owned lists.

    Source-owned lists (``source: true``) are read-only — user mutations
    (``item_update``) are rejected on them — so they're excluded.
    """
    out: dict[str, dict] = {}
    for key, spec in (defn.get("state") or {}).items():
        if (
            isinstance(spec, dict)
            and spec.get("type") == "list"
            and not spec.get("source")
        ):
            item = spec.get("item")
            if isinstance(item, dict):
                out[key] = dict(item)
    return out


def _editable_fields(fields: dict) -> list[tuple[str, str]]:
    """Ordered ``(field, type)`` pairs a user can edit — never ``id``/``image``."""
    out: list[tuple[str, str]] = []
    for f, t in fields.items():
        if f == "id" or t == "image" or t not in _EDITABLE_FIELD_TYPES:
            continue
        out.append((f, t))
    return out


def _collect_covered_fields(node: Any, acc: set[str]) -> None:
    """Fields already bound to an ``item_update``/``item_toggle`` in this subtree."""
    if isinstance(node, list):
        for n in node:
            _collect_covered_fields(n, acc)
        return
    if not isinstance(node, dict):
        return
    action = node.get("action")
    if isinstance(action, dict):
        op = action.get("op")
        if op in ("item_update", "item_toggle"):
            f = action.get("field")
            if isinstance(f, str):
                acc.add(f)
            fields = action.get("fields")
            if isinstance(fields, dict):
                acc.update(k for k in fields if isinstance(k, str))
    _collect_covered_fields(node.get("children"), acc)
    item = node.get("item")
    if isinstance(item, dict):
        _collect_covered_fields(item, acc)


def _collect_ids(node: Any, acc: set[str]) -> None:
    if isinstance(node, list):
        for n in node:
            _collect_ids(n, acc)
        return
    if not isinstance(node, dict):
        return
    cid = node.get("id")
    if isinstance(cid, str):
        acc.add(cid)
    _collect_ids(node.get("children"), acc)
    item = node.get("item")
    if isinstance(item, dict):
        _collect_ids(item, acc)


def _edit_input(field: str, ftype: str, list_key: str, used_ids: set[str]) -> dict:
    """An input component bound to one row field via an item op, seeded with the
    row's current value (``$.field``) so the client shows what it's editing."""
    # Letter-leading id (the id grammar requires it); the counter keeps it
    # unique. Agent ids also start with a letter, so a collision is only ever
    # with another injected input, which the counter resolves.
    cid = f"edit_{field}"
    n = 2
    while cid in used_ids:
        cid = f"edit_{field}_{n}"
        n += 1
    used_ids.add(cid)
    common = {"id": cid, "label": field, "value": f"$.{field}"}
    if ftype == "bool":
        return {"type": "toggle", **common,
                "action": {"op": "item_toggle", "key": list_key, "field": field}}
    ctype = {"number": "number_input", "date": "date"}.get(ftype, "input")
    return {"type": ctype, **common,
            "action": {"op": "item_update", "key": list_key, "field": field}}


def ensure_editable_drill(defn: dict) -> dict:
    """Guarantee every user-owned list row is editable — never left to the agent.

    A drilled-into row must expose an editable control for each of its fields;
    otherwise a wrong value (e.g. a photo-vision kcal estimate) can't be fixed.
    This is the edit analogue of :func:`ensure_photo_display`: serve-only, never
    persisted, idempotent, additive — an author-placed edit input always wins;
    we only fill the gaps.

    * A repeater/table with a drill screen: inject an ``item_update``/
      ``item_toggle`` input for each editable field the screen doesn't already
      cover, appended to the screen.
    * A repeater whose list has editable fields but NO drill screen: synthesize
      one (edit inputs) and point the repeater at it, so drill-to-edit always
      exists — bounded by ``MAX_SCREENS``.

    Compose OUTSIDE this (``ensure_photo_display(ensure_editable_drill(defn))``)
    so a synthesized screen also gets its full photo.
    """
    lists = _mutable_list_fields(defn)
    if not lists:
        return defn

    out = copy.deepcopy(defn)
    screens = out.get("screens")
    if not isinstance(screens, dict):
        screens = {}
    patched = False

    nav_targets: dict[str, str] = {}          # screenId → list it drills into
    orphan_repeaters: list[tuple[dict, str]] = []  # repeaters with no valid drill

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            src = node.get("source")
            typ = node.get("type")
            if typ in ("repeater", "table") and isinstance(src, str) and src in lists:
                nav = node.get("navigate")
                if isinstance(nav, str) and nav in screens:
                    nav_targets.setdefault(nav, src)
                elif typ == "repeater":
                    # No screen to edit in — a repeater can grow one (a table
                    # can't cleanly, so it's left read-only if it has no nav).
                    orphan_repeaters.append((node, src))
            walk(node.get("children"))

    walk(out.get("layout"))

    # 1. Fill gaps in existing drill screens.
    for sid, list_key in nav_targets.items():
        screen = screens.get(sid)
        layout = screen.get("layout") if isinstance(screen, dict) else None
        editable = _editable_fields(lists[list_key])
        if not editable or not isinstance(layout, list):
            continue
        covered: set[str] = set()
        _collect_covered_fields(layout, covered)
        used_ids: set[str] = set()
        _collect_ids(layout, used_ids)
        added = [
            _edit_input(f, t, list_key, used_ids)
            for f, t in editable
            if f not in covered
        ]
        if added:
            layout.extend(added)
            patched = True

    # 2. Synthesize a drill screen for a mutable list that has none.
    for node, list_key in orphan_repeaters:
        if len(screens) >= catalog.MAX_SCREENS:
            break
        editable = _editable_fields(lists[list_key])
        if not editable:
            continue
        sid = f"edit_{list_key}"
        n = 2
        while sid in screens:
            sid = f"edit_{list_key}_{n}"
            n += 1
        used_ids: set[str] = set()
        inputs = [_edit_input(f, t, list_key, used_ids) for f, t in editable]
        screen: dict = {"layout": inputs}
        # Title from the first text field so the header names the row.
        title_field = next((f for f, t in editable if t == "string"), None)
        if title_field:
            screen["title"] = "{$." + title_field + "}"
        screens[sid] = screen
        node["navigate"] = sid
        patched = True

    if patched:
        out["screens"] = screens
        return out
    return defn


# ── chart layout (never cram a chart into columns) ────────────────────────────

def _contains_chart(node: Any) -> bool:
    if isinstance(node, list):
        return any(_contains_chart(n) for n in node)
    if not isinstance(node, dict):
        return False
    if node.get("type") == "chart":
        return True
    return _contains_chart(node.get("children"))


def ensure_chart_layout(defn: dict) -> dict:
    """Force any multi-column ``grid`` that holds a ``chart`` to a SINGLE column.

    A chart needs the full width — a donut's legend/labels spill off a phone in
    a 2-up grid, and two chart cards of different heights read as broken side by
    side. Stacking them full-width is always right, so the system guarantees it
    regardless of how the agent laid it out. Runs on the EXPANDED definition (a
    ``tracker_card`` is a card+chart by then). Serve-only, idempotent; returns
    the original when nothing needed changing.
    """
    out = copy.deepcopy(defn)
    changed = False

    def walk(nodes: Any) -> None:
        nonlocal changed
        if isinstance(nodes, list):
            for n in nodes:
                walk(n)
            return
        if not isinstance(nodes, dict):
            return
        if (nodes.get("type") == "grid" and int(nodes.get("columns") or 2) >= 2
                and _contains_chart(nodes.get("children"))):
            nodes["columns"] = 1
            changed = True
        walk(nodes.get("children"))
        item = nodes.get("item")
        if isinstance(item, dict):
            walk(item)

    walk(out.get("layout"))
    for screen in (out.get("screens") or {}).values():
        if isinstance(screen, dict):
            walk(screen.get("layout"))

    return out if changed else defn
