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

from datetime import tzinfo
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.flowlets import queries
from flowly.flowlets.queries import coerce_state, resolve_values

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
        store.add_event(flowlet_id, series, float(v))

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
        try:
            await agent_runner(flowlet, str(message))
        except Exception as exc:  # noqa: BLE001 — never crash the action path
            logger.warning("Flowlet agent action failed: {}", exc)
            raise FlowletActionError("UNAVAILABLE", "the agent couldn't handle that just now")

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
