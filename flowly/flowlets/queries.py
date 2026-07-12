"""Value resolution for flowlets — the read side.

The bot is the only place aggregation happens. Clients receive a flat
``values`` map (state + computed scalars + per-component series arrays) and do
nothing but substitute and render. This module builds that map:

* :func:`resolve_values` — the entry point.
* :func:`aggregate_scalar` / :func:`aggregate_buckets` — the event math.
* :func:`eval_expr` / :func:`validate_expr` — a deliberately tiny, safe
  arithmetic evaluator for ``computed.expr`` (no attribute access, no calls
  except a whitelist, no names outside the resolved namespace).

Time buckets are computed in the caller-supplied timezone (defaulting to the
machine's local zone), so ``today`` rolls over at the user's midnight without
any separate reset mechanism.
"""

from __future__ import annotations

import ast
import operator
import re
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Iterable

from flowly.flowlets import catalog

# ── Safe expression evaluator ─────────────────────────────────────────────────

class _UnresolvedNameError(Exception):
    """Raised during eval when a referenced name isn't in the namespace yet."""


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Comparison + boolean ops make the grammar expressive enough for watch `when`
# conditions (``glasses < goal and hour >= 18``). They evaluate to 1.0 / 0.0 so
# the same evaluator still returns a float; existing arithmetic-only computed
# exprs are unaffected.
_CMP_OPS = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


def _safe_round(x, ndigits=0):
    return round(x, int(ndigits))


_FUNCS = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": _safe_round,
    "floor": lambda x: float(__import__("math").floor(x)),
    "ceil": lambda x: float(__import__("math").ceil(x)),
}

# Date/time functions. `now`/`weekday` take no args; `days_until`/`days_since`
# take one "YYYY-MM-DD" string (a literal or a name resolving to a date value).
# They read the reserved `__now__` (epoch ms) + `__tz__` from the namespace,
# injected at every eval site (resolve_values / watches / list `where`), and are
# mirrored 1:1 on the clients so an expr evaluates identically everywhere.
_DATE_FUNCS = frozenset({"now", "weekday", "days_until", "days_since"})
_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_EPOCH_ORD = date(1970, 1, 1).toordinal()


def _daynum_from_str(s: str) -> int:
    """A "YYYY-MM-DD" string → days since 1970-01-01 (a pure calendar-day count,
    identical on every platform — no DST/tz drift). Raises on a bad date."""
    m = _DATE_RE.match(s)
    if not m:
        raise _UnresolvedNameError("date")
    try:
        return date(int(m[1]), int(m[2]), int(m[3])).toordinal() - _EPOCH_ORD
    except ValueError:
        raise _UnresolvedNameError("date")


def _today_daynum(now_ms: int, tz: tzinfo | None) -> int:
    return _local_dt(now_ms, tz).date().toordinal() - _EPOCH_ORD


def _eval_date_fn(name: str, arg_nodes: list, ns: dict) -> float:
    now = ns.get("__now__")
    if not isinstance(now, (int, float)) or isinstance(now, bool):
        raise _UnresolvedNameError("__now__")
    now = int(now)
    tz = ns.get("__tz__")
    if name == "now":
        return float(now)
    if name == "weekday":
        return float(_local_dt(now, tz).weekday())  # 0=Mon .. 6=Sun
    # days_until / days_since — a single date argument (literal or name)
    a = arg_nodes[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        s = a.value
    elif isinstance(a, ast.Name):
        v = ns.get(a.id)
        if not isinstance(v, str):
            raise _UnresolvedNameError(a.id)
        s = v
    else:
        raise _UnresolvedNameError(name)
    delta = _daynum_from_str(s) - _today_daynum(now, tz)
    return float(delta if name == "days_until" else -delta)


def validate_expr(expr: str) -> None:
    """Confirm ``expr`` parses under the safe grammar. Raises ``ValueError`` with
    a specific reason otherwise. Does not evaluate — only shapes are checked."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"expr does not parse: {exc.msg}")
    _check_node(tree.body)


def _check_node(node: ast.AST) -> None:
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ValueError(f"operator {type(node.op).__name__} is not allowed")
        _check_node(node.left)
        _check_node(node.right)
    elif isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            _check_node(node.operand)
        elif type(node.op) in _UNARY_OPS:
            _check_node(node.operand)
        else:
            raise ValueError(f"unary operator {type(node.op).__name__} is not allowed")
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CMP_OPS:
                raise ValueError(f"comparison {type(op).__name__} is not allowed")
        _check_node(node.left)
        for comp in node.comparators:
            _check_node(comp)
    elif isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError("only `and` / `or` boolean operators are allowed")
        for v in node.values:
            _check_node(v)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only named functions are allowed")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed in expr")
        fn = node.func.id
        if fn in _DATE_FUNCS:
            if fn in ("now", "weekday"):
                if node.args:
                    raise ValueError(f"{fn}() takes no arguments")
            else:  # days_until / days_since — one "YYYY-MM-DD" string or a name
                if len(node.args) != 1:
                    raise ValueError(f"{fn}() takes one date argument")
                a = node.args[0]
                if isinstance(a, ast.Constant):
                    if not isinstance(a.value, str):
                        raise ValueError(f'{fn}() argument must be a "YYYY-MM-DD" string or a date key')
                elif not isinstance(a, ast.Name):
                    raise ValueError(f'{fn}() argument must be a "YYYY-MM-DD" string or a date key')
            return  # args validated above; don't recurse (a string arg is only legal here)
        if fn not in _FUNCS:
            raise ValueError(f"only these functions are allowed: {sorted(set(_FUNCS) | _DATE_FUNCS)}")
        for arg in node.args:
            _check_node(arg)
    elif isinstance(node, ast.Name):
        return  # resolved against the namespace at eval time
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise ValueError("only numeric constants are allowed in expr")
    else:
        raise ValueError(
            f"{type(node).__name__} is not allowed in expr "
            "(only + - * / % ** // , comparisons < <= > >= == !=, and/or/not, "
            "numbers, names, and min/max/abs/round/floor/ceil)"
        )


def eval_expr(expr: str, namespace: dict[str, Any]) -> float:
    """Evaluate a validated ``expr`` against ``namespace``. Raises
    :class:`_UnresolvedNameError` if a referenced name isn't present yet (so the
    caller can defer it in the dependency-order resolve)."""
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree.body, namespace)


def _eval_node(node: ast.AST, ns: dict[str, Any]) -> float:
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, ns)
        right = _eval_node(node.right, ns)
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError:
            return 0.0
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return 0.0 if _eval_node(node.operand, ns) != 0 else 1.0
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand, ns))
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ns)
        ok = True
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, ns)
            ok = ok and bool(_CMP_OPS[type(op)](left, right))
            left = right  # chained: 0 < x < 10
            if not ok:
                break
        return 1.0 if ok else 0.0
    if isinstance(node, ast.BoolOp):
        # Short-circuit like Python so an unresolved name in the untaken branch
        # doesn't raise (a watch `when` fails safe to false either way).
        if isinstance(node.op, ast.And):
            for v in node.values:
                if _eval_node(v, ns) == 0:
                    return 0.0
            return 1.0
        for v in node.values:
            if _eval_node(v, ns) != 0:
                return 1.0
        return 0.0
    if isinstance(node, ast.Call):
        fn = node.func.id
        if fn in _DATE_FUNCS:
            return _eval_date_fn(fn, node.args, ns)
        args = [_eval_node(a, ns) for a in node.args]
        return _FUNCS[fn](*args)
    if isinstance(node, ast.Name):
        if node.id not in ns:
            raise _UnresolvedNameError(node.id)
        val = ns[node.id]
        if isinstance(val, bool):
            return 1.0 if val else 0.0
        if isinstance(val, (int, float)):
            return val
        raise _UnresolvedNameError(node.id)  # non-numeric → treat as unresolved
    if isinstance(node, ast.Constant):
        return node.value
    raise ValueError(f"unexpected node {type(node).__name__}")


# ── String templating (computed `cases` text + watch notify share this) ───────

_TEMPLATE_RE = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.1f}"
    if isinstance(v, dict):  # a timer resolves to {running, elapsed}
        return _fmt_value(v.get("elapsed", ""))
    return str(v)


def render_template(text: Any, values: dict) -> str:
    """Substitute ``{key}`` placeholders in a string with formatted values.
    Unknown keys are left verbatim so a stray brace never explodes."""
    if not text:
        return ""

    def repl(m: "re.Match[str]") -> str:
        key = m.group(1)
        return _fmt_value(values[key]) if key in values else m.group(0)

    return _TEMPLATE_RE.sub(repl, str(text))


def _aggregate_list(spec: dict, values: dict, now_ms: int, tz: tzinfo | None) -> float:
    """Aggregate a dynamic ``list`` state into a scalar so stat / visibleWhen /
    watches can reason about it: ``{list, agg, field?, where?}``. ``where`` is an
    expr evaluated per item with the item's own fields (plus ``__now__`` for date
    fns) in scope. Makes lists first-class in the value system."""
    key = spec.get("list")
    items = values.get(key)
    if not isinstance(items, list):
        raise _UnresolvedNameError(str(key))
    where = spec.get("where")
    if where:
        selected = []
        for it in items:
            if not isinstance(it, dict):
                continue
            ns = {**it, "__now__": now_ms, "__tz__": tz}
            try:
                if eval_expr(str(where), ns) != 0:
                    selected.append(it)
            except _UnresolvedNameError:
                continue  # an item lacking a field the filter needs is excluded
        items = selected
    agg = spec.get("agg", "count")
    if agg == "count":
        return float(len(items))
    field = spec.get("field")
    nums = [
        float(it[field])
        for it in items
        if isinstance(it, dict)
        and isinstance(it.get(field), (int, float))
        and not isinstance(it.get(field), bool)
    ]
    if not nums:
        return 0.0
    return float(_apply_agg(nums, agg))


def _resolve_cases(spec: dict, values: dict) -> str:
    """A conditional-text computed: the first case whose ``when`` is truthy
    wins; its ``text`` (templated with ``{key}``) becomes the value. Falls back
    to ``else`` (default empty). Raises ``_UnresolvedNameError`` to defer in
    the fixpoint when a referenced name isn't resolved yet."""
    for case in spec.get("cases") or []:
        if eval_expr(str(case.get("when", "")), values) != 0:
            return render_template(case.get("text", ""), values)
    return render_template(spec.get("else", ""), values)


# ── Time helpers ──────────────────────────────────────────────────────────────

def _local_dt(ts_ms: int, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz)


def _date_start_ms(d: date, tz: tzinfo | None) -> int:
    dt = datetime(d.year, d.month, d.day, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _window_bounds(window: str, now_ms: int, tz: tzinfo | None) -> tuple[int | None, int]:
    """Return ``(start_ms_inclusive_or_None, end_ms_inclusive)`` for a window."""
    now_dt = _local_dt(now_ms, tz)
    today = now_dt.date()
    if window == "today":
        start_date = today
    elif window == "7d":
        start_date = today - timedelta(days=6)
    elif window == "30d":
        start_date = today - timedelta(days=29)
    elif window == "90d":
        start_date = today - timedelta(days=89)
    elif window == "all":
        return None, now_ms
    else:
        start_date = today - timedelta(days=6)
    return _date_start_ms(start_date, tz), now_ms


def _apply_agg(values: list[float], agg: str) -> float:
    if agg == "count":
        return float(len(values))
    if not values:
        return 0.0
    if agg == "sum":
        return float(sum(values))
    if agg == "avg":
        return float(sum(values) / len(values))
    if agg == "min":
        return float(min(values))
    if agg == "max":
        return float(max(values))
    if agg == "last":
        return float(values[-1])
    return float(sum(values))


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate_scalar(
    events: Iterable[dict],
    agg: str,
    window: str,
    now_ms: int,
    tz: tzinfo | None = None,
) -> float:
    """A single aggregated number over a time window.

    ``events`` are dicts ``{"value": float, "ts": int_ms}`` assumed sorted by
    ``ts`` ascending (so ``last`` is well-defined)."""
    start_ms, end_ms = _window_bounds(window, now_ms, tz)
    vals = [
        float(e["value"])
        for e in events
        if (start_ms is None or e["ts"] >= start_ms) and e["ts"] <= end_ms
    ]
    return _apply_agg(vals, agg)


def _bucket_key(ts_ms: int, bucket: str, tz: tzinfo | None) -> str:
    dt = _local_dt(ts_ms, tz)
    if bucket == "hour":
        return dt.strftime("%Y-%m-%dT%H")
    if bucket == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return dt.strftime("%Y-%m-%d")  # day


def _iter_bucket_keys(
    start_ms: int, end_ms: int, bucket: str, tz: tzinfo | None
) -> list[str]:
    """All bucket keys in [start, end], inclusive, so empty buckets render 0."""
    keys: list[str] = []
    seen: set[str] = set()
    if bucket == "hour":
        step = timedelta(hours=1)
        cur = _local_dt(start_ms, tz).replace(minute=0, second=0, microsecond=0)
    elif bucket == "week":
        step = timedelta(weeks=1)
        d = _local_dt(start_ms, tz)
        cur = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        step = timedelta(days=1)
        cur = _local_dt(start_ms, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = _local_dt(end_ms, tz)
    guard = 0
    while cur <= end_dt and guard < 2000:
        k = _bucket_key(int(cur.timestamp() * 1000), bucket, tz)
        if k not in seen:
            keys.append(k)
            seen.add(k)
        cur += step
        guard += 1
    return keys


def aggregate_buckets(
    events: Iterable[dict],
    agg: str,
    bucket: str,
    window: str,
    now_ms: int,
    tz: tzinfo | None = None,
) -> list[dict]:
    """Per-bucket series for a chart: ``[{"t": key, "v": number}, ...]`` with
    every bucket in the window present (missing buckets = 0)."""
    start_ms, end_ms = _window_bounds(window, now_ms, tz)
    if start_ms is None:
        # "all" — derive lower bound from the earliest event (or now).
        ev_list = list(events)
        events = ev_list
        start_ms = min((e["ts"] for e in ev_list), default=now_ms)
    grouped: dict[str, list[float]] = {
        k: [] for k in _iter_bucket_keys(start_ms, end_ms, bucket, tz)
    }
    for e in events:
        if e["ts"] < start_ms or e["ts"] > end_ms:
            continue
        k = _bucket_key(e["ts"], bucket, tz)
        grouped.setdefault(k, []).append(float(e["value"]))
    return [{"t": k, "v": _apply_agg(v, agg)} for k, v in grouped.items()]


def _category_breakdown(
    events: Iterable[dict],
    agg: str,
    window: str,
    now_ms: int,
    tz: tzinfo | None = None,
) -> list[dict]:
    """A categorical breakdown for a pie/donut/stacked chart:
    ``[{"k": category, "v": number}, …]`` sorted high→low, capped at
    ``MAX_PIE_SLICES`` with the tail folded into an "other" slice.

    An event's category is ``meta.category`` (missing → "other"). ``agg`` is
    ``sum`` (slice = total) or ``count`` (slice = tally). Percentages are a
    drawing concern the client computes from the totals.
    """
    start_ms, end_ms = _window_bounds(window, now_ms, tz)
    grouped: dict[str, list[float]] = {}
    for e in events:
        if start_ms is not None and (e["ts"] < start_ms or e["ts"] > end_ms):
            continue
        cat = ((e.get("meta") or {}).get("category") or "other")
        cat = str(cat).strip()[: catalog.MAX_CATEGORY_LEN] or "other"
        grouped.setdefault(cat, []).append(float(e["value"]))
    rows = [{"k": k, "v": _clean_number(_apply_agg(v, agg))} for k, v in grouped.items()]
    rows.sort(key=lambda r: (-r["v"], r["k"]))
    if len(rows) > catalog.MAX_PIE_SLICES:
        head = rows[: catalog.MAX_PIE_SLICES - 1]
        tail_total = sum(r["v"] for r in rows[catalog.MAX_PIE_SLICES - 1 :])
        # Merge into an existing "other" slice if one is already in the head.
        for r in head:
            if r["k"] == "other":
                r["v"] = _clean_number(r["v"] + tail_total)
                break
        else:
            head.append({"k": "other", "v": _clean_number(float(tail_total))})
        rows = head
    return rows


# ── List-backed charts ────────────────────────────────────────────────────────
#
# A chart ABOUT a list must aggregate the list's rows directly. The old pattern
# (a parallel `series` the agent logs alongside every item op) has two sources
# of truth that drift: a vision-added row never logs, and `item_remove`/
# `item_update` can't un-log — deleted expenses haunted the charts. Rows are
# mapped to the pseudo-event shape and fed through the SAME aggregators, so
# every chart stays a pure function of the list.


def _shadow_series(definition: dict) -> dict[str, dict]:
    """Series that are mere SHADOWS of a list → ``{series: {list, field, by}}``.

    The give-away is an authored ``batch`` that pairs ``item_add`` into a list
    with a ``log`` into a series — every manual add writes both, but a vision
    capture only writes the list and a delete can't un-log, so any chart bound
    to that series drifts from the list (ghost slices for deleted rows, missing
    receipts). Charts on a shadow series are resolved FROM THE LIST instead.

    Field mapping is by template identity: the ``log`` op's ``value`` template
    matching exactly one ``item_add`` field names the number field; likewise its
    ``category`` template names the group-by field (falling back to a field
    literally called ``category``).
    """
    out: dict[str, dict] = {}

    def scan(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                scan(n)
            return
        if not isinstance(node, dict):
            return
        action = node.get("action")
        if isinstance(action, dict) and action.get("op") == "batch":
            ops = [o for o in action.get("ops") or [] if isinstance(o, dict)]
            adds = [o for o in ops if o.get("op") == "item_add" and isinstance(o.get("key"), str)]
            logs = [o for o in ops if o.get("op") == "log" and isinstance(o.get("series"), str)]
            for add in adds:
                fields = add.get("fields") if isinstance(add.get("fields"), dict) else {}

                def field_matching(tpl: Any) -> str | None:
                    if not isinstance(tpl, str):
                        return None
                    hits = [f for f, t in fields.items() if t == tpl]
                    return hits[0] if len(hits) == 1 else None

                for log in logs:
                    by = field_matching(log.get("category"))
                    if by is None and "category" in fields:
                        by = "category"
                    out.setdefault(log["series"], {
                        "list": add["key"],
                        "field": field_matching(log.get("value")),
                        "by": by,
                    })
        scan(node.get("children"))
        item = node.get("item")
        if isinstance(item, dict):
            scan(item)

    scan(definition.get("layout"))
    for screen in (definition.get("screens") or {}).values():
        if isinstance(screen, dict):
            scan(screen.get("layout"))
    return out


def _row_ts(v: Any, tz: tzinfo | None) -> int | None:
    """A row's date field ("YYYY-MM-DD", datetime strings tolerated) → local
    MIDDAY ms (midday keeps the bucket stable across DST edges). None if the
    value doesn't parse."""
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        d = date.fromisoformat(v[:10])
    except ValueError:
        return None
    return _date_start_ms(d, tz) + 12 * 3600 * 1000


def _rows_as_events(
    rows: Any,
    field: str | None,
    date_field: str | None,
    cat_field: str | None,
    now_ms: int,
    tz: tzinfo | None,
) -> list[dict]:
    """List rows → the ``{"ts","value","meta"}`` shape the aggregators consume.

    * ``field`` names the numeric field to aggregate; absent → each row
      contributes 1.0 (a count). A row whose field isn't numeric is skipped.
    * ``date_field`` places the row in time; a row with an unparseable date is
      skipped (it can't be bucketed or window-filtered). With NO date field at
      all, rows stamp ``now`` so any window keeps them (category charts over
      undated lists).
    """
    out: list[dict] = []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        if field is not None:
            v = r.get(field)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            value = float(v)
        else:
            value = 1.0
        if date_field is not None:
            ts = _row_ts(r.get(date_field), tz)
            if ts is None:
                continue
        else:
            ts = now_ms
        e: dict = {"ts": ts, "value": value}
        if cat_field is not None:
            e["meta"] = {"category": r.get(cat_field)}
        out.append(e)
    out.sort(key=lambda e: e["ts"])
    return out


# ── State coercion ────────────────────────────────────────────────────────────

def coerce_state(value: Any, spec: dict) -> Any:
    """Coerce/clamp a stored or incoming state value to its declared type."""
    stype = spec.get("type")
    if stype == "number":
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = float(spec.get("default", 0) or 0)
        mn, mx = spec.get("min"), spec.get("max")
        if isinstance(mn, (int, float)):
            v = max(v, float(mn))
        if isinstance(mx, (int, float)):
            v = min(v, float(mx))
        # keep ints int-looking when clean
        return int(v) if v == int(v) else v
    if stype == "bool":
        return bool(value)
    if stype == "string":
        s = "" if value is None else str(value)
        ml = spec.get("maxLength", catalog.MAX_STRING_INPUT)
        return s[: int(ml)]
    if stype == "timer":
        return value if isinstance(value, dict) else {"running": False, "since_ms": 0, "accum_s": 0.0}
    if stype == "list":
        # An array of {id, ...fields} dicts; anything malformed drops out so a
        # client can iterate blindly. Capped defensively (writes cap too).
        if not isinstance(value, list):
            return []
        items = [it for it in value if isinstance(it, dict) and it.get("id")]
        return items[: catalog.MAX_LIST_ITEMS]
    return value


# ── Entry point ───────────────────────────────────────────────────────────────

def _iter_components(layout: list) -> Iterable[dict]:
    stack = list(layout)
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(children)
            # A repeater's row template lives under `item`, not `children` —
            # its components (toggle/delete buttons) must be findable too.
            if node.get("type") == "repeater" and isinstance(node.get("item"), dict):
                stack.append(node["item"])


def resolve_values(
    definition: dict,
    state_map: dict[str, Any],
    events: list[dict],
    now_ms: int,
    tz: tzinfo | None = None,
) -> dict[str, Any]:
    """Build the flat ``values`` map clients render against.

    * ``state_map`` — the persisted overrides ``{key: value}`` (unset keys fall
      back to their declared default).
    * ``events`` — all event rows for this flowlet, each ``{"series","value","ts"}``,
      sorted by ``ts`` ascending.
    """
    # Composites (catalog 3) inject their own state/computed/charts on
    # expansion — a tracker's aggregate metric, a form's draft keys. Resolve
    # against the EXPANDED definition so those exist. Then assign ids the
    # author forgot (an id-less chart would otherwise resolve NOTHING — its
    # series is keyed by id). Both idempotent; no-ops (no copy) when clean.
    from flowly.flowlets.composites import expand_composites
    from flowly.flowlets.normalize import assign_missing_ids
    definition = assign_missing_ids(expand_composites(definition))

    values: dict[str, Any] = {}

    # 1. state (declared defaults, overridden by persisted values)
    state_defs = definition.get("state", {}) or {}
    for key, spec in state_defs.items():
        if isinstance(spec, dict) and spec.get("type") == "timer":
            # Expose a timer as {running, elapsed} so a running one ticks live.
            t = state_map.get(key) or {}
            running = bool(t.get("running"))
            accum = float(t.get("accum_s", 0) or 0)
            since = int(t.get("since_ms", 0) or 0)
            elapsed = accum + (max(0, now_ms - since) / 1000.0 if running else 0.0)
            values[key] = {"running": running, "elapsed": round(elapsed, 1)}
        else:
            raw = state_map.get(key, spec.get("default") if isinstance(spec, dict) else None)
            values[key] = coerce_state(raw, spec) if isinstance(spec, dict) else raw

    # group events by series once
    by_series: dict[str, list[dict]] = {}
    for e in events:
        by_series.setdefault(e["series"], []).append(e)

    # `now`/`days_until` etc. read these from the eval namespace. Reserved
    # (double-underscore) so no user key can collide; stripped before we return
    # so they never reach the client or the preview.
    values["__now__"] = now_ms
    values["__tz__"] = tz

    # 2. computed (dependency-order resolve; order-independent via fixpoint)
    computed_defs = definition.get("computed", {}) or {}
    pending = dict(computed_defs)
    passes = len(pending) + 1
    for _ in range(passes):
        if not pending:
            break
        progressed = False
        for key in list(pending):
            spec = pending[key]
            if "series" in spec:
                values[key] = aggregate_scalar(
                    by_series.get(spec["series"], []),
                    spec.get("agg", "sum"),
                    spec.get("window", "all"),
                    now_ms,
                    tz,
                )
                del pending[key]
                progressed = True
            elif "list" in spec:  # aggregate a dynamic list (count/sum/... + where)
                try:
                    values[key] = _aggregate_list(spec, values, now_ms, tz)
                    del pending[key]
                    progressed = True
                except _UnresolvedNameError:
                    continue
            elif "cases" in spec:  # conditional text
                try:
                    values[key] = _resolve_cases(spec, values)
                    del pending[key]
                    progressed = True
                except _UnresolvedNameError:
                    continue
            else:  # expr
                try:
                    values[key] = eval_expr(spec["expr"], values)
                    del pending[key]
                    progressed = True
                except _UnresolvedNameError:
                    continue
        if not progressed:
            break
    for key, spec in pending.items():  # unresolved (cycle / bad ref) → safe fallback
        values[key] = "" if isinstance(spec, dict) and "cases" in spec else 0.0

    values.pop("__now__", None)
    values.pop("__tz__", None)

    # 3. per-component series data (chart / sparkline / heatmap).
    #    `data` forms (see catalog): single → [{t,v}]; multi → {multi:[…]};
    #    category → [{k,v}]; list-backed time/category → same shapes, derived
    #    from the list's rows; scatter → skipped (the client reads the rows).
    shadows = _shadow_series(definition)

    def _item_schema(list_key: str) -> dict:
        spec = state_defs.get(list_key)
        return (spec.get("item") or {}) if isinstance(spec, dict) else {}

    for comp in _iter_components(definition.get("layout", [])):
        if comp.get("type") not in catalog.SERIES_COMPONENTS or not comp.get("id"):
            continue
        cid = comp["id"]
        data = comp.get("data", {}) or {}

        # A chart bound to a SHADOW series (one only ever logged in lockstep
        # with a list's item_add) resolves from the LIST instead — the series
        # drifts (a vision add never logs; a delete can't un-log), the list is
        # the truth. The stored definition is untouched; only resolution
        # redirects, so every caller (get/action/broadcast/watch) heals alike.
        sref = data.get("series")
        if isinstance(sref, str) and sref in shadows:
            sh = shadows[sref]
            schema_ = _item_schema(sh["list"])
            nums = [f for f, t in schema_.items() if t == "number"]
            num_field = sh["field"] or (nums[0] if len(nums) == 1 else None)
            if data.get("by") == "category":
                if sh["by"] and (num_field or data.get("agg") == "count"):
                    data = {"list": sh["list"], "by": sh["by"],
                            **({"field": num_field} if num_field else {}),
                            **{k: data[k] for k in ("agg", "window") if k in data}}
            elif num_field and any(t == "date" for t in schema_.values()):
                data = {"list": sh["list"], "field": num_field,
                        **{k: data[k] for k in ("agg", "bucket", "window") if k in data}}
            # (no usable mapping → fall through to the event-based resolve)

        if "list" in data:
            if "x" in data or "y" in data:
                continue  # scatter: the list is already in `values`; nothing to resolve
            # List-backed: aggregate the rows THEMSELVES, so the chart stays a
            # pure function of the list (edits/deletes/vision adds included).
            rows = values.get(data["list"])
            item_schema = _item_schema(data["list"])
            date_field = data.get("date") or next(
                (f for f, t in item_schema.items() if t == "date"), None
            )
            # A row is stamped at local MIDDAY of its date, so a today-dated
            # row would sit "in the future" when values resolve in the morning
            # and the window (which closes at `now`) would drop it. For
            # date-granular list data the window closes at the END of the
            # current day instead — same calendar day, so the window start and
            # the bucket keys are identical; only the end bound extends.
            eod_ms = _date_start_ms(_local_dt(now_ms, tz).date(), tz) + 86_400_000 - 1
            by = data.get("by")
            if isinstance(by, str):
                events = _rows_as_events(
                    rows, data.get("field"), date_field, by, now_ms, tz,
                )
                values[cid] = _category_breakdown(
                    events, data.get("agg", "sum"), data.get("window", "30d"),
                    eod_ms, tz,
                )
            else:
                events = _rows_as_events(
                    rows, data.get("field"), date_field, None, now_ms, tz,
                )
                buckets = aggregate_buckets(
                    events, data.get("agg", "sum"), data.get("bucket", "day"),
                    data.get("window", "7d"), eod_ms, tz,
                )
                for b in buckets:
                    b["v"] = _clean_number(b["v"])
                values[cid] = buckets
            continue
        if data.get("by") == "category":
            values[cid] = _category_breakdown(
                by_series.get(data.get("series"), []),
                data.get("agg", "sum"),
                data.get("window", "30d"),
                now_ms,
                tz,
            )
            continue
        series = data.get("series")
        if isinstance(series, list):  # multi-series overlay
            multi = []
            for entry in series:
                k = entry.get("key") if isinstance(entry, dict) else None
                pts = aggregate_buckets(
                    by_series.get(k, []),
                    data.get("agg", "sum"),
                    data.get("bucket", "day"),
                    data.get("window", "7d"),
                    now_ms,
                    tz,
                )
                for b in pts:
                    b["v"] = _clean_number(b["v"])
                multi.append({"k": k, "points": pts})
            values[cid] = {"multi": multi}
            continue
        buckets = aggregate_buckets(
            by_series.get(series, []),
            data.get("agg", "sum"),
            data.get("bucket", "day"),
            data.get("window", "7d"),
            now_ms,
            tz,
        )
        for b in buckets:
            b["v"] = _clean_number(b["v"])
        values[cid] = buckets

    # Present whole numbers as ints so a label like "{today_ml} / {goal_ml}"
    # renders "750 / 2000" not "750.0 / 2000.0". Locale formatting (separators,
    # units) stays on the client; this is only representation cleanup.
    for k, v in values.items():
        if isinstance(v, float):
            values[k] = _clean_number(v)

    return values


def _clean_number(v: Any) -> Any:
    """Collapse an integer-valued float to an int for clean display."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _iter_ordered(layout: list) -> Iterable[dict]:
    """Preorder walk (reading order) — for picking the 'headline' component."""
    for node in layout:
        if isinstance(node, dict):
            yield node
            children = node.get("children")
            if isinstance(children, list):
                yield from _iter_ordered(children)


def _num(v: Any, values: dict, default: float = 0.0) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        r = values.get(v)
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            return float(r)
    return default


def _interp(text: Any, values: dict) -> str:
    if not isinstance(text, str):
        return ""
    import re as _re
    return _re.sub(
        r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}",
        lambda m: str(_clean_number(values.get(m.group(1), ""))),
        text,
    )


def flowlet_preview(definition: dict, values: dict) -> dict | None:
    """A compact headline for a list card: the first progress/ring/gauge (with a
    percent for a mini bar) or the first stat — as ready-to-show ``text`` plus an
    optional ``pct`` (0..1). Lets a tile read as content, not just an icon."""
    # Expand composites so a tracker_card's metric (hidden in the composite in
    # the stored definition) can headline the tile. Idempotent no-op otherwise.
    from flowly.flowlets.composites import expand_composites
    definition = expand_composites(definition)
    for comp in _iter_ordered(definition.get("layout", []) or []):
        t = comp.get("type")
        if t in ("progress", "ring", "gauge"):
            val = _num(comp.get("value"), values)
            mx = _num(comp.get("max"), values, 100.0)
            label = _interp(comp.get("label"), values)
            text = label or f"{_clean_number(val)} / {_clean_number(mx)}"
            pct = min(1.0, max(0.0, val / mx)) if mx else 0.0
            return {"text": text, "pct": pct}
        if t in ("stat", "metric") and comp.get("value") is not None:
            val = _num(comp.get("value"), values)
            label = comp.get("label")
            if isinstance(label, str) and "{" in label:
                text = _interp(label, values)
            elif isinstance(label, str) and label:
                text = f"{_clean_number(val)} · {label}"
            else:
                text = str(_clean_number(val))
            return {"text": text, "pct": None}
        if t == "repeater":
            # A list screen headlines as its progress: "done/total" when the
            # item schema has a bool field, plain count otherwise.
            items = values.get(comp.get("source") or "")
            if not isinstance(items, list):
                continue
            spec = (definition.get("state", {}) or {}).get(comp.get("source") or "")
            fields = (spec or {}).get("item") or {}
            bool_field = next((f for f, ft in fields.items() if ft == "bool"), None)
            total = len(items)
            if bool_field is not None and total:
                done = sum(1 for it in items if isinstance(it, dict) and it.get(bool_field))
                return {"text": f"{done}/{total}", "pct": done / total}
            return {"text": str(total), "pct": None}
    return None
