"""The action interpreter — the write side.

A client never sends a free-form mutation. It sends ``{flowletId, componentId,
value?}``; this module finds the component in the *stored definition*, reads
its declared ``action``, validates the value against the component's and the
state key's constraints, applies it deterministically (no LLM), then returns
the freshly-resolved ``values`` so the caller can broadcast one ``flowlet.state``.

The only op that reaches the model is ``agent``, which hands a message to an
injected runner — the same privilege as the user typing that message.
"""

from __future__ import annotations

import os
import re
from datetime import tzinfo
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.flowlets import catalog, queries
from flowly.flowlets.queries import coerce_state, resolve_values

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

AgentRunner = Callable[[dict, str], Awaitable[None]]


class FlowletActionError(Exception):
    """A client action was invalid or targeted a missing flowlet/component."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _find_component(definition: dict, component_id: str) -> dict | None:
    for comp in queries._iter_components(definition.get("layout", [])):
        if comp.get("id") == component_id:
            return comp
    return None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _state_spec(definition: dict, key: str) -> dict:
    spec = (definition.get("state", {}) or {}).get(key)
    if not isinstance(spec, dict):
        raise FlowletActionError("INVALID", f"state key '{key}' is not declared")
    return spec


def _current_scalar(store, flowlet_id: str, definition: dict, key: str) -> Any:
    state_map = store.get_state(flowlet_id)
    spec = _state_spec(definition, key)
    raw = state_map.get(key, spec.get("default"))
    return coerce_state(raw, spec)


# ── Dynamic-list helpers ──────────────────────────────────────────────────────

def _list_spec(definition: dict, key: str) -> dict:
    spec = _state_spec(definition, key)
    if spec.get("type") != "list":
        raise FlowletActionError("INVALID", f"'{key}' is not a list state key")
    return spec


def _load_items(store, flowlet_id: str, key: str, spec: dict) -> list[dict]:
    return list(coerce_state(store.get_state(flowlet_id).get(key), spec))


def _coerce_field(key: str, field: str, ftype: str, v: Any) -> Any:
    """Coerce one item-field value to its declared type; raise on nonsense."""
    if ftype == "string":
        if isinstance(v, (dict, list)):
            raise FlowletActionError("INVALID", f"'{key}.{field}' must be text")
        return ("" if v is None else str(v)).strip()[: catalog.MAX_STRING_INPUT]
    if ftype == "number":
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise FlowletActionError("INVALID", f"'{key}.{field}' must be a number")
        return float(v)
    if ftype == "bool":
        return bool(v)
    if ftype == "date":
        s = str(v or "").strip()
        if not _DATE_RE.match(s):
            raise FlowletActionError("INVALID", f"'{key}.{field}' must be YYYY-MM-DD")
        return s
    if ftype == "image":
        # an attachment id (normally written by the `vision` op); an opaque,
        # bounded token that the `image` component resolves to bytes.
        return ("" if v is None else str(v)).strip()[:128]
    raise FlowletActionError("INVALID", f"'{key}.{field}' has an unknown type")


def _item_envelope(passed_value: Any) -> tuple[str, Any]:
    """Row-scoped ops arrive as ``{"itemId": ..., "value": ...}`` — the repeater
    on the client attaches the tapped row's id."""
    if not isinstance(passed_value, dict) or not passed_value.get("itemId"):
        raise FlowletActionError("INVALID", "this action needs the tapped item (itemId)")
    return str(passed_value["itemId"]), passed_value.get("value")


def _gc_item_photos(store, flowlet_id: str, fields: dict, item: dict) -> None:
    """Delete any stored photos an item's `image` fields reference."""
    for f, ft in fields.items():
        if ft == "image" and isinstance(item.get(f), str) and item[f]:
            store.delete_attachment(flowlet_id, item[f])


def remove_list_item(store, flowlet_id: str, definition: dict, key: str, item_id: str) -> bool:
    """Remove one row from a list state key by id (GC-ing its photos). Shared by
    the `item_remove` op and the client's swipe-to-delete RPC. Returns True if a
    row was removed. Raises FlowletActionError if `key` isn't a declared list."""
    spec = _list_spec(definition, key)
    fields = spec.get("item") or {}
    items = _load_items(store, flowlet_id, key, spec)
    match = next((it for it in items if it.get("id") == item_id), None)
    if match is None:
        return False
    _gc_item_photos(store, flowlet_id, fields, match)
    store.set_state(flowlet_id, key, [it for it in items if it.get("id") != item_id])
    return True


def _find_item(items: list[dict], item_id: str) -> dict:
    for it in items:
        if it.get("id") == item_id:
            return it
    raise FlowletActionError("NOT_FOUND", "that item no longer exists")


async def apply_action(
    store,
    flowlet_id: str,
    component_id: str,
    value: Any = None,
    *,
    tz: tzinfo | None = None,
    agent_runner: AgentRunner | None = None,
) -> dict:
    """Apply the action declared on ``component_id`` and return the new values.

    Returns ``{"flowletId": id, "values": {...}}``. Raises
    :class:`FlowletActionError` (``code`` in NOT_FOUND / INVALID / UNAVAILABLE).
    """
    flowlet = store.get(flowlet_id)
    if not flowlet:
        raise FlowletActionError("NOT_FOUND", f"flowlet '{flowlet_id}' not found")
    definition = flowlet["definition"]

    component = _find_component(definition, component_id)
    if component is None:
        raise FlowletActionError("NOT_FOUND", f"component '{component_id}' not found")

    if component.get("type") == "checklist":
        # A checklist has no single action — each tap toggles the item whose
        # `key` the client passes as ``value``. Restrict to the declared item
        # keys so a client can't flip an arbitrary state key.
        item_keys = {
            it.get("key")
            for it in (component.get("items") or [])
            if isinstance(it, dict)
        }
        if value not in item_keys:
            raise FlowletActionError(
                "INVALID", f"checklist item '{value}' is not part of '{component_id}'"
            )
        await _apply_op(
            store, flowlet_id, definition, component,
            {"op": "toggle", "key": value}, None, agent_runner=agent_runner,
        )
    else:
        action = component.get("action")
        if not isinstance(action, dict):
            raise FlowletActionError("INVALID", f"component '{component_id}' has no action")
        await _apply_op(
            store, flowlet_id, definition, component, action, value,
            agent_runner=agent_runner,
        )

    # Recompute the full values map from the post-mutation state + events.
    values = resolve_values(
        definition,
        store.get_state(flowlet_id),
        store.get_events(flowlet_id),
        queries_now_ms(),
        tz,
    )
    return {"flowletId": flowlet_id, "values": values}


def queries_now_ms() -> int:
    # Indirection kept tiny so tests can monkeypatch a fixed clock if needed.
    from flowly.flowlets.store import now_ms
    return now_ms()


async def _apply_op(
    store,
    flowlet_id: str,
    definition: dict,
    component: dict,
    action: dict,
    passed_value: Any,
    *,
    agent_runner: AgentRunner | None,
    _depth: int = 0,
) -> None:
    op = action.get("op")

    # Inside a repeater template the client wraps EVERY action value in the
    # row envelope {"itemId", "value"}. Row-scoped ops consume it whole; any
    # other op (an agent button in a row, an item_add) just wants the inner
    # value — unwrap here so templates stay fully general.
    if (
        isinstance(passed_value, dict)
        and "itemId" in passed_value
        and op not in ("item_update", "item_toggle", "item_remove", "item_move")
    ):
        passed_value = passed_value.get("value")

    # A fixed value in the action (e.g. "drink 250ml" button) always wins over
    # whatever the client passed — the client can only supply a value when the
    # component is a free input (slider / number_input / input / rating).
    def effective_value() -> Any:
        return action["value"] if "value" in action else passed_value

    if op == "set":
        key = action["key"]
        spec = _state_spec(definition, key)
        v = effective_value()
        if v is None:
            raise FlowletActionError("INVALID", f"action `set` on '{key}' needs a value")
        v = _validate_component_value(component, spec, v)
        store.set_state(flowlet_id, key, coerce_state(v, spec))

    elif op in ("increment", "decrement"):
        key = action["key"]
        spec = _state_spec(definition, key)
        if spec.get("type") != "number":
            raise FlowletActionError("INVALID", f"`{op}` needs a number state key; '{key}' is {spec.get('type')}")
        by = action.get("by", 1)
        if not _is_number(by):
            raise FlowletActionError("INVALID", f"`{op}` `by` must be a number")
        # A stepper serves both its − and + buttons with one action by passing a
        # signed direction (−1 / +1); the magnitude stays the declared `by`. A
        # plain button passes nothing and just uses `by`.
        if passed_value is not None and _is_number(passed_value):
            by = abs(by) * (1 if passed_value >= 0 else -1)
        cur = _current_scalar(store, flowlet_id, definition, key)
        cur = cur if _is_number(cur) else 0
        new = cur + by if op == "increment" else cur - by
        store.set_state(flowlet_id, key, coerce_state(new, spec))

    elif op == "toggle":
        key = action["key"]
        spec = _state_spec(definition, key)
        cur = _current_scalar(store, flowlet_id, definition, key)
        store.set_state(flowlet_id, key, coerce_state(not bool(cur), spec))

    elif op == "timer_toggle":
        key = action["key"]
        _state_spec(definition, key)  # ensure declared
        cur = store.get_state(flowlet_id).get(key) or {}
        running = bool(cur.get("running"))
        accum = float(cur.get("accum_s", 0) or 0)
        since = int(cur.get("since_ms", 0) or 0)
        now = queries_now_ms()
        if running:
            accum += max(0, now - since) / 1000.0
            new = {"running": False, "since_ms": 0, "accum_s": accum}
        else:
            new = {"running": True, "since_ms": now, "accum_s": accum}
        store.set_state(flowlet_id, key, new)

    elif op == "log":
        series = action.get("series")
        if not series:
            raise FlowletActionError("INVALID", "action `log` needs a series")
        v = effective_value()
        if v is None:
            v = 1  # a bare "log" with no value counts as one occurrence
        if not _is_number(v):
            raise FlowletActionError("INVALID", "action `log` value must be a number")
        # An optional `category` tags the event for pie/donut charts. It may be a
        # literal ("food") or a "{token}" that templates from live values plus
        # `{value}` (what the user tapped/typed) — same rule as the agent op.
        meta = None
        cat_tpl = action.get("category")
        if isinstance(cat_tpl, str) and cat_tpl.strip():
            ns = resolve_values(
                definition, store.get_state(flowlet_id), store.get_events(flowlet_id),
                queries_now_ms(), None,
            )
            if passed_value is not None and not isinstance(passed_value, (dict, list)):
                ns = {**ns, "value": passed_value}
            cat = queries.render_template(cat_tpl, ns).strip()[: catalog.MAX_CATEGORY_LEN]
            if cat:
                meta = {"category": cat}
        store.add_event(flowlet_id, series, float(v), meta=meta)

    elif op == "remove_last":
        series = action.get("series")
        if not series:
            raise FlowletActionError("INVALID", "action `remove_last` needs a series")
        store.remove_last_event(flowlet_id, series)

    elif op == "reset":
        key = action.get("key")
        series = action.get("series")
        if key:
            store.reset_state(flowlet_id, key)
        if series:
            store.reset_events(flowlet_id, series)
        if not key and not series:
            raise FlowletActionError("INVALID", "action `reset` needs key or series")

    elif op == "agent":
        message = action.get("message")
        if not message:
            raise FlowletActionError("INVALID", "action `agent` needs a message")
        if agent_runner is None:
            raise FlowletActionError("UNAVAILABLE", "this flowlet action can't run right now")
        flowlet = store.get(flowlet_id)
        # Template the message with live values plus `{value}` = whatever the
        # user typed/tapped — this is what lets a free-text input reach the
        # model ("Log this meal: {value}" → "…: iki dilim pizza"). The agent
        # only ever sees the declared message shape, never a raw client string
        # outside its {value} slot.
        ns = resolve_values(
            definition, store.get_state(flowlet_id), store.get_events(flowlet_id),
            queries_now_ms(), None,
        )
        if passed_value is not None and not isinstance(passed_value, (dict, list)):
            v = passed_value
            if isinstance(v, str):
                v = v.strip()[:500]  # same cap as the `input` component
            ns = {**ns, "value": v}
        message = queries.render_template(message, ns)
        try:
            await agent_runner(flowlet, message)
        except Exception as exc:  # noqa: BLE001 — never crash the action path
            logger.warning("Flowlet agent action failed: {}", exc)
            raise FlowletActionError("UNAVAILABLE", "the agent couldn't handle that just now")

    elif op == "item_add":
        key = action["key"]
        spec = _list_spec(definition, key)
        fields: dict = spec.get("item") or {}
        items = _load_items(store, flowlet_id, key, spec)
        limit = int(spec.get("max") or catalog.MAX_LIST_ITEMS)
        if len(items) >= limit:
            raise FlowletActionError("INVALID", f"'{key}' is full ({limit} items)")
        new: dict[str, Any] = {}
        for f, v in (action.get("item") or {}).items():  # fixed values from the action
            new[f] = _coerce_field(key, f, fields[f], v)
        pv = passed_value
        if isinstance(pv, dict):
            # A form of declared fields (e.g. title + due from two inputs).
            for f, v in pv.items():
                if f in fields:
                    new[f] = _coerce_field(key, f, fields[f], v)
        elif pv is not None:
            # A bare value from a single input → the first (only) matching
            # string/number field, the quick-add pattern.
            target = next(
                (f for f, ft in fields.items()
                 if ft == ("number" if _is_number(pv) else "string") and f not in new),
                None,
            )
            if target is not None:
                new[target] = _coerce_field(key, target, fields[target], pv)
        # Drop a fully-empty add (an empty quick-add input) instead of storing
        # a blank row.
        if not any(v not in ("", None) for v in new.values()):
            raise FlowletActionError("INVALID", "nothing to add")
        new["id"] = f"itm_{os.urandom(4).hex()}"
        items.append(new)
        store.set_state(flowlet_id, key, items)

    elif op in ("item_update", "item_toggle", "item_remove", "item_move"):
        key = action["key"]
        spec = _list_spec(definition, key)
        fields = spec.get("item") or {}
        items = _load_items(store, flowlet_id, key, spec)
        item_id, inner_value = _item_envelope(passed_value)
        item = _find_item(items, item_id)

        if op == "item_toggle":
            f = action["field"]
            item[f] = not bool(item.get(f))
        elif op == "item_update":
            if "fields" in action:  # fixed updates declared on the action
                for f, v in (action.get("fields") or {}).items():
                    item[f] = _coerce_field(key, f, fields[f], v)
            else:  # single field, value from the client control
                f = action["field"]
                if inner_value is None:
                    raise FlowletActionError("INVALID", f"`item_update` on '{f}' needs a value")
                item[f] = _coerce_field(key, f, fields[f], inner_value)
        elif op == "item_remove":
            _gc_item_photos(store, flowlet_id, fields, item)
            items = [it for it in items if it.get("id") != item_id]
        else:  # item_move
            if not _is_number(inner_value):
                raise FlowletActionError("INVALID", "`item_move` needs the new index as value")
            items = [it for it in items if it.get("id") != item_id]
            idx = max(0, min(len(items), int(inner_value)))
            items.insert(idx, item)
        store.set_state(flowlet_id, key, items)

    elif op == "batch":
        if _depth > 0:
            raise FlowletActionError("INVALID", "nested batch is not allowed")
        for sub in action.get("ops", []):
            await _apply_op(
                store, flowlet_id, definition, component, sub, passed_value,
                agent_runner=agent_runner, _depth=_depth + 1,
            )

    else:
        raise FlowletActionError("INVALID", f"unknown action op '{op}'")


def _validate_component_value(component: dict, spec: dict, value: Any) -> Any:
    """Clamp/validate a client-supplied value against the *component's* declared
    bounds (a slider can't exceed its own min/max; an input can't exceed its
    maxLength). State-level type/min/max is enforced afterwards by coerce_state.
    """
    ctype = component.get("type")
    if ctype in ("slider", "number_input", "stepper", "gauge"):
        if not _is_number(value):
            raise FlowletActionError("INVALID", f"{ctype} value must be a number")
        mn, mx = component.get("min"), component.get("max")
        if _is_number(mn):
            value = max(value, mn)
        if _is_number(mx):
            value = min(value, mx)
    elif ctype in ("input",):
        value = str(value)
        ml = component.get("maxLength", spec.get("maxLength"))
        if isinstance(ml, int) and ml > 0:
            value = value[:ml]
    elif ctype == "rating":
        if not _is_number(value):
            raise FlowletActionError("INVALID", "rating value must be a number")
        mx = component.get("max", 5)
        value = max(0, min(value, mx if _is_number(mx) else 5))
    return value
