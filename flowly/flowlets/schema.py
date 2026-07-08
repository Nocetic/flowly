"""Definition validator for flowlets.

Hand-written (not jsonschema) so the error messages are precise enough for the
agent to *fix* a bad definition on the next turn — e.g.
``slider 'goalSlider': min (4000) must be < max (1000)`` rather than a generic
schema path. :func:`validate_definition` raises :class:`FlowletValidationError`
with a single human-readable message on the first problem it finds.
"""

from __future__ import annotations

import json
import re
from typing import Any

from flowly.flowlets import catalog

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")  # 24h "HH:MM"


class FlowletValidationError(ValueError):
    """A definition failed validation. ``str(exc)`` is LLM-facing guidance."""


def _err(msg: str) -> "FlowletValidationError":
    return FlowletValidationError(msg)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_definition(defn: Any) -> dict:
    """Validate a flowlet definition. Returns the (unchanged) definition dict on
    success; raises :class:`FlowletValidationError` with actionable guidance.

    Collects the full namespace of scalar keys (state + computed + component
    ids) and series keys, then checks every `bind`, `action`, and `data`
    reference resolves — so a client never receives a dangling reference.
    """
    if not isinstance(defn, dict):
        raise _err("definition must be a JSON object")

    # ── size guard (measured on the canonical serialization) ──────────────────
    try:
        size = len(json.dumps(defn).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise _err(f"definition is not JSON-serializable: {exc}")
    if size > catalog.MAX_DEFINITION_BYTES:
        raise _err(
            f"definition is {size} bytes; the limit is {catalog.MAX_DEFINITION_BYTES} "
            "(~64 KB). Split it into more than one flowlet or trim the layout."
        )

    # ── catalog version ───────────────────────────────────────────────────────
    catalog_ver = defn.get("catalog")
    if not isinstance(catalog_ver, int) or isinstance(catalog_ver, bool):
        raise _err("top-level `catalog` must be an integer (use catalog: 1)")
    if catalog_ver > catalog.CATALOG_VERSION:
        raise _err(
            f"catalog {catalog_ver} is newer than this bot supports "
            f"(max {catalog.CATALOG_VERSION}). Use catalog: {catalog.CATALOG_VERSION}."
        )

    # ── name / icon / accent ──────────────────────────────────────────────────
    name = defn.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _err("`name` is required and must be a non-empty string")
    if len(name) > catalog.MAX_NAME_LEN:
        raise _err(f"`name` must be ≤ {catalog.MAX_NAME_LEN} characters")

    icon = defn.get("icon")
    if icon is not None and not isinstance(icon, str):
        raise _err("`icon` must be a string icon name")

    accent = defn.get("accent")
    if accent is not None:
        if not isinstance(accent, str) or not _HEX_RE.match(accent):
            raise _err("`accent` must be a hex color like #00A6C8 or #0AC")

    # ── state schema ──────────────────────────────────────────────────────────
    state_defs = defn.get("state", {})
    if not isinstance(state_defs, dict):
        raise _err("`state` must be an object of {key: {type, default, ...}}")
    if len(state_defs) > catalog.MAX_STATE_KEYS:
        raise _err(f"too many state keys (max {catalog.MAX_STATE_KEYS})")
    scalar_keys: set[str] = set()
    for key, spec in state_defs.items():
        if not _KEY_RE.match(key):
            raise _err(
                f"state key '{key}' is invalid; keys must start with a letter "
                "and contain only letters, digits, and underscores"
            )
        _validate_state_spec(key, spec)
        scalar_keys.add(key)

    # ── series schema ─────────────────────────────────────────────────────────
    series_defs = defn.get("series", {})
    if not isinstance(series_defs, dict):
        raise _err("`series` must be an object of {name: {unit?}}")
    if len(series_defs) > catalog.MAX_SERIES:
        raise _err(f"too many series (max {catalog.MAX_SERIES})")
    series_keys: set[str] = set()
    for key, spec in series_defs.items():
        if not _KEY_RE.match(key):
            raise _err(f"series key '{key}' is invalid (letters/digits/underscore)")
        if spec is not None and not isinstance(spec, dict):
            raise _err(f"series '{key}' must be an object (e.g. {{\"unit\": \"ml\"}})")
        series_keys.add(key)

    # ── computed schema ───────────────────────────────────────────────────────
    computed_defs = defn.get("computed", {})
    if not isinstance(computed_defs, dict):
        raise _err("`computed` must be an object of {key: {series|expr, ...}}")
    if len(computed_defs) > catalog.MAX_COMPUTED:
        raise _err(f"too many computed keys (max {catalog.MAX_COMPUTED})")
    computed_keys: set[str] = set()
    for key, spec in computed_defs.items():
        if not _KEY_RE.match(key):
            raise _err(f"computed key '{key}' is invalid (letters/digits/underscore)")
        if key in scalar_keys:
            raise _err(f"computed key '{key}' collides with a state key of the same name")
        _validate_computed_spec(key, spec, series_keys)
        computed_keys.add(key)

    scalar_keys |= computed_keys  # both resolve to scalars in `values`

    # ── layout tree ───────────────────────────────────────────────────────────
    layout = defn.get("layout")
    if not isinstance(layout, list) or not layout:
        raise _err("`layout` must be a non-empty array of components")

    ctx = _Ctx(
        scalar_keys=scalar_keys,
        series_keys=series_keys,
        component_ids=set(),
        count=0,
    )
    for node in layout:
        _validate_node(node, ctx, depth=1)

    if ctx.count > catalog.MAX_COMPONENTS:
        raise _err(f"too many components ({ctx.count}); the limit is {catalog.MAX_COMPONENTS}")

    # ── watches (reactive rules; evaluated LLM-free — see watches.py) ─────────
    watches = defn.get("watches")
    if watches is not None:
        _validate_watches(watches, scalar_keys)

    return defn


# ── state / computed spec validation ─────────────────────────────────────────

def _validate_state_spec(key: str, spec: Any) -> None:
    if not isinstance(spec, dict):
        raise _err(f"state '{key}' must be an object with a `type`")
    stype = spec.get("type")
    if stype not in catalog.STATE_TYPES:
        raise _err(
            f"state '{key}': type must be one of {sorted(catalog.STATE_TYPES)}, got {stype!r}"
        )
    default = spec.get("default")
    if stype == "number":
        if default is not None and not _is_number(default):
            raise _err(f"state '{key}': default must be a number")
        mn, mx = spec.get("min"), spec.get("max")
        if mn is not None and not _is_number(mn):
            raise _err(f"state '{key}': min must be a number")
        if mx is not None and not _is_number(mx):
            raise _err(f"state '{key}': max must be a number")
        if mn is not None and mx is not None and mn >= mx:
            raise _err(f"state '{key}': min ({mn}) must be < max ({mx})")
    elif stype == "bool":
        if default is not None and not isinstance(default, bool):
            raise _err(f"state '{key}': default must be true or false")
    elif stype == "string":
        if default is not None and not isinstance(default, str):
            raise _err(f"state '{key}': default must be a string")
        ml = spec.get("maxLength")
        if ml is not None and (not isinstance(ml, int) or isinstance(ml, bool) or ml <= 0):
            raise _err(f"state '{key}': maxLength must be a positive integer")
    elif stype == "timer":
        # Structured, managed state ({running, since_ms, accum_s}); the agent
        # doesn't set a default — a timer_toggle action drives it.
        pass


def _validate_computed_spec(key: str, spec: Any, series_keys: set[str]) -> None:
    if not isinstance(spec, dict):
        raise _err(f"computed '{key}' must be an object with `series`, `expr`, or `cases`")
    forms = [f for f in ("series", "expr", "cases") if f in spec]
    if len(forms) != 1:
        raise _err(f"computed '{key}' must have exactly one of `series`, `expr`, or `cases`")
    if forms[0] == "cases":
        _validate_cases_spec(key, spec)
        return
    has_series = forms[0] == "series"
    if has_series:
        s = spec["series"]
        if s not in series_keys:
            raise _err(
                f"computed '{key}': series '{s}' is not declared in top-level `series`"
            )
        agg = spec.get("agg", "sum")
        if agg not in catalog.AGGS:
            raise _err(f"computed '{key}': agg must be one of {sorted(catalog.AGGS)}")
        window = spec.get("window", "all")
        if window not in catalog.WINDOWS:
            raise _err(f"computed '{key}': window must be one of {sorted(catalog.WINDOWS)}")
    else:
        expr = spec["expr"]
        if not isinstance(expr, str) or not expr.strip():
            raise _err(f"computed '{key}': expr must be a non-empty string")
        # Full symbol/safety check happens in queries.eval_expr; here we only
        # confirm it parses under the safe grammar so a bad expr is rejected at
        # author time rather than silently resolving to 0 at render time.
        from flowly.flowlets.queries import validate_expr
        try:
            validate_expr(expr)
        except ValueError as exc:
            raise _err(f"computed '{key}': {exc}")


def _validate_cases_spec(key: str, spec: dict) -> None:
    """Conditional text: ``{cases: [{when, text}], else?}`` resolves to a string
    (the first truthy ``when`` wins; ``text`` may template ``{key}``)."""
    from flowly.flowlets.queries import validate_expr

    cases = spec["cases"]
    if not isinstance(cases, list) or not cases:
        raise _err(f"computed '{key}': `cases` must be a non-empty array of {{when, text}}")
    for i, case in enumerate(cases):
        where = f"computed '{key}' case #{i + 1}"
        if not isinstance(case, dict):
            raise _err(f"{where} must be an object with `when` and `text`")
        when = case.get("when")
        if not isinstance(when, str) or not when.strip():
            raise _err(f"{where}: `when` must be a non-empty expression string")
        try:
            validate_expr(when)
        except ValueError as exc:
            raise _err(f"{where}: when {exc}")
        if not isinstance(case.get("text"), str):
            raise _err(f"{where}: `text` must be a string")
    els = spec.get("else")
    if els is not None and not isinstance(els, str):
        raise _err(f"computed '{key}': `else` must be a string")


# ── layout tree validation ───────────────────────────────────────────────────

class _Ctx:
    __slots__ = ("scalar_keys", "series_keys", "component_ids", "count")

    def __init__(self, scalar_keys, series_keys, component_ids, count):
        self.scalar_keys = scalar_keys
        self.series_keys = series_keys
        self.component_ids = component_ids
        self.count = count


def _validate_node(node: Any, ctx: _Ctx, depth: int) -> None:
    if depth > catalog.MAX_DEPTH:
        raise _err(f"layout nested too deep (> {catalog.MAX_DEPTH} levels)")
    if not isinstance(node, dict):
        raise _err("every component must be a JSON object with a `type`")

    ctype = node.get("type")
    if ctype not in catalog.COMPONENT_TYPES:
        raise _err(
            f"unknown component type {ctype!r}. Valid types: "
            f"{', '.join(sorted(catalog.COMPONENT_TYPES))}"
        )
    spec = catalog.COMPONENTS[ctype]
    ctx.count += 1

    # id — required when the component carries an action or is otherwise
    # addressable; must be unique and not collide with scalar keys.
    cid = node.get("id")
    has_action = bool(node.get("action"))
    if cid is not None:
        if not isinstance(cid, str) or not _KEY_RE.match(cid):
            raise _err(
                f"component id '{cid}' is invalid; ids must start with a letter "
                "and contain only letters, digits, and underscores"
            )
        if cid in ctx.component_ids:
            raise _err(f"duplicate component id '{cid}'")
        # Only chart/sparkline/heatmap ids are written into the `values` map
        # (as their resolved series), so only those may not collide with a
        # scalar key. An `input` whose id equals the state key it writes is
        # both natural and safe.
        if ctype in catalog.SERIES_COMPONENTS and cid in ctx.scalar_keys:
            raise _err(
                f"chart component id '{cid}' collides with a state/computed key; "
                "give the chart a distinct id"
            )
        ctx.component_ids.add(cid)
    if has_action and cid is None:
        raise _err(f"{ctype} carries an action, so it needs a unique `id`")

    # Optional conditional visibility — any component may carry
    # `visibleWhen: "<expr>"`, evaluated client-side against live values (a
    # falsy result hides the node; on any evaluation error the client fails
    # open and shows it). Validated here so a typo'd key or bad grammar is
    # caught at author time instead of silently always-showing.
    vw = node.get("visibleWhen")
    if vw is not None:
        if not isinstance(vw, str) or not vw.strip():
            raise _err(f"{ctype} (id={cid}): `visibleWhen` must be a non-empty expression string")
        from flowly.flowlets.queries import validate_expr
        try:
            validate_expr(vw)
        except ValueError as exc:
            raise _err(f"{ctype} (id={cid}): visibleWhen {exc}")
        import ast as _ast
        for _n in _ast.walk(_ast.parse(vw, mode="eval")):
            if isinstance(_n, _ast.Name) and _n.id not in ctx.scalar_keys:
                raise _err(
                    f"{ctype} (id={cid}): visibleWhen references unknown key '{_n.id}' — "
                    "it must be a declared state or computed key"
                )

    # required props
    for prop in spec.get("required", []):
        if prop not in node:
            raise _err(f"{ctype} (id={cid}) is missing required prop `{prop}`")

    # label length guard (any string prop named text/label/title)
    for prop in ("text", "label", "title"):
        v = node.get(prop)
        if isinstance(v, str) and len(v) > catalog.MAX_LABEL_LEN:
            raise _err(f"{ctype}: `{prop}` exceeds {catalog.MAX_LABEL_LEN} characters")

    # scalar bindings — a binds prop is either a numeric literal or a known key
    for prop in spec.get("binds", []):
        if prop in node:
            _validate_scalar_ref(ctype, cid, prop, node[prop], ctx)

    # action
    if has_action:
        _validate_action(ctype, cid, node["action"], ctx)

    # series data (chart / sparkline / heatmap)
    if ctype in catalog.SERIES_COMPONENTS:
        _validate_data(ctype, cid, node.get("data"), ctx)

    # component-specific extra checks
    _validate_component_extras(ctype, cid, node, ctx)

    # children
    children = node.get("children")
    if spec.get("container"):
        if children is not None:
            if not isinstance(children, list):
                raise _err(f"{ctype} `children` must be an array")
            for child in children:
                _validate_node(child, ctx, depth + 1)
    elif children:
        raise _err(f"{ctype} cannot have children")


def _validate_scalar_ref(ctype, cid, prop, value, ctx: _Ctx) -> None:
    if _is_number(value):
        return
    if isinstance(value, str):
        if value not in ctx.scalar_keys:
            raise _err(
                f"{ctype} (id={cid}) `{prop}` references unknown key '{value}'. "
                "Declare it under `state` or `computed`, or use a number."
            )
        return
    raise _err(f"{ctype} (id={cid}) `{prop}` must be a number or a state/computed key name")


def _validate_action(ctype, cid, action, ctx: _Ctx) -> None:
    if not isinstance(action, dict):
        raise _err(f"{ctype} (id={cid}) `action` must be an object with an `op`")
    op = action.get("op")
    if op not in catalog.ACTION_OPS:
        raise _err(
            f"{ctype} (id={cid}) action op must be one of {sorted(catalog.ACTION_OPS)}, "
            f"got {op!r}"
        )

    if op in ("set", "increment", "decrement", "toggle"):
        key = action.get("key")
        if not isinstance(key, str) or key not in ctx.scalar_keys:
            raise _err(
                f"{ctype} (id={cid}) action `{op}` needs `key` naming a declared "
                f"state key; got {key!r}"
            )
        # toggle requires a bool state key isn't enforced here (renderer-neutral)
        if op in ("increment", "decrement") and "by" in action and not _is_number(action["by"]):
            raise _err(f"{ctype} (id={cid}) action `{op}` `by` must be a number")
    elif op in ("log", "remove_last"):
        series = action.get("series")
        if not isinstance(series, str) or series not in ctx.series_keys:
            raise _err(
                f"{ctype} (id={cid}) action `{op}` needs `series` naming a declared "
                f"series; got {series!r}"
            )
        if op == "log" and "value" in action and not _is_number(action["value"]):
            raise _err(f"{ctype} (id={cid}) action `log` `value` must be a number")
    elif op == "reset":
        key = action.get("key")
        series = action.get("series")
        if key is None and series is None:
            raise _err(f"{ctype} (id={cid}) action `reset` needs `key` or `series`")
        if key is not None and key not in ctx.scalar_keys:
            raise _err(f"{ctype} (id={cid}) action `reset` key '{key}' is not a state key")
        if series is not None and series not in ctx.series_keys:
            raise _err(f"{ctype} (id={cid}) action `reset` series '{series}' is not declared")
    elif op == "timer_toggle":
        key = action.get("key")
        if not isinstance(key, str) or key not in ctx.scalar_keys:
            raise _err(
                f"{ctype} (id={cid}) action `timer_toggle` needs `key` naming a "
                f"declared timer state; got {key!r}"
            )
    elif op == "agent":
        msg = action.get("message")
        if not isinstance(msg, str) or not msg.strip():
            raise _err(f"{ctype} (id={cid}) action `agent` needs a non-empty `message`")
        if len(msg) > 2000:
            raise _err(f"{ctype} (id={cid}) action `agent` message is too long (max 2000)")
    elif op == "batch":
        ops = action.get("ops")
        if not isinstance(ops, list) or not ops:
            raise _err(f"{ctype} (id={cid}) action `batch` needs a non-empty `ops` array")
        if len(ops) > 20:
            raise _err(f"{ctype} (id={cid}) action `batch` supports at most 20 ops")
        for sub in ops:
            if isinstance(sub, dict) and sub.get("op") == "batch":
                raise _err(f"{ctype} (id={cid}) action `batch` cannot nest another batch")
            _validate_action(ctype, cid, sub, ctx)


def _validate_data(ctype, cid, data, ctx: _Ctx) -> None:
    if not isinstance(data, dict):
        raise _err(f"{ctype} (id={cid}) `data` must be an object")
    series = data.get("series")
    if not isinstance(series, str) or series not in ctx.series_keys:
        raise _err(
            f"{ctype} (id={cid}) `data.series` must name a declared series; got {series!r}"
        )
    agg = data.get("agg", "sum")
    if agg not in catalog.AGGS:
        raise _err(f"{ctype} (id={cid}) `data.agg` must be one of {sorted(catalog.AGGS)}")
    bucket = data.get("bucket", "day")
    if bucket not in catalog.BUCKETS:
        raise _err(f"{ctype} (id={cid}) `data.bucket` must be one of {sorted(catalog.BUCKETS)}")
    window = data.get("window", "7d")
    if window not in catalog.WINDOWS:
        raise _err(f"{ctype} (id={cid}) `data.window` must be one of {sorted(catalog.WINDOWS)}")


def _validate_component_extras(ctype, cid, node, ctx: _Ctx) -> None:
    if ctype == "slider":
        mn, mx = node.get("min"), node.get("max")
        if not _is_number(mn) or not _is_number(mx):
            raise _err(f"slider (id={cid}) needs numeric `min` and `max`")
        if mn >= mx:
            raise _err(f"slider (id={cid}): min ({mn}) must be < max ({mx})")
        step = node.get("step")
        if step is not None and (not _is_number(step) or step <= 0):
            raise _err(f"slider (id={cid}): step must be a positive number")
    elif ctype == "checklist":
        items = node.get("items")
        if not isinstance(items, list) or not items:
            raise _err(f"checklist (id={cid}) needs a non-empty `items` array")
        for it in items:
            if not isinstance(it, dict) or "key" not in it:
                raise _err(f"checklist (id={cid}) items each need a `key`")
            k = it["key"]
            if k not in ctx.scalar_keys:
                raise _err(
                    f"checklist (id={cid}) item key '{k}' must be a declared state key"
                )
    elif ctype == "segmented":
        opts = node.get("options")
        if not isinstance(opts, list) or not opts:
            raise _err(f"segmented (id={cid}) needs a non-empty `options` array")
    elif ctype in ("input", "number_input"):
        # input writes to a state key via a `set` action; enforced by _validate_action
        pass
    elif ctype == "table":
        rows = node.get("rows")
        if not isinstance(rows, list):
            raise _err(f"table (id={cid}) `rows` must be an array")
    elif ctype == "countdown":
        # target is an epoch-ms number or an ISO string; accept both
        target = node.get("target")
        if not (_is_number(target) or isinstance(target, str)):
            raise _err(f"countdown (id={cid}) `target` must be a timestamp or ISO string")
    elif ctype == "keyvalue":
        rows = node.get("rows")
        if not isinstance(rows, list) or not rows:
            raise _err(f"keyvalue (id={cid}) needs a non-empty `rows` array")
        for r in rows:
            if not isinstance(r, dict) or "label" not in r:
                raise _err(f"keyvalue (id={cid}) each row needs a `label`")
    elif ctype == "timeline":
        events = node.get("events")
        if not isinstance(events, list) or not events:
            raise _err(f"timeline (id={cid}) needs a non-empty `events` array")
        for e in events:
            if not isinstance(e, dict) or "title" not in e:
                raise _err(f"timeline (id={cid}) each event needs a `title`")
    elif ctype == "link":
        url = node.get("url")
        if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            raise _err(f"link (id={cid}) `url` must be an http(s) URL")
    elif ctype == "image":
        src = node.get("src")
        if not isinstance(src, str) or not src.startswith(("http://", "https://", "data:")):
            raise _err(f"image (id={cid}) `src` must be an http(s) or data URL")
    elif ctype == "select":
        opts = node.get("options")
        if not isinstance(opts, list) or not opts:
            raise _err(f"select (id={cid}) needs a non-empty `options` array")
    elif ctype == "timer":
        val = node.get("value")
        if not isinstance(val, str) or val not in ctx.scalar_keys:
            raise _err(f"timer (id={cid}) `value` must name a declared timer state key")


# ── watches (reactive rules) ─────────────────────────────────────────────────

def _validate_watches(watches: Any, scalar_keys: set[str]) -> None:
    if not isinstance(watches, list):
        raise _err("`watches` must be an array of rule objects")
    if len(watches) > catalog.MAX_WATCHES:
        raise _err(f"too many watches ({len(watches)}); the limit is {catalog.MAX_WATCHES}")
    seen: set[str] = set()
    for i, w in enumerate(watches):
        _validate_watch(i, w, scalar_keys, seen)


def _validate_watch(i: int, w: Any, scalar_keys: set[str], seen: set[str]) -> None:
    where = f"watch #{i + 1}"
    if not isinstance(w, dict):
        raise _err(f"{where} must be an object")

    wid = w.get("id")
    if not isinstance(wid, str) or not _KEY_RE.match(wid):
        raise _err(
            f"{where} needs a stable string `id` (starts with a letter; "
            "letters, digits, underscore) — it keys the fire/cooldown state"
        )
    if wid in seen:
        raise _err(f"duplicate watch id '{wid}'")
    seen.add(wid)
    where = f"watch '{wid}'"

    trigger = w.get("trigger")
    if trigger not in catalog.WATCH_TRIGGERS:
        raise _err(
            f"{where}: `trigger` must be one of {sorted(catalog.WATCH_TRIGGERS)}, got {trigger!r}"
        )

    # notify — a watch that fires must have something to say.
    notify = w.get("notify")
    if not isinstance(notify, dict):
        raise _err(f"{where}: a `notify` object with a `title` is required")
    title = notify.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _err(f"{where}: notify.title must be a non-empty string")
    if len(title) > catalog.MAX_NAME_LEN:
        raise _err(f"{where}: notify.title must be ≤ {catalog.MAX_NAME_LEN} characters")
    body = notify.get("body")
    if body is not None and not isinstance(body, str):
        raise _err(f"{where}: notify.body must be a string")
    if isinstance(body, str) and len(body) > catalog.MAX_LABEL_LEN:
        raise _err(f"{where}: notify.body must be ≤ {catalog.MAX_LABEL_LEN} characters")
    # compose: the agent writes the notification text with live context when
    # the watch fires (title/body above stay as the deterministic fallback).
    if "compose" in notify and not isinstance(notify["compose"], bool):
        raise _err(f"{where}: notify.compose must be true or false")

    # trigger-specific fields
    if trigger == "schedule":
        _validate_watch_schedule(where, w)
    elif trigger in ("condition", "goal"):
        _validate_watch_expr(where, w, scalar_keys)
    elif trigger == "stale":
        idle = w.get("idleMinutes")
        if not isinstance(idle, int) or isinstance(idle, bool) or idle <= 0:
            raise _err(f"{where}: a stale watch needs a positive integer `idleMinutes`")

    # optional common fields
    days = w.get("days")
    if days is not None:
        if not isinstance(days, list) or not days or not all(
            isinstance(d, str) and d.lower() in catalog.WATCH_DAYS for d in days
        ):
            raise _err(f"{where}: `days` must be a non-empty list of {sorted(catalog.WATCH_DAYS)}")

    cooldown = w.get("cooldownMinutes")
    if cooldown is not None and (
        not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0
    ):
        raise _err(f"{where}: cooldownMinutes must be a non-negative integer")

    if "once" in w and not isinstance(w["once"], bool):
        raise _err(f"{where}: `once` must be true or false")

    # optional agent escape hatch — the ONLY side-effect a watch may trigger
    # besides the push itself.
    also = w.get("also")
    if also is not None:
        if not isinstance(also, dict):
            raise _err(f"{where}: `also` must be an object")
        if also.get("op") != "agent":
            raise _err(f'{where}: `also.op` must be "agent" (the only action a watch may trigger)')
        msg = also.get("message")
        if not isinstance(msg, str) or not msg.strip():
            raise _err(f"{where}: also.message must be a non-empty string")
        if len(msg) > catalog.MAX_WATCH_MESSAGE_LEN:
            raise _err(f"{where}: also.message must be ≤ {catalog.MAX_WATCH_MESSAGE_LEN} characters")


def _validate_watch_schedule(where: str, w: dict) -> None:
    at = w.get("at")
    every = w.get("everyMinutes")
    if at is None and every is None:
        raise _err(f'{where}: a schedule watch needs `at` ("HH:MM") or `everyMinutes`')
    if at is not None and (not isinstance(at, str) or not _HHMM_RE.match(at)):
        raise _err(f'{where}: `at` must be a 24-hour time like "20:00"')
    if every is not None and (not isinstance(every, int) or isinstance(every, bool) or every <= 0):
        raise _err(f"{where}: everyMinutes must be a positive integer")


def _validate_watch_expr(where: str, w: dict, scalar_keys: set[str]) -> None:
    when = w.get("when")
    if not isinstance(when, str) or not when.strip():
        raise _err(f"{where}: a `when` boolean expression is required (e.g. \"glasses < goal\")")
    from flowly.flowlets.queries import validate_expr

    try:
        validate_expr(when)
    except ValueError as exc:
        raise _err(f"{where}: when {exc}")
    # every referenced name must be a declared scalar (state or computed key),
    # so a typo is caught at author time rather than silently never firing.
    import ast

    for node in ast.walk(ast.parse(when, mode="eval")):
        if isinstance(node, ast.Name) and node.id not in scalar_keys:
            raise _err(
                f"{where}: `when` references unknown key '{node.id}' — it must be a "
                "declared state or computed key"
            )
    after = w.get("after")
    if after is not None and (not isinstance(after, str) or not _HHMM_RE.match(after)):
        raise _err(f'{where}: `after` must be a 24-hour time like "18:00"')
