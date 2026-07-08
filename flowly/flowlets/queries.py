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
        if type(node.op) not in _UNARY_OPS:
            raise ValueError(f"unary operator {type(node.op).__name__} is not allowed")
        _check_node(node.operand)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError(
                f"only these functions are allowed: {sorted(_FUNCS)}"
            )
        if node.keywords:
            raise ValueError("keyword arguments are not allowed in expr")
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
            "(only + - * / % ** // , numbers, names, and min/max/abs/round/floor/ceil)"
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
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand, ns))
    if isinstance(node, ast.Call):
        args = [_eval_node(a, ns) for a in node.args]
        return _FUNCS[node.func.id](*args)
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
    values: dict[str, Any] = {}

    # 1. state (declared defaults, overridden by persisted values)
    state_defs = definition.get("state", {}) or {}
    for key, spec in state_defs.items():
        raw = state_map.get(key, spec.get("default"))
        values[key] = coerce_state(raw, spec) if isinstance(spec, dict) else raw

    # group events by series once
    by_series: dict[str, list[dict]] = {}
    for e in events:
        by_series.setdefault(e["series"], []).append(e)

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
            else:  # expr
                try:
                    values[key] = eval_expr(spec["expr"], values)
                    del pending[key]
                    progressed = True
                except _UnresolvedNameError:
                    continue
        if not progressed:
            break
    for key in pending:  # unresolved (cycle / bad ref) → safe zero
        values[key] = 0.0

    # 3. per-component series data (chart / sparkline / heatmap)
    for comp in _iter_components(definition.get("layout", [])):
        if comp.get("type") in catalog.SERIES_COMPONENTS and comp.get("id"):
            data = comp.get("data", {}) or {}
            series = data.get("series")
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
            values[comp["id"]] = buckets

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
