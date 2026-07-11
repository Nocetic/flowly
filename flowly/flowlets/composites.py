"""Composite components → primitive expansion (catalog 3).

A composite states INTENT ("this field is the row's title, this one its value")
and the bot expands it to the v2 primitives the renderers already draw, so the
system — not the agent — owns layout. This is the structural half of the
"agent'a kalmasın" doctrine: the agent can no longer mis-lay-out a list row
because it never authors the layout.

Expansion is a pure, deterministic, idempotent transform on a deep copy; a
definition with no composites is returned unchanged (no copy). It runs at serve
time, INNERMOST of the serving-time transforms, so every downstream pass
(photo/edit augmentation, value resolution) sees plain primitives.

Golden expansion vectors (``tests/flowlets/vectors/``) pin the output so the
native renderers (which will later draw composites directly) can be checked for
byte-identical structure — the same discipline as the shared sort vectors.
"""

from __future__ import annotations

import copy
from typing import Any

from flowly.flowlets import catalog

_THUMB_HEIGHT = 44


def _text_template(v: Any) -> str | None:
    """A composite prop → the string a ``text`` node should show.

    ``"$.title"`` → ``"{$.title}"`` (interpolated); ``"{$.amount} ₺"`` → as-is
    (already a template); a bare literal → itself. ``None``/non-string → None
    (the sub-node is omitted)."""
    if not isinstance(v, str) or not v:
        return None
    if "{" in v:
        return v
    if v.startswith("$."):
        return "{" + v + "}"
    return v


def _field_ref(v: Any) -> str | None:
    """A composite prop → an ``image`` ``src`` ref (a raw ``$.field``).

    Accepts ``"$.receipt"`` or ``"{$.receipt}"``; returns ``"$.receipt"``."""
    if not isinstance(v, str) or not v:
        return None
    inner = v.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()
    return inner if inner.startswith("$.") else None


def _expand_list_row(node: dict) -> dict:
    """A ``list_row`` → the canonical repeater-row primitive tree.

    ``[thumb]  title / subtitle(muted)  ……  [badge] value  ›``

    The leading text block is a greedy ``column`` (``grow: true``) so the
    trailing badge+value cluster pins right; the client's row renderer mutes the
    subtitle (second text) and appends the chevron when the repeater navigates.
    Absent props drop their sub-node — a row with only a title collapses to a
    single greedy text, which lays out correctly with no special-casing.
    """
    children: list[dict] = []

    thumb = _field_ref(node.get("thumb"))
    if thumb:
        children.append({"type": "image", "src": thumb, "height": _THUMB_HEIGHT})

    title = _text_template(node.get("title"))
    subtitle = _text_template(node.get("subtitle"))
    lead_texts = [{"type": "text", "text": t} for t in (title, subtitle) if t]
    if len(lead_texts) == 1 and not thumb:
        # One line, no thumb → a lone greedy text fills the row; badge/value
        # (below) hug right. No wrapping column needed.
        children.append(lead_texts[0])
    elif lead_texts:
        # Stacked title/subtitle → a greedy column fills the leading space; the
        # texts hug their own line (grow:false) so they don't stretch vertically.
        children.append({
            "type": "column", "grow": True,
            "children": [{**t, "grow": False} for t in lead_texts],
        })

    badge = _text_template(node.get("badge"))
    if badge:
        children.append({"type": "badge", "text": badge})

    value = _text_template(node.get("value"))
    if value:
        # The trailing value hugs the right edge, next to the chevron.
        children.append({"type": "text", "text": value, "grow": False})

    row: dict = {"type": "row", "children": children}
    if isinstance(node.get("visibleWhen"), str):
        row["visibleWhen"] = node["visibleWhen"]
    return row


#: composite type → (node) -> primitive node
_EXPANDERS = {
    "list_row": _expand_list_row,
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


def _expand_node(node: Any) -> Any:
    """Recursively replace composites with their primitive expansion."""
    if isinstance(node, list):
        return [_expand_node(n) for n in node]
    if not isinstance(node, dict):
        return node
    expander = _EXPANDERS.get(node.get("type"))
    if expander is not None:
        node = expander(node)
    out = dict(node)
    if isinstance(out.get("children"), list):
        out["children"] = [_expand_node(c) for c in out["children"]]
    if isinstance(out.get("item"), dict):
        out["item"] = _expand_node(out["item"])
    return out


def expand_composites(defn: dict) -> dict:
    """Return the definition with every composite expanded to primitives.

    Deterministic, idempotent (a re-expanded definition is a no-op — the output
    has no composites), and non-mutating. Returns the ORIGINAL object when the
    definition declares no composite (the common case — no copy, no walk).
    """
    if not _has_composite(defn.get("layout")) and not any(
        isinstance(s, dict) and _has_composite(s.get("layout"))
        for s in (defn.get("screens") or {}).values()
    ):
        return defn

    out = copy.deepcopy(defn)
    if isinstance(out.get("layout"), list):
        out["layout"] = _expand_node(out["layout"])
    for screen in (out.get("screens") or {}).values():
        if isinstance(screen, dict) and isinstance(screen.get("layout"), list):
            screen["layout"] = _expand_node(screen["layout"])
    return out
