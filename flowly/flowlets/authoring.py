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
    """Provider-friendly top-level schema; semantic validation remains authoritative."""
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
            "Schedule example: {id:'evening',trigger:'schedule',at:'21:00',notify:{title:'Summary',body:'{total} today'}}.",
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

    for error in Draft202012Validator(definition_parameters()).iter_errors(definition):
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
