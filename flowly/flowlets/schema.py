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


def _expr_key_refs(expr: str) -> set[str]:
    """The names an expr references that must be declared keys — i.e. every
    ``Name`` except those used as a function (``min``, ``days_until``, …). Lets
    author-time validation flag a typo'd key without mistaking a function for
    one."""
    import ast

    tree = ast.parse(expr, mode="eval")
    funcs = {
        c.func.id
        for c in ast.walk(tree)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in funcs}


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
    timer_keys: set[str] = set()     # state keys of type "timer"
    list_keys: dict[str, dict] = {}  # list state key → {field: type}
    source_keys: set[str] = set()    # state keys a `source` owns (user-read-only)
    for key, spec in state_defs.items():
        if not _KEY_RE.match(key):
            raise _err(
                f"state key '{key}' is invalid; keys must start with a letter "
                "and contain only letters, digits, and underscores"
            )
        _validate_state_spec(key, spec)
        if isinstance(spec, dict) and spec.get("source") is not None:
            if not isinstance(spec["source"], bool):
                raise _err(f"state '{key}': `source` must be true or false")
            if spec["source"]:
                source_keys.add(key)
        if isinstance(spec, dict) and spec.get("type") == "list":
            # A list resolves to an item array, not a scalar — it may not be
            # referenced by exprs/binds, only by a repeater / item ops.
            list_keys[key] = dict(spec.get("item") or {})
        else:
            scalar_keys.add(key)
            if isinstance(spec, dict) and spec.get("type") == "timer":
                timer_keys.add(key)

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
        if key in scalar_keys or key in list_keys:
            raise _err(f"computed key '{key}' collides with a state key of the same name")
        _validate_computed_spec(key, spec, series_keys, list_keys)
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
        list_keys=list_keys,
    )
    ctx.source_keys = source_keys
    ctx.timer_keys = timer_keys
    ctx.screens = _validate_screens_structure(defn.get("screens"))
    for node in layout:
        _validate_node(node, ctx, depth=1)

    if ctx.count > catalog.MAX_COMPONENTS:
        raise _err(f"too many components ({ctx.count}); the limit is {catalog.MAX_COMPONENTS}")

    # search targets are checked after the walk (a `search` may precede its
    # target repeater/table in reading order).
    for cid, target, fields in ctx.searches:
        if target not in ctx.filterable:
            raise _err(
                f"search (id={cid}) `target` must name a repeater or data-bound table id; "
                f"got {target!r}"
            )
        if fields is not None:
            list_fields = ctx.list_keys.get(ctx.filterable[target], {})
            for f in fields:
                if f not in list_fields:
                    raise _err(
                        f"search (id={cid}) `fields` entry '{f}' is not a field of the "
                        f"target's list (declared: {sorted(list_fields)})"
                    )

    # ── drill-down screens — validated against their navigator's item scope ────
    for sid in ctx.screens:
        if sid not in ctx.navigations:
            raise _err(
                f"screen '{sid}' is never navigated to — add `navigate: \"{sid}\"` on a "
                "repeater or data-bound table"
            )
    for sid, list_key in ctx.navigations.items():
        _validate_screen_layout(sid, ctx.screens[sid], ctx, list_key)

    # ── watches (reactive rules; evaluated LLM-free — see watches.py) ─────────
    watches = defn.get("watches")
    if watches is not None:
        _validate_watches(watches, scalar_keys)

    # ── sources (live/external data bindings — see sources.py) ────────────────
    sources = defn.get("sources")
    if sources is not None:
        _validate_sources(sources, state_defs, source_keys)
    # Every source-owned key must actually be written by a source (else it's a
    # dead read-only key the user can never fill).
    written = {s.get("into") for s in (sources or {}).values() if isinstance(s, dict)}
    for k in source_keys:
        if k not in written:
            raise _err(f"state '{k}' is marked `source:true` but no source writes it")

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
    elif stype == "list":
        item = spec.get("item")
        if not isinstance(item, dict) or not item:
            raise _err(
                f"state '{key}': a list needs an `item` field schema, e.g. "
                '{"title": "string", "done": "bool"}'
            )
        if len(item) > catalog.MAX_ITEM_FIELDS:
            raise _err(f"state '{key}': at most {catalog.MAX_ITEM_FIELDS} item fields")
        for fname, ftype in item.items():
            if not _KEY_RE.match(fname):
                raise _err(f"state '{key}': item field '{fname}' is an invalid name")
            if fname == "id":
                raise _err(f"state '{key}': `id` is reserved (assigned automatically)")
            if ftype not in catalog.ITEM_FIELD_TYPES:
                raise _err(
                    f"state '{key}': item field '{fname}' type must be one of "
                    f"{sorted(catalog.ITEM_FIELD_TYPES)}, got {ftype!r}"
                )
        mx = spec.get("max")
        if mx is not None and (
            not isinstance(mx, int) or isinstance(mx, bool)
            or not 1 <= mx <= catalog.MAX_LIST_ITEMS
        ):
            raise _err(f"state '{key}': `max` must be an integer 1..{catalog.MAX_LIST_ITEMS}")


def _validate_computed_spec(
    key: str, spec: Any, series_keys: set[str], list_keys: dict[str, dict] | None = None
) -> None:
    list_keys = list_keys or {}
    if not isinstance(spec, dict):
        raise _err(f"computed '{key}' must be an object with `series`, `list`, `expr`, or `cases`")
    forms = [f for f in ("series", "list", "expr", "cases") if f in spec]
    if len(forms) != 1:
        raise _err(f"computed '{key}' must have exactly one of `series`, `list`, `expr`, or `cases`")
    if forms[0] == "cases":
        _validate_cases_spec(key, spec)
        return
    if forms[0] == "list":
        _validate_list_agg_spec(key, spec, list_keys)
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


def _validate_sources(sources: Any, state_defs: dict, source_keys: set[str]) -> None:
    """Live data bindings: ``{name: {kind, prompt, into, refresh?, limit?}}``.
    Each writes a source-owned state key on a schedule (see sources.py)."""
    if not isinstance(sources, dict):
        raise _err("`sources` must be an object of {name: {kind, prompt, into, …}}")
    if len(sources) > catalog.MAX_SOURCES:
        raise _err(f"too many sources ({len(sources)}); the limit is {catalog.MAX_SOURCES}")
    seen_into: set[str] = set()
    for name, spec in sources.items():
        where = f"source '{name}'"
        if not _KEY_RE.match(name):
            raise _err(f"source name '{name}' is invalid (letters/digits/underscore)")
        if not isinstance(spec, dict):
            raise _err(f"{where} must be an object")
        kind = spec.get("kind")
        if kind not in catalog.SOURCE_KINDS:
            raise _err(f"{where}: kind must be one of {sorted(catalog.SOURCE_KINDS)}, got {kind!r}")
        prompt = spec.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _err(f"{where}: a non-empty `prompt` is required (what data to fetch)")
        if len(prompt) > catalog.MAX_SOURCE_PROMPT_LEN:
            raise _err(f"{where}: prompt must be ≤ {catalog.MAX_SOURCE_PROMPT_LEN} characters")
        into = spec.get("into")
        if into not in source_keys:
            raise _err(
                f"{where}: `into` must name a state key declared with `source: true`; got {into!r}"
            )
        if into in seen_into:
            raise _err(f"{where}: two sources both write '{into}'")
        seen_into.add(into)
        refresh = spec.get("refresh", "manual")
        if refresh != "manual":
            mins = _parse_refresh_minutes(refresh)
            if mins is None:
                raise _err(f'{where}: refresh must be "manual" or like "15m" / "1h"')
            floor = catalog.SOURCE_MIN_REFRESH_MIN.get(kind, 10)
            if mins < floor:
                raise _err(f"{where}: refresh for a {kind} source must be ≥ {floor}m")
        limit = spec.get("limit")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool)
            or not 1 <= limit <= catalog.MAX_LIST_ITEMS
        ):
            raise _err(f"{where}: limit must be an integer 1..{catalog.MAX_LIST_ITEMS}")


def _parse_refresh_minutes(v: Any) -> int | None:
    """"15m" / "2h" → minutes; None if malformed."""
    if not isinstance(v, str):
        return None
    m = re.match(r"^\s*(\d+)\s*([mh])\s*$", v)
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2) == "m" else n * 60


def _validate_list_agg_spec(key: str, spec: dict, list_keys: dict[str, dict]) -> None:
    """Conditional aggregation of a dynamic list: ``{list, agg, field?, where?}``
    → a scalar. ``where`` is an expr over the item's own fields."""
    from flowly.flowlets.queries import validate_expr

    lk = spec["list"]
    if lk not in list_keys:
        raise _err(f"computed '{key}': `list` must name a declared list state key; got {lk!r}")
    fields = list_keys[lk]
    agg = spec.get("agg", "count")
    if agg not in ("count", "sum", "avg", "min", "max"):
        raise _err(f"computed '{key}': agg must be one of count/sum/avg/min/max, got {agg!r}")
    if agg != "count":
        field = spec.get("field")
        if field not in fields:
            raise _err(f"computed '{key}': `{agg}` needs a declared numeric `field` of '{lk}'")
        if fields[field] != "number":
            raise _err(f"computed '{key}': field '{field}' must be a number to `{agg}` it")
    where = spec.get("where")
    if where is not None:
        if not isinstance(where, str) or not where.strip():
            raise _err(f"computed '{key}': `where` must be a non-empty expression")
        try:
            validate_expr(where)
        except ValueError as exc:
            raise _err(f"computed '{key}': where {exc}")
        # names in `where` are item fields (or a date literal); a typo is caught
        # here rather than silently excluding every row.
        for name in _expr_key_refs(where):
            if name not in fields:
                raise _err(
                    f"computed '{key}': where references unknown item field '{name}' "
                    f"(declared: {sorted(fields)})"
                )


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
    __slots__ = (
        "scalar_keys", "series_keys", "component_ids", "count",
        "list_keys", "item_fields", "item_source", "source_keys",
        "filterable", "searches", "screens", "navigations", "timer_keys",
    )

    def __init__(self, scalar_keys, series_keys, component_ids, count, list_keys=None):
        self.scalar_keys = scalar_keys
        self.series_keys = series_keys
        self.component_ids = component_ids
        self.count = count
        self.timer_keys = set()            # state keys declared type "timer"
        self.list_keys = list_keys or {}   # list state key → {field: type}
        self.item_fields = None            # {field: type} while inside a repeater template
        self.item_source = None            # the repeater's source key, ditto
        self.source_keys = set()           # state keys a `source` owns (user-read-only)
        self.filterable = {}               # id of a repeater/source-table → its list key
        self.searches = []                 # (cid, target, fields) — checked after the walk
        self.screens = {}                  # screenId → screen def (drill-down fragments)
        self.navigations = {}              # screenId → the list key that navigates to it


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
        for _name in _expr_key_refs(vw):
            if _name not in ctx.scalar_keys:
                raise _err(
                    f"{ctype} (id={cid}): visibleWhen references unknown key '{_name}' — "
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
        _validate_data(ctype, cid, node.get("data"), ctx, node.get("kind"))

    # component-specific extra checks
    _validate_component_extras(ctype, cid, node, ctx, depth)

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
        if value.startswith("$."):
            # An item-field bind, valid only inside a repeater's item template.
            if ctx.item_fields is None:
                raise _err(
                    f"{ctype} (id={cid}) `{prop}` uses '{value}' outside a repeater "
                    "item template"
                )
            field = value[2:]
            if field not in ctx.item_fields:
                raise _err(
                    f"{ctype} (id={cid}) `{prop}` references unknown item field "
                    f"'{field}' (declared: {sorted(ctx.item_fields)})"
                )
            return
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

    # A source owns its state key; the user can't write it (the source snapshot
    # would be clobbered / re-overwritten). Read-only from the UI.
    tgt = action.get("key")
    if isinstance(tgt, str) and tgt in ctx.source_keys and op in (
        "set", "increment", "decrement", "toggle", "reset", "timer_toggle",
        "item_add", "item_update", "item_remove", "item_toggle", "item_move",
    ):
        raise _err(
            f"{ctype} (id={cid}) action `{op}` targets '{tgt}', which is owned by a "
            "data source and is read-only"
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
        # A logged event may carry a `category` (a literal or a "{token}" that
        # templates from live values) → it feeds categorical pie/donut charts.
        if op == "log" and "category" in action:
            cat = action["category"]
            if not isinstance(cat, str) or not cat.strip() or len(cat) > 120:
                raise _err(
                    f"{ctype} (id={cid}) action `log` `category` must be a non-empty string "
                    "(≤120 chars; may contain {tokens})"
                )
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
        if not isinstance(key, str) or key not in ctx.timer_keys:
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
    elif op == "item_add":
        key = action.get("key")
        if not isinstance(key, str) or key not in ctx.list_keys:
            raise _err(
                f"{ctype} (id={cid}) action `item_add` needs `key` naming a declared "
                f"list state key; got {key!r} (declared lists: {sorted(ctx.list_keys)})"
            )
        fixed = action.get("item")
        if fixed is not None:
            if not isinstance(fixed, dict):
                raise _err(f"{ctype} (id={cid}) `item_add` `item` must be an object of fields")
            for f in fixed:
                if f not in ctx.list_keys[key]:
                    raise _err(
                        f"{ctype} (id={cid}) `item_add` sets unknown field '{f}' "
                        f"(declared: {sorted(ctx.list_keys[key])})"
                    )
        # `fields` is a TEMPLATED form: each value may carry `{value}` (what the
        # user typed) / `{state_key}` tokens / a `today` date sentinel, resolved
        # at write time (like the `log` op's `category`).
        tpl_fields = action.get("fields")
        if tpl_fields is not None:
            if not isinstance(tpl_fields, dict):
                raise _err(f"{ctype} (id={cid}) `item_add` `fields` must be an object of field templates")
            for f in tpl_fields:
                if f not in ctx.list_keys[key]:
                    raise _err(
                        f"{ctype} (id={cid}) `item_add` `fields` sets unknown field '{f}' "
                        f"(declared: {sorted(ctx.list_keys[key])})"
                    )
    elif op in ("item_update", "item_toggle", "item_remove", "item_move"):
        key = action.get("key")
        if not isinstance(key, str) or key not in ctx.list_keys:
            raise _err(
                f"{ctype} (id={cid}) action `{op}` needs `key` naming a declared "
                f"list state key; got {key!r} (declared lists: {sorted(ctx.list_keys)})"
            )
        # These need the tapped row's itemId, which only a repeater bound to the
        # same list can supply.
        if ctx.item_source != key:
            raise _err(
                f"{ctype} (id={cid}) action `{op}` must sit inside the repeater whose "
                f"`source` is '{key}'"
            )
        if op == "item_toggle":
            field = action.get("field")
            if field not in ctx.list_keys[key]:
                raise _err(f"{ctype} (id={cid}) `item_toggle` needs a declared `field`")
            if ctx.list_keys[key][field] != "bool":
                raise _err(f"{ctype} (id={cid}) `item_toggle` field '{field}' must be bool")
        elif op == "item_update":
            field = action.get("field")
            fields = action.get("fields")
            if (field is None) == (fields is None):
                raise _err(
                    f"{ctype} (id={cid}) `item_update` needs exactly one of `field` "
                    "(client value) or `fields` (fixed values)"
                )
            if field is not None and field not in ctx.list_keys[key]:
                raise _err(f"{ctype} (id={cid}) `item_update` unknown field '{field}'")
            if fields is not None:
                if not isinstance(fields, dict) or not fields:
                    raise _err(f"{ctype} (id={cid}) `item_update` `fields` must be a non-empty object")
                for f in fields:
                    if f not in ctx.list_keys[key]:
                        raise _err(f"{ctype} (id={cid}) `item_update` unknown field '{f}'")
    elif op == "vision":
        prompt = action.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _err(f"{ctype} (id={cid}) action `vision` needs a non-empty `prompt`")
        if len(prompt) > catalog.MAX_VISION_PROMPT_LEN:
            raise _err(
                f"{ctype} (id={cid}) action `vision` prompt is too long "
                f"(max {catalog.MAX_VISION_PROMPT_LEN})"
            )
        into = action.get("into")
        if not isinstance(into, str) or into not in ctx.list_keys:
            raise _err(
                f"{ctype} (id={cid}) action `vision` `into` must name a declared list "
                f"state key; got {into!r}"
            )
        if into in ctx.source_keys:
            raise _err(
                f"{ctype} (id={cid}) action `vision` `into` targets '{into}', which is "
                "owned by a data source and is read-only"
            )
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


def _validate_data(ctype, cid, data, ctx: _Ctx, kind=None) -> None:
    """Validate a chart-family `data` prop. Six forms, detected by shape:

    * scatter    — ``{list, x, y}``                     (chart only; client-drawn)
    * list time  — ``{list, field?, date?, bucket?, window?}`` (chart only;
                    aggregates the rows themselves — survives edits/deletes)
    * list cat.  — ``{list, by:"<string field>", field?, agg?}`` (chart only)
    * category   — ``{series:"k", by:"category", agg?}`` (chart only; pie/donut)
    * multi      — ``{series:[{key,label?,color?}], …}`` (chart only; overlay)
    * single     — ``{series:"k", agg?, bucket?, window?}`` (unchanged; all three)
    """
    if not isinstance(data, dict):
        raise _err(f"{ctype} (id={cid}) `data` must be an object")

    is_chart = ctype == "chart"
    if "list" in data:  # ── list-backed (scatter / time / category) ──
        if not is_chart:
            raise _err(f"{ctype} (id={cid}) list-backed `data` is only for `chart`")
        if "x" in data or "y" in data:
            _validate_scatter_data(cid, data, ctx)
        else:
            _validate_list_chart_data(cid, data, ctx)
        return
    if data.get("by") is not None:  # ── categorical breakdown ──
        if not is_chart:
            raise _err(f"{ctype} (id={cid}) categorical `data.by` is only for `chart`")
        _validate_category_data(cid, data, ctx)
        return

    series = data.get("series")
    if isinstance(series, list):  # ── multi-series overlay ──
        if not is_chart:
            raise _err(f"{ctype} (id={cid}) a multi-series `data.series` is only for `chart`")
        _validate_multi_series_data(cid, data, ctx, kind)
        _validate_time_axis(ctype, cid, data)
        return

    # ── single time series (sparkline / heatmap / plain chart) ──
    if not isinstance(series, str) or series not in ctx.series_keys:
        raise _err(
            f"{ctype} (id={cid}) `data.series` must name a declared series; got {series!r}"
        )
    _validate_time_axis(ctype, cid, data)


def _validate_time_axis(ctype, cid, data) -> None:
    agg = data.get("agg", "sum")
    if agg not in catalog.AGGS:
        raise _err(f"{ctype} (id={cid}) `data.agg` must be one of {sorted(catalog.AGGS)}")
    bucket = data.get("bucket", "day")
    if bucket not in catalog.BUCKETS:
        raise _err(f"{ctype} (id={cid}) `data.bucket` must be one of {sorted(catalog.BUCKETS)}")
    window = data.get("window", "7d")
    if window not in catalog.WINDOWS:
        raise _err(f"{ctype} (id={cid}) `data.window` must be one of {sorted(catalog.WINDOWS)}")


def _validate_multi_series_data(cid, data, ctx: _Ctx, kind=None) -> None:
    entries = data["series"]
    if not (2 <= len(entries) <= catalog.MAX_CHART_SERIES):
        raise _err(
            f"chart (id={cid}) a multi-series `data.series` needs 2–"
            f"{catalog.MAX_CHART_SERIES} entries; got {len(entries)}"
        )
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, dict):
            raise _err(f"chart (id={cid}) each `data.series` entry must be an object with a `key`")
        key = e.get("key")
        if not isinstance(key, str) or key not in ctx.series_keys:
            raise _err(
                f"chart (id={cid}) series entry `key` must name a declared series; got {key!r}"
            )
        if key in seen:
            raise _err(f"chart (id={cid}) series '{key}' is listed twice")
        seen.add(key)
        label = e.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > catalog.MAX_LABEL_LEN):
            raise _err(f"chart (id={cid}) series '{key}' `label` must be a short string")
        color = e.get("color")
        if color is not None and not (isinstance(color, str) and _HEX_RE.match(color)):
            raise _err(f"chart (id={cid}) series '{key}' `color` must be a #hex value")
    stacked = data.get("stacked")
    if stacked is not None:
        if not isinstance(stacked, bool):
            raise _err(f"chart (id={cid}) `data.stacked` must be true/false")
        if stacked and kind not in ("bar", None):
            raise _err(f"chart (id={cid}) `stacked` is only valid for a bar chart")


def _validate_category_data(cid, data, ctx: _Ctx) -> None:
    series = data.get("series")
    if not isinstance(series, str) or series not in ctx.series_keys:
        raise _err(
            f"chart (id={cid}) a categorical chart needs `data.series` naming a declared "
            f"series; got {series!r}"
        )
    if data.get("by") != "category":
        raise _err(f"chart (id={cid}) `data.by` must be \"category\"")
    agg = data.get("agg", "sum")
    if agg not in catalog.CATEGORY_AGGS:
        raise _err(
            f"chart (id={cid}) a categorical `data.agg` must be one of "
            f"{sorted(catalog.CATEGORY_AGGS)} (a slice is a total or a tally)"
        )
    if "bucket" in data:
        raise _err(f"chart (id={cid}) a categorical chart has no time axis — drop `bucket`")
    window = data.get("window", "30d")
    if window not in catalog.WINDOWS:
        raise _err(f"chart (id={cid}) `data.window` must be one of {sorted(catalog.WINDOWS)}")


def _validate_list_chart_data(cid, data, ctx: _Ctx) -> None:
    """A chart aggregating a list's ROWS (no parallel series to drift):

    * time     — ``{list, field?, date?, bucket?, window?}``; needs a date
                 field (explicit ``date`` or one declared on the item schema).
    * category — ``{list, by:"<string field>", field?, agg?, window?}``;
                 groups rows by a string field (sum a number field, or count).
    """
    lk = data["list"]
    if not isinstance(lk, str) or lk not in ctx.list_keys:
        raise _err(
            f"chart (id={cid}) `data.list` must name a declared list state key; got {lk!r}"
        )
    fields = ctx.list_keys[lk]

    field = data.get("field")
    if field is not None and fields.get(field) != "number":
        raise _err(
            f"chart (id={cid}) `data.field` must name a number field of list '{lk}'; "
            f"got {field!r}"
        )
    date_f = data.get("date")
    if date_f is not None and fields.get(date_f) != "date":
        raise _err(
            f"chart (id={cid}) `data.date` must name a date field of list '{lk}'; "
            f"got {date_f!r}"
        )

    by = data.get("by")
    if by is not None:  # ── categorical from rows ──
        if not isinstance(by, str) or fields.get(by) != "string":
            raise _err(
                f"chart (id={cid}) `data.by` must name a string field of list '{lk}' "
                f"to group by; got {by!r}"
            )
        agg = data.get("agg", "sum")
        if agg not in catalog.CATEGORY_AGGS:
            raise _err(
                f"chart (id={cid}) a categorical `data.agg` must be one of "
                f"{sorted(catalog.CATEGORY_AGGS)} (a slice is a total or a tally)"
            )
        if agg == "sum" and field is None:
            raise _err(
                f"chart (id={cid}) a sum-by-category chart needs `data.field` naming "
                f"the number field of list '{lk}' to total"
            )
        if "bucket" in data:
            raise _err(f"chart (id={cid}) a categorical chart has no time axis — drop `bucket`")
        window = data.get("window", "30d")
        if window not in catalog.WINDOWS:
            raise _err(f"chart (id={cid}) `data.window` must be one of {sorted(catalog.WINDOWS)}")
        return

    # ── time series from rows ──
    if date_f is None and not any(t == "date" for t in fields.values()):
        raise _err(
            f"chart (id={cid}) a time chart over list '{lk}' needs a date field "
            f"(declare one on the item schema or set `data.date`)"
        )
    _validate_time_axis("chart", cid, data)


def _validate_scatter_data(cid, data, ctx: _Ctx) -> None:
    lk = data.get("list")
    if not isinstance(lk, str) or lk not in ctx.list_keys:
        raise _err(
            f"chart (id={cid}) a scatter `data.list` must name a declared list state key; "
            f"got {lk!r}"
        )
    fields = ctx.list_keys[lk]
    for axis in ("x", "y"):
        f = data.get(axis)
        if not isinstance(f, str) or f not in fields:
            raise _err(
                f"chart (id={cid}) scatter `data.{axis}` must name a field of list '{lk}'; got {f!r}"
            )
        if fields[f] != "number":
            raise _err(
                f"chart (id={cid}) scatter `data.{axis}` field '{f}' must be a number "
                f"(is {fields[f]})"
            )


def _validate_table(cid, node, ctx: _Ctx) -> None:
    """A table is either static `rows` or a data-bound `source` + `columns`
    (exactly one). Source mode renders one row per item of a `list` state key,
    with header-tap sorting on the client."""
    has_rows = "rows" in node
    src = node.get("source")
    has_source = isinstance(src, str)
    if has_rows == has_source:
        raise _err(
            f"table (id={cid}) needs exactly one of `rows` (static) or `source` (data-bound)"
        )
    if has_rows:
        if not isinstance(node["rows"], list):
            raise _err(f"table (id={cid}) `rows` must be an array")
        return

    # ── data-bound mode ──
    if src not in ctx.list_keys:
        raise _err(
            f"table (id={cid}) `source` must name a declared list state key; got {src!r}"
        )
    fields = ctx.list_keys[src]
    if cid:
        ctx.filterable[cid] = src   # a `search` may target this table
    if node.get("navigate") is not None:
        _register_navigate(f"table (id={cid})", node["navigate"], src, ctx)
    cols = node.get("columns")
    if not isinstance(cols, list) or not (1 <= len(cols) <= catalog.MAX_TABLE_COLUMNS):
        raise _err(
            f"table (id={cid}) `columns` must be 1–{catalog.MAX_TABLE_COLUMNS} column objects"
        )
    col_fields: set[str] = set()
    for c in cols:
        if not isinstance(c, dict):
            raise _err(f"table (id={cid}) each column must be an object with a `field`")
        f = c.get("field")
        if not isinstance(f, str) or f not in fields:
            raise _err(
                f"table (id={cid}) column `field` must name a field of list '{src}'; got {f!r}"
            )
        col_fields.add(f)
        align = c.get("align")
        if align is not None and align not in catalog.TABLE_ALIGNS:
            raise _err(f"table (id={cid}) column `align` must be one of {sorted(catalog.TABLE_ALIGNS)}")
        label = c.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > catalog.MAX_LABEL_LEN):
            raise _err(f"table (id={cid}) column `label` must be a short string")
        width = c.get("width")
        if width is not None and not isinstance(width, str):
            raise _err(f'table (id={cid}) column `width` must be a percent string like "20%"')
    sort_by = node.get("sortBy")
    if sort_by is not None:
        if not isinstance(sort_by, dict):
            raise _err(f"table (id={cid}) `sortBy` must be an object {{field, dir}}")
        sf = sort_by.get("field")
        if sf not in col_fields:
            raise _err(
                f"table (id={cid}) `sortBy.field` must be one of the table's columns; got {sf!r}"
            )
        if sort_by.get("dir", "asc") not in catalog.SORT_DIRS:
            raise _err(f"table (id={cid}) `sortBy.dir` must be asc or desc")
    empty = node.get("empty")
    if empty is not None and not isinstance(empty, str):
        raise _err(f"table (id={cid}) `empty` must be a string")
    where = node.get("where")
    if where is not None:
        _validate_item_where(f"table (id={cid})", where, fields)


def _validate_screens_structure(screens: Any) -> dict:
    """Shape-check the top-level `screens` map (drill-down fragments). The layout
    of each screen is validated later, against its navigator's item scope."""
    if screens is None:
        return {}
    if not isinstance(screens, dict):
        raise _err("`screens` must be an object of {screenId: {title?, layout}}")
    if len(screens) > catalog.MAX_SCREENS:
        raise _err(f"too many screens (max {catalog.MAX_SCREENS})")
    for sid, sdef in screens.items():
        if not _KEY_RE.match(sid):
            raise _err(f"screen id '{sid}' is invalid (start with a letter; letters/digits/_)")
        if not isinstance(sdef, dict):
            raise _err(f"screen '{sid}' must be an object with a `layout`")
        title = sdef.get("title")
        if title is not None and not isinstance(title, str):
            raise _err(f"screen '{sid}' `title` must be a string")
        layout = sdef.get("layout")
        if not isinstance(layout, list) or not layout:
            raise _err(f"screen '{sid}' `layout` must be a non-empty array of components")
    return screens


def _register_navigate(label: str, nav: Any, list_key: str, ctx: _Ctx) -> None:
    """A repeater/table row carries `navigate: <screenId>`; the tapped item is
    pushed into that screen. Only at top level — never inside a screen/row."""
    if ctx.item_fields is not None:
        raise _err(f"{label} `navigate` isn't allowed inside a screen (v1 is one level deep)")
    if not isinstance(nav, str) or nav not in ctx.screens:
        raise _err(
            f"{label} `navigate` must name a screen declared in top-level `screens`; got {nav!r}"
        )
    ctx.navigations.setdefault(nav, list_key)


def _validate_screen_layout(sid: str, sdef: dict, ctx: _Ctx, list_key: str) -> None:
    """Validate a screen's layout with the navigating list's item in scope, so
    `$.field` binds and the row's item ops resolve. A fresh id namespace (a
    screen is a separate fragment) that never collides with the main layout."""
    sub = _Ctx(
        scalar_keys=ctx.scalar_keys,
        series_keys=ctx.series_keys,
        component_ids=set(),
        count=0,
        list_keys=ctx.list_keys,
    )
    sub.source_keys = ctx.source_keys
    sub.timer_keys = ctx.timer_keys
    sub.screens = ctx.screens
    sub.item_fields = ctx.list_keys.get(list_key, {})
    sub.item_source = list_key
    for node in sdef["layout"]:
        _validate_node(node, sub, depth=2)  # already one level in (pushed from a row)
    if sub.count > catalog.MAX_COMPONENTS:
        raise _err(f"screen '{sid}' has too many components (max {catalog.MAX_COMPONENTS})")


def _validate_item_where(label: str, where: Any, fields: dict) -> None:
    """A per-item filter expr — evaluated client-side per row; its key refs must
    be fields of the list it filters."""
    if not isinstance(where, str) or not where.strip():
        raise _err(f"{label} `where` must be a non-empty expression string")
    from flowly.flowlets.queries import validate_expr
    try:
        validate_expr(where)
    except ValueError as exc:
        raise _err(f"{label} where {exc}")
    for name in _expr_key_refs(where):
        if name not in fields:
            raise _err(
                f"{label} `where` references unknown field '{name}' "
                f"(declared: {sorted(fields)})"
            )


def _validate_item_sortby(label: str, sort_by: Any, fields: dict) -> None:
    if not isinstance(sort_by, dict):
        raise _err(f"{label} `sortBy` must be an object {{field, dir}}")
    f = sort_by.get("field")
    if f not in fields:
        raise _err(f"{label} `sortBy.field` must be a field of the list; got {f!r}")
    if sort_by.get("dir", "asc") not in catalog.SORT_DIRS:
        raise _err(f"{label} `sortBy.dir` must be asc or desc")


def _validate_component_extras(ctype, cid, node, ctx: _Ctx, depth: int = 1) -> None:
    if ctype == "repeater":
        source = node.get("source")
        if not isinstance(source, str) or source not in ctx.list_keys:
            raise _err(
                f"repeater (id={cid}) `source` must name a declared list state key; "
                f"got {source!r}"
            )
        if ctx.item_fields is not None:
            raise _err(f"repeater (id={cid}) cannot nest inside another repeater")
        empty = node.get("empty")
        if empty is not None and not isinstance(empty, str):
            raise _err(f"repeater (id={cid}) `empty` must be a string")
        fields = ctx.list_keys[source]
        if cid:
            ctx.filterable[cid] = source   # a `search` may target this repeater
        if node.get("where") is not None:
            _validate_item_where(f"repeater (id={cid})", node["where"], fields)
        if node.get("sortBy") is not None:
            _validate_item_sortby(f"repeater (id={cid})", node["sortBy"], fields)
        if node.get("navigate") is not None:
            _register_navigate(f"repeater (id={cid})", node["navigate"], source, ctx)
        item = node.get("item")
        if not isinstance(item, dict):
            raise _err(f"repeater (id={cid}) `item` must be a component object (the row template)")
        # Validate the row template with the item scope open so `$.field` binds
        # and item_* ops resolve against this list's fields.
        ctx.item_fields = ctx.list_keys[source]
        ctx.item_source = source
        try:
            _validate_node(item, ctx, depth + 1)
        finally:
            ctx.item_fields = None
            ctx.item_source = None
        return
    if ctype == "search":
        target = node.get("target")
        if not isinstance(target, str) or not target.strip():
            raise _err(f"search (id={cid}) needs a `target` (a repeater/table id)")
        fields = node.get("fields")
        if fields is not None:
            if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
                raise _err(f"search (id={cid}) `fields` must be an array of field names")
        placeholder = node.get("placeholder")
        if placeholder is not None and not isinstance(placeholder, str):
            raise _err(f"search (id={cid}) `placeholder` must be a string")
        # target/fields are cross-checked after the full walk (forward refs).
        ctx.searches.append((cid, target, fields))
        return
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
        _validate_table(cid, node, ctx)
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
        # An http(s)/data URL, OR a `$.field` item ref (resolves per-row to a URL
        # or a stored-photo attachment id — the client fetches it), OR a state key
        # holding one.
        ok = isinstance(src, str) and (
            src.startswith(("http://", "https://", "data:", "$."))
            or _KEY_RE.match(src) is not None
        )
        if not ok:
            raise _err(
                f"image (id={cid}) `src` must be an http(s)/data URL, a `$.field` ref, "
                "or a state/computed key"
            )
    elif ctype == "select":
        opts = node.get("options")
        if not isinstance(opts, list) or not opts:
            raise _err(f"select (id={cid}) needs a non-empty `options` array")
    elif ctype == "timer":
        val = node.get("value")
        if not isinstance(val, str) or val not in ctx.timer_keys:
            raise _err(f"timer (id={cid}) `value` must name a declared timer state key")
    elif ctype == "list_row":
        _validate_list_row(cid, node, ctx)
    elif ctype == "form":
        _validate_form(cid, node, ctx)
    elif ctype == "tracker_card":
        _validate_tracker_card(cid, node, ctx)


_LIST_ROW_PROPS = ("title", "subtitle", "value", "badge", "thumb")
_ITEM_REF_RE = re.compile(r"\$\.([a-zA-Z][a-zA-Z0-9_]*)")


def _validate_list_row(cid, node, ctx: _Ctx) -> None:
    """A ``list_row`` composite is ONLY valid as a repeater's row template, and
    every ``$.field`` it names must be a field of that list. It expands (see
    composites.py) to the canonical row — the agent never lays it out."""
    if ctx.item_fields is None:
        raise _err(
            f"list_row (id={cid}) is only valid as a repeater's `item` template "
            "(it renders one list row); put it inside a repeater"
        )
    for prop in _LIST_ROW_PROPS:
        v = node.get(prop)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            raise _err(f"list_row (id={cid}) `{prop}` must be a non-empty string")
        for field in _ITEM_REF_RE.findall(v):
            if field not in ctx.item_fields:
                raise _err(
                    f"list_row (id={cid}) `{prop}` references unknown item field "
                    f"'{field}' (declared: {sorted(ctx.item_fields)})"
                )


def _mutable_list(cid, ctype, key, ctx: _Ctx) -> dict:
    """The item schema of a USER-owned (non-source) list ``key``, or raise."""
    if not isinstance(key, str) or key not in ctx.list_keys:
        raise _err(
            f"{ctype} (id={cid}) must name a declared list state key; got {key!r}"
        )
    if key in ctx.source_keys:
        raise _err(
            f"{ctype} (id={cid}) list '{key}' is data-source-owned (read-only); "
            "a form/tracker writes rows, so point it at a user list"
        )
    return ctx.list_keys[key]


def _validate_form(cid, node, ctx: _Ctx) -> None:
    """A ``form`` adds rows into a mutable list; every field must be on that
    list's item schema, and `options` only make sense on a string field. It
    expands (composites.py) to typed inputs + a submit — the agent never wires
    the draft state or the item_add."""
    if not isinstance(cid, str) or not cid:
        raise _err("form needs a stable `id` (it namespaces the form's draft state)")
    if ctx.item_fields is not None:
        raise _err(f"form (id={cid}) can't be nested inside a repeater row")
    item = _mutable_list(cid, "form", node.get("into"), ctx)
    fields = node.get("fields")
    if not isinstance(fields, list) or not fields:
        raise _err(f"form (id={cid}) needs a non-empty `fields` array")
    if len(fields) > catalog.MAX_ITEM_FIELDS:
        raise _err(f"form (id={cid}) has too many fields (max {catalog.MAX_ITEM_FIELDS})")
    seen: set[str] = set()
    for f in fields:
        if not isinstance(f, dict) or not isinstance(f.get("field"), str):
            raise _err(f"form (id={cid}) each field needs a string `field` name")
        name = f["field"]
        if name not in item:
            raise _err(
                f"form (id={cid}) field '{name}' is not on list '{node['into']}' "
                f"(declared: {sorted(item)})"
            )
        if name in seen:
            raise _err(f"form (id={cid}) field '{name}' is listed twice")
        seen.add(name)
        if name == "id" or item[name] == "image":
            raise _err(f"form (id={cid}) can't edit the '{name}' field")
        opts = f.get("options")
        if opts is not None:
            if not isinstance(opts, list) or not opts:
                raise _err(f"form (id={cid}) field '{name}' `options` must be a non-empty array")
            if item[name] != "string":
                raise _err(
                    f"form (id={cid}) field '{name}' has `options` but is a {item[name]} "
                    "field; options are for string fields only"
                )


def _validate_tracker_card(cid, node, ctx: _Ctx) -> None:
    """A ``tracker_card`` summarizes one list: an aggregate metric (+ chart).
    Expands to a computed over the list + a list-backed chart."""
    if not isinstance(cid, str) or not cid:
        raise _err("tracker_card needs a stable `id` (it namespaces the aggregate)")
    if ctx.item_fields is not None:
        raise _err(f"tracker_card (id={cid}) can't be nested inside a repeater row")
    item = _mutable_list(cid, "tracker_card", node.get("list"), ctx)
    field = node.get("field")
    if field is not None and item.get(field) != "number":
        raise _err(
            f"tracker_card (id={cid}) `field` must name a number field of the list; "
            f"got {field!r}"
        )
    agg = node.get("agg")
    if agg is not None and agg not in catalog.AGGS:
        raise _err(f"tracker_card (id={cid}) `agg` must be one of {sorted(catalog.AGGS)}")
    if agg in ("sum", "avg", "min", "max") and field is None:
        raise _err(f"tracker_card (id={cid}) agg '{agg}' needs a number `field`")
    chart = node.get("chart")
    if chart is not None and chart not in ("bar", "line", "area", "pie", "donut"):
        raise _err(
            f"tracker_card (id={cid}) `chart` must be bar/line/area/pie/donut; got {chart!r}"
        )
    window = node.get("window")
    if window is not None and window not in catalog.WINDOWS:
        raise _err(f"tracker_card (id={cid}) `window` must be one of {sorted(catalog.WINDOWS)}")


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
    for name in _expr_key_refs(when):
        if name not in scalar_keys:
            raise _err(
                f"{where}: `when` references unknown key '{name}' — it must be a "
                "declared state or computed key"
            )
    after = w.get("after")
    if after is not None and (not isinstance(after, str) or not _HHMM_RE.match(after)):
        raise _err(f'{where}: `after` must be a 24-hour time like "18:00"')
