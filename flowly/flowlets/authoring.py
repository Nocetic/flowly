"""Small, executable authoring contract shared by tools and validation feedback.

Keep the full reference out of routine creation turns. Catalog metadata is the
source of component names, required properties and bind types, so guidance cannot
quietly disagree with the validator when the catalog evolves.
"""

from __future__ import annotations

import json
from typing import Any

from flowly.flowlets import catalog


def definition_parameters() -> dict:
    """The model-facing top-level schema: types and shape only.

    Range keywords (minimum, maxLength, minItems …) are kept out of what
    providers see — some function-calling dialects reject them — and live in
    :func:`_definition_schema`, the preflight check, instead.
    """
    return _strip_constraints(_definition_schema())


_CONSTRAINT_KEYWORDS = frozenset({"minimum", "maximum", "minLength", "maxLength", "minItems"})


def _strip_constraints(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_constraints(v) for k, v in node.items() if k not in _CONSTRAINT_KEYWORDS}
    if isinstance(node, list):
        return [_strip_constraints(v) for v in node]
    return node


def _definition_schema() -> dict:
    """Top-level shape with ranges, for the preflight; semantics live in schema.py."""
    return {
        "type": "object",
        "description": (
            "Full definition. Prefer create with template_id for standard screens. "
            "For custom screens use action=guide once. Lists belong in state; "
            "layout is an array; computed expr is a string."
        ),
        "properties": {
            "catalog": {"type": "integer", "minimum": 1, "maximum": catalog.CATALOG_VERSION},
            "name": {"type": "string", "minLength": 1, "maxLength": catalog.MAX_NAME_LEN},
            "icon": {"type": "string"},
            "accent": {"type": "string"},
            "locale": {"type": "string", "enum": ["en", "tr", "es"]},
            "state": {"type": "object", "description":
                'Key to {type,default}; list: {type:"list",item:{field:"string|number|bool|date|image"},max:200}.'},
            "series": {"type": "object"},
            "computed": {"type": "object", "description":
                'Key to {expr:"max(0, goal-total)"} or {list:"rows",field:"amount",agg:"sum",where:"days_since(date)==0"}.'},
            "layout": {"type": "array", "minItems": 1, "items": {"type": "object"}},
            "screens": {"type": "object"},
            "sources": {"type": "object"},
            "watches": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["catalog", "name", "layout"],
    }


def guide() -> dict:
    return {
        "catalog": catalog.CATALOG_VERSION,
        "workflow": (
            "Standard screen: create(template_id, lang, pinned). Custom screen: "
            "template(template_id, lang) returns an editable definition; adapt it then create. "
            "Existing screen: get then update. list/get are authoritative; memory is not an inventory. "
            "No shell commands or skill files needed. create/update validate before saving and "
            "return live values plus an advisory review; do not get again just to verify creation."
        ),
        "shape": definition_parameters(),
        "rules": [
            "Use scalar key strings for binds: progress.value='total', max='goal', not {ref:...}.",
            "Put arithmetic in computed: remaining={expr:'max(0, goal-total)'}, never inline expression objects.",
            "Use photo with action={op:'vision',into:'rows',prompt:'...'}; include image and date fields in the list.",
            "vision.dateDefaults=['date'] fills a missing/invalid date with the capture day; extracted dates win.",
            "Use form(into,fields,submit) for manual entry, list_row for repeater items, tracker_card for list totals/charts.",
            "Derive totals/charts from the same list; no shadow series. Today filter: days_since(date)==0.",
            "Use watches for requested reminders/summaries; no cron or model needed for a templated summary.",
            "Schedule example: {id:'evening',trigger:'schedule',at:'21:00',when:'count>0',notify:{title:'Summary',body:'{total} today'},otherwise:{title:'Summary',body:'Nothing logged yet'}}.",
            "Set locale ('en'|'tr'|'es') so reminders format numbers for the user (1.450 in tr).",
            "Don't open the layout with a header repeating name; clients already title the screen.",
            "Template reminders attach by name: create(template_id, watches=[{preset:'daily_summary'}]).",
            "Times and day windows use the execution host's local timezone. State list max is 200; never silently discard history.",
        ],
        "components": {name: {k: v for k, v in spec.items()
                              if k in {"required", "binds", "container", "action"}}
                       for name, spec in catalog.COMPONENTS.items()},
        "limits": {"listItems": catalog.MAX_LIST_ITEMS, "itemFields": catalog.MAX_ITEM_FIELDS,
                   "components": catalog.MAX_COMPONENTS, "screens": catalog.MAX_SCREENS},
    }


def structural_errors(definition: Any) -> list[dict[str, str]]:
    """Report independent shape errors together instead of one paid retry each.

    This preflight never repairs, drops data, or replaces semantic validation.
    Its walk is bounded even for malformed inputs. Unknown component properties
    remain forward compatible; top-level lists is a common mistaken state shape.
    """
    from jsonschema import Draft202012Validator

    try:
        if len(json.dumps(definition).encode("utf-8")) > catalog.MAX_DEFINITION_BYTES:
            return [{"path": "$", "message": f"Definition exceeds {catalog.MAX_DEFINITION_BYTES} bytes."}]
    except (TypeError, ValueError):
        return [{"path": "$", "message": "Definition must be JSON-serializable."}]

    errors: list[dict[str, str]] = []

    def add(path: str, message: str) -> None:
        if len(errors) < 20:
            errors.append({"path": path, "message": message})

    for error in Draft202012Validator(_definition_schema()).iter_errors(definition):
        add("$." + ".".join(str(p) for p in error.absolute_path), error.message)
        if len(errors) >= 20:
            return errors
    if not isinstance(definition, dict):
        return errors
    if "lists" in definition:
        add("$.lists", "Lists belong in state: {rows:{type:'list',item:{name:'string'}}}.")
    state = definition.get("state")
    if isinstance(state, dict):
        for key, spec in state.items():
            if isinstance(spec, dict) and spec.get("type") == "list" and "max" in spec:
                maximum = spec["max"]
                if type(maximum) is not int or not 1 <= maximum <= catalog.MAX_LIST_ITEMS:
                    add(f"$.state.{key}.max", f"Must be an integer 1..{catalog.MAX_LIST_ITEMS}.")
    computed = definition.get("computed")
    if isinstance(computed, dict):
        for key, spec in computed.items():
            if isinstance(spec, dict) and "expr" in spec and not isinstance(spec["expr"], str):
                add(f"$.computed.{key}.expr", "Expression must be a string, e.g. 'max(0, goal-total)'.")

    visited = 0

    def walk(node: Any, path: str, depth: int = 0) -> None:
        nonlocal visited
        if visited >= catalog.MAX_COMPONENTS or depth > catalog.MAX_DEPTH or len(errors) >= 20:
            return
        if isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]", depth)
            return
        visited += 1
        if not isinstance(node, dict):
            add(path, "Component must be an object.")
            return
        kind = node.get("type")
        spec = catalog.COMPONENTS.get(kind) if isinstance(kind, str) else None
        if spec is None:
            add(path + ".type", "Unknown component; use guide.components (photo captures images).")
            return
        for prop in spec.get("required", []):
            if prop not in node and prop != "id":  # IDs are assigned before persistence.
                add(path + "." + prop, f"{kind} requires '{prop}'.")
        for prop in spec.get("binds", []):
            if prop in node and isinstance(node[prop], (dict, list)):
                add(path + "." + prop, "Bind must be a scalar key string or numeric literal.")
        if "children" in node:
            walk(node["children"], path + ".children", depth + 1)
        if kind == "repeater" and "item" in node:
            walk(node["item"], path + ".item", depth + 1)

    if isinstance(definition.get("layout"), list):
        walk(definition["layout"], "$.layout")
    screens = definition.get("screens")
    if isinstance(screens, dict):
        for key, screen in screens.items():
            if isinstance(screen, dict) and isinstance(screen.get("layout"), list):
                walk(screen["layout"], f"$.screens.{key}.layout")
    return errors


# ── lossless repair of unambiguous dialect slips ─────────────────────────────
#
# A model that writes `heading` for `header` or `{"ref": "total"}` for a bind
# means exactly one thing; bouncing it back costs a full model round trip and
# teaches nothing. These rewrites are restricted to cases with ONE reading,
# never drop content, and every rewrite is reported back to the author.

#: Spellings models reach for → the catalog's name (plus fixed props).
TYPE_ALIASES: dict[str, tuple[str, dict]] = {
    "heading": ("header", {}), "title": ("header", {}), "h1": ("header", {}),
    "paragraph": ("text", {}), "label": ("text", {}), "note": ("text", {}),
    "textfield": ("input", {}), "text_field": ("input", {}), "text_input": ("input", {}),
    "textbox": ("input", {}),
    "number": ("number_input", {}), "numeric": ("number_input", {}),
    "number_field": ("number_input", {}), "numeric_input": ("number_input", {}),
    "camera": ("photo", {}), "photo_capture": ("photo", {}), "image_capture": ("photo", {}),
    "photo_input": ("photo", {}), "image_picker": ("photo", {}), "photo_picker": ("photo", {}),
    "checkbox": ("toggle", {}), "switch": ("toggle", {}),
    "progress_bar": ("progress", {}), "progressbar": ("progress", {}),
    "dropdown": ("select", {}), "picker": ("select", {}),
    "date_input": ("date", {}), "date_picker": ("date", {}), "datepicker": ("date", {}),
    "bar_chart": ("chart", {"kind": "bar"}), "line_chart": ("chart", {"kind": "line"}),
    "area_chart": ("chart", {"kind": "area"}),
    "stack": ("column", {}), "vstack": ("column", {}), "hstack": ("row", {}),
    "section": ("card", {}), "container": ("card", {}),
}

#: Components whose required `text` a model often spells as another prop.
_TEXT_PROPS = ("title", "label", "content")
_REF_KEYS = ("ref", "key", "bind", "state")


def repair_definition(definition: Any) -> tuple[Any, list[str]]:
    """Return ``(definition, notes)`` with unambiguous slips rewritten.

    The input is never mutated. Anything ambiguous is left for validation to
    report. ``notes`` says what changed so the author learns the dialect.
    """
    if not isinstance(definition, dict):
        return definition, []
    import copy

    d = copy.deepcopy(definition)
    notes: list[str] = []

    cat = d.get("catalog")
    if cat is None:
        d["catalog"] = catalog.CATALOG_VERSION
        notes.append(f"catalog: set to {catalog.CATALOG_VERSION}")
    elif isinstance(cat, str) and cat.strip().isdigit():
        d["catalog"] = int(cat.strip())
        notes.append("catalog: string → integer")
    elif isinstance(cat, float) and cat.is_integer():
        d["catalog"] = int(cat)

    if isinstance(d.get("layout"), dict):
        d["layout"] = [d["layout"]]
        notes.append("layout: single component wrapped in an array")

    lists = d.get("lists")
    if isinstance(lists, dict) and lists:
        state = d.setdefault("state", {})
        if isinstance(state, dict) and all(
            isinstance(v, dict) and isinstance(v.get("item"), dict) and k not in state
            for k, v in lists.items()
        ):
            for k, v in lists.items():
                state[k] = {"type": "list", **{x: y for x, y in v.items() if x != "type"}}
            del d["lists"]
            notes.append("lists: moved into state as type list")

    computed = d.get("computed")
    if isinstance(computed, dict):
        for key, spec in list(computed.items()):
            if isinstance(spec, str) and spec.strip():
                computed[key] = {"expr": spec}
                notes.append(f"computed.{key}: bare expression wrapped as {{expr}}")

    def fix(node: Any, path: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                fix(n, f"{path}[{i}]")
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if isinstance(kind, str) and kind not in catalog.COMPONENTS:
            alias = TYPE_ALIASES.get(kind.strip().lower().replace("-", "_"))
            if alias is not None:
                node["type"] = alias[0]
                for k, v in alias[1].items():
                    node.setdefault(k, v)
                notes.append(f"{path}: type {kind!r} → {alias[0]!r}")
        spec = catalog.COMPONENTS.get(node.get("type"))
        if spec is not None:
            if "text" in spec.get("required", []) and "text" not in node:
                prop = next((p for p in _TEXT_PROPS
                             if isinstance(node.get(p), str) and node[p].strip()), None)
                if prop is not None:
                    node["text"] = node.pop(prop)
                    notes.append(f"{path}: `{prop}` → `text`")
            for prop in spec.get("binds", []):
                v = node.get(prop)
                if isinstance(v, dict) and len(v) == 1:
                    (rk, rv), = v.items()
                    if rk in _REF_KEYS and isinstance(rv, str) and rv.strip():
                        node[prop] = rv.strip()
                        notes.append(f"{path}.{prop}: {{{rk}: …}} → key string")
        fix(node.get("children"), path + ".children")
        if isinstance(node.get("item"), dict) and node.get("type") == "repeater":
            fix(node["item"], path + ".item")

    fix(d.get("layout"), "$.layout")
    screens = d.get("screens")
    if isinstance(screens, dict):
        for sid, screen in screens.items():
            if isinstance(screen, dict):
                fix(screen.get("layout"), f"$.screens.{sid}.layout")
    return d, notes
