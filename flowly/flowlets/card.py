"""What a flowlet's card on the Flowlets page shows — computed here, once.

A card is not a picture of the screen; it is a picture of the person's data.
Every client draws the same four things from this payload:

* ``figure`` — the headline number (``value``, optional ``max``, ``label``),
  left unformatted so each client writes it in the screen's ``locale``.
* ``fill`` — 0..1 progress toward a goal, when the screen has one. A card
  with a fill is drawn as a vessel filled that far.
* ``portrait`` — the last 90 days of activity, one value per day normalised
  to 0..1 (``end`` names the last day). Drawn as a growth ring: a new
  flowlet is a faint circle, and months of use turn it into a pattern only
  this person has.
* ``moment`` — when one of the screen's own reminders is relevant right now
  (a scheduled one around its time, a condition that holds), the sentence
  its author wrote for it and a one-tap action. The page lifts the most
  relevant moment to its top.

Nothing here calls a model: it is a pure function of the definition, the
resolved values and the event log, so it costs nothing to keep live.
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo
from typing import Any

from flowly.flowlets import catalog
from flowly.flowlets.queries import (
    _date_start_ms,
    _iter_ordered,
    _local_dt,
    _rows_as_events,
    _shadow_series,
    aggregate_buckets,
    eval_expr,
    flowlet_preview,
    render_template,
)

#: Days the portrait covers.
PORTRAIT_DAYS = 90

#: How far around a scheduled reminder its moment holds: an hour before it
#: (getting ready) to three hours after (still worth doing).
_MOMENT_BEFORE_MIN = 60
_MOMENT_AFTER_MIN = 180

#: Ops a card may fire with one tap: quick, deterministic, undoable. Anything
#: that asks the model, deletes, or needs typed input opens the screen instead.
_TAP_OPS = frozenset({"log", "increment", "toggle", "set", "reset", "batch"})

_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def card_preview(definition: dict, values: dict, events: list[dict],
                 now_ms: int, tz: tzinfo | None = None) -> dict | None:
    """The full card payload: the legacy ``text``/``pct`` headline plus
    ``figure``, ``fill``, ``portrait``, ``moment`` and ``locale``."""
    from flowly.flowlets.composites import expand_composites
    from flowly.flowlets.normalize import assign_missing_ids

    defn = assign_missing_ids(expand_composites(definition or {}))
    locale = defn.get("locale") if isinstance(defn.get("locale"), str) else None
    out: dict[str, Any] = dict(flowlet_preview(defn, values, locale=locale) or {})

    figure = _figure(defn, values)
    if figure is not None:
        out["figure"] = figure
    if isinstance(out.get("pct"), (int, float)):
        out["fill"] = round(float(out["pct"]), 4)
    portrait = _portrait(defn, values, events, now_ms, tz)
    if portrait is not None:
        out["portrait"] = portrait
    moment = _moment(defn, values, now_ms, tz)
    if moment is not None:
        out["moment"] = moment
    if locale:
        out["locale"] = locale
    return out or None


def preview_for(flowlet: dict, values: dict, store: Any = None,
                now_ms: int | None = None, tz: tzinfo | None = None) -> dict | None:
    """``card_preview`` for a stored flowlet: reads its events and uses its
    remembered device zone unless a zone is given."""
    from flowly.flowlets.store import get_store
    from flowly.flowlets.store import now_ms as _now
    from flowly.flowlets.zones import zone_for

    store = store or get_store()
    try:
        events = store.get_events(flowlet["id"])
    except Exception:  # noqa: BLE001 — a card never breaks a list
        events = []
    return card_preview(
        flowlet.get("definition") or {}, values, events,
        now_ms if now_ms is not None else _now(),
        tz if tz is not None else zone_for(flowlet),
    )


# ── figure ────────────────────────────────────────────────────────────────────

def _scalar(v: Any, values: dict, default: float | None = None) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        r = values.get(v)
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            return float(r)
    return default


def _clean(v: float) -> int | float:
    return int(v) if float(v).is_integer() else round(v, 2)


_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z][a-zA-Z0-9_]*\}")
_UNIT_RE = re.compile(r"^[^\d\s/{}]{1,6}$")


def _unit(comp: dict) -> str | None:
    """The component's unit: its own ``unit``, else what a label like
    "{today_ml} ml" or "{last_night}h" leaves around its number."""
    if isinstance(comp.get("unit"), str) and comp["unit"].strip():
        return comp["unit"].strip()
    label = comp.get("label")
    if isinstance(label, str) and len(_PLACEHOLDER_RE.findall(label)) == 1:
        rest = _PLACEHOLDER_RE.sub("", label).strip()
        if _UNIT_RE.match(rest):
            return rest
    return None


def _figure(defn: dict, values: dict) -> dict | None:
    """The headline number, in reading order: the first goal (progress, ring,
    gauge), stat or metric, or a list's done/total."""
    locale = defn.get("locale") if isinstance(defn.get("locale"), str) else None
    for comp in _iter_ordered(defn.get("layout") or []):
        t = comp.get("type")
        label = render_template(comp.get("label"), values, locale) if comp.get("label") else ""
        # A label that only prints the number again ("{today}/7") says nothing new.
        if label and "{" in str(comp.get("label")):
            label = ""
        if t in ("progress", "ring", "gauge"):
            val = _scalar(comp.get("value"), values, 0.0)
            mx = _scalar(comp.get("max"), values, 100.0)
            fig: dict = {"value": _clean(val), "max": _clean(mx)}
            if label:
                fig["label"] = label
            if _unit(comp):
                fig["unit"] = _unit(comp)
            return fig
        if t in ("stat", "metric") and comp.get("value") is not None:
            val = _scalar(comp.get("value"), values)
            if val is None:
                continue
            fig = {"value": _clean(val)}
            if label:
                fig["label"] = label
            if _unit(comp):
                fig["unit"] = _unit(comp)
            return fig
        if t == "repeater":
            items = values.get(comp.get("source") or "")
            if not isinstance(items, list):
                continue
            spec = (defn.get("state") or {}).get(comp.get("source") or "") or {}
            fields = spec.get("item") or {}
            done_field = next((f for f, ft in fields.items() if ft == "bool"), None)
            if done_field is not None and items:
                done = sum(1 for it in items if isinstance(it, dict) and it.get(done_field))
                return {"value": done, "max": len(items)}
            return {"value": len(items)}
    return None


# ── portrait ──────────────────────────────────────────────────────────────────

def _item_schema(defn: dict, list_key: str) -> dict:
    spec = (defn.get("state") or {}).get(list_key)
    return (spec.get("item") or {}) if isinstance(spec, dict) else {}


def _date_field(schema: dict, preferred: Any = None) -> str | None:
    if isinstance(preferred, str) and preferred:
        return preferred
    return next((f for f, t in schema.items() if t == "date"), None)


def _activity(defn: dict, values: dict, events: list[dict],
              now_ms: int, tz: tzinfo | None) -> tuple[list[dict], str] | None:
    """The screen's main activity as ``(events, agg)``: the source of its first
    time chart, else its first logged series, else its first dated list."""
    by_series: dict[str, list[dict]] = {}
    for e in events or []:
        by_series.setdefault(e.get("series"), []).append(e)
    shadows = _shadow_series(defn)

    # Reading order: the chart a person sees first is the one that matters.
    for comp in _iter_ordered(defn.get("layout") or []):
        if comp.get("type") not in catalog.SERIES_COMPONENTS:
            continue
        data = comp.get("data") or {}
        if not isinstance(data, dict) or data.get("by") or "x" in data or "y" in data:
            continue
        agg = data.get("agg") if data.get("agg") in ("sum", "count", "max", "avg", "min") else "sum"
        series = data.get("series")
        if isinstance(series, str) and series in shadows:
            sh = shadows[series]
            data = {"list": sh["list"], "field": sh["field"]}
        if isinstance(data.get("list"), str):
            schema = _item_schema(defn, data["list"])
            date_field = _date_field(schema, data.get("date"))
            if date_field is None:
                continue
            rows = _rows_as_events(values.get(data["list"]), data.get("field"), date_field,
                                   None, now_ms, tz)
            return rows, agg
        if isinstance(series, str):
            return by_series.get(series, []), agg

    declared = defn.get("series") or {}
    for name in declared:
        if by_series.get(name):
            return by_series[name], "sum"
    for key, spec in (defn.get("state") or {}).items():
        if isinstance(spec, dict) and spec.get("type") == "list":
            date_field = _date_field(spec.get("item") or {})
            if date_field:
                return _rows_as_events(values.get(key), None, date_field, None, now_ms, tz), "count"
    if declared:
        return [], "sum"
    return None


def _portrait(defn: dict, values: dict, events: list[dict],
              now_ms: int, tz: tzinfo | None) -> dict | None:
    found = _activity(defn, values, events, now_ms, tz)
    if found is None:
        return None
    rows, agg = found
    # Dated rows sit at local midday; close the window at the end of today so
    # a row dated today counts this morning too.
    end_ms = _date_start_ms(_local_dt(now_ms, tz).date(), tz) + 86_400_000 - 1
    buckets = aggregate_buckets(rows, agg, "day", f"{PORTRAIT_DAYS}d", end_ms, tz)
    buckets = buckets[-PORTRAIT_DAYS:]
    peak = max((b["v"] for b in buckets), default=0.0)
    days = [round(max(0.0, b["v"]) / peak, 3) if peak > 0 else 0.0 for b in buckets]
    return {"days": len(days), "end": buckets[-1]["t"] if buckets else None, "v": days}


# ── moment ────────────────────────────────────────────────────────────────────

def _hhmm(s: Any) -> int | None:
    if not isinstance(s, str) or ":" not in s:
        return None
    hh, _, mm = s.partition(":")
    try:
        return int(hh) * 60 + int(mm)
    except ValueError:
        return None


def _holds(expr: Any, ns: dict) -> bool | None:
    """A `when` as True/False, or None when it can't be read (never shown)."""
    if not expr:
        return True
    try:
        return eval_expr(str(expr), ns) != 0
    except Exception:  # noqa: BLE001
        return None


def _say(notify: Any, values: dict, locale: str | None) -> tuple[str, str]:
    if not isinstance(notify, dict):
        return "", ""
    title = render_template(notify.get("title"), values, locale).strip()
    body = render_template(notify.get("body"), values, locale).strip()
    return title, body


def _quick_action(defn: dict) -> dict | None:
    """The screen's one-tap action: its first top-level photo capture or
    quick button (outside any list row)."""
    def walk(nodes: Any) -> dict | None:
        for node in nodes or []:
            if not isinstance(node, dict) or node.get("visibleWhen"):
                continue
            t = node.get("type")
            cid = node.get("id")
            if t == "photo" and cid:
                return {"id": cid, "kind": "photo",
                        **({"label": node["label"]} if isinstance(node.get("label"), str) else {})}
            if t == "button" and cid and isinstance(node.get("text"), str):
                op = (node.get("action") or {}).get("op")
                if op in _TAP_OPS and node.get("style") != "destructive":
                    return {"id": cid, "kind": "tap", "label": node["text"]}
            if t == "repeater":
                continue
            hit = walk(node.get("children"))
            if hit:
                return hit
        return None
    return walk(defn.get("layout"))


def _moment(defn: dict, values: dict, now_ms: int, tz: tzinfo | None) -> dict | None:
    """The most relevant reminder right now, as the card's moment."""
    locale = defn.get("locale") if isinstance(defn.get("locale"), str) else None
    now = datetime.fromtimestamp(now_ms / 1000, tz)
    now_min = now.hour * 60 + now.minute
    ns = {**values, "__now__": now_ms, "__tz__": tz}
    best: dict | None = None

    for w in defn.get("watches") or []:
        if not isinstance(w, dict):
            continue
        days = w.get("days")
        if days and _WEEKDAY[now.weekday()] not in {str(d).lower() for d in days}:
            continue
        trigger = w.get("trigger")
        notify = w.get("notify")
        score = 0.0
        at = None
        if trigger == "schedule":
            at_min = _hhmm(w.get("at"))
            if at_min is None:
                continue
            delta = now_min - at_min
            if delta < -_MOMENT_BEFORE_MIN or delta > _MOMENT_AFTER_MIN:
                continue
            holds = _holds(w.get("when"), ns)
            if holds is None:
                continue
            if not holds:
                # The goal is already met: the author's `otherwise` line, if
                # any, is the moment ("All done for today").
                notify = w.get("otherwise")
                if not isinstance(notify, dict):
                    continue
            score = 1.0 - abs(delta) / (_MOMENT_AFTER_MIN + _MOMENT_BEFORE_MIN)
            at = w.get("at")
        elif trigger in ("condition", "goal"):
            if not w.get("when") or _holds(w.get("when"), ns) is not True:
                continue
            after = _hhmm(w.get("after"))
            if after is not None and now_min < after:
                continue
            score = 0.6
        else:
            continue
        title, body = _say(notify, values, locale)
        text = body or title
        if not text:
            continue
        if best is None or score > best["score"]:
            best = {"score": round(score, 3), "text": text,
                    **({"title": title} if body and title else {}),
                    **({"at": at} if at else {})}

    if best is None:
        return None
    action = _quick_action(defn)
    if action is not None:
        best["action"] = action
    return best
