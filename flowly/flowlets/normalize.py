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

_THUMB_HEIGHT = 44


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
