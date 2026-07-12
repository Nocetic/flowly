"""Deterministic, LLM-free lint for flowlet definitions.

Validation (schema.py) rejects a definition a client *can't render*. Lint is the
next layer: a definition that renders but reads *wrong* — a hand-rolled row that
`list_row` would lay out better, a chart bound to a series that drifts from its
list, a list nobody can add to. Each finding is written FOR THE AGENT, so it can
fix it on a follow-up ``update`` (the tool returns the report on create/update).

Every rule here is derived from a real bug class seen in testing. Findings are
advisory (``severity: "warn"``) for now — warn-heavy first, promote a rule to
``"error"`` (which will block save) only once it's proven zero-false-positive.
Rules must be conservative: a false "used" is fine, a false "unused" is not.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from flowly.flowlets import catalog

_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
#: Interactive component types — a row with one of these is NOT a plain
#: display row (so `list_row` doesn't fit it).
_CONTROL_TYPES = frozenset(
    t for t, s in catalog.COMPONENTS.items() if s.get("category") == "input"
)
_DISPLAY_TEXT_PROPS = ("text", "title", "subtitle", "label")


def _finding(rid: str, msg: str, severity: str = "warn") -> dict:
    return {"id": rid, "severity": severity, "message": msg}


def _walk(node: Any) -> Iterator[dict]:
    """Every component dict in the definition tree (layout + repeater items +
    children), depth-first."""
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
        return
    if not isinstance(node, dict):
        return
    yield node
    yield from _walk(node.get("children"))
    item = node.get("item")
    if isinstance(item, dict):
        yield from _walk(item)


def _all_components(defn: dict) -> Iterator[dict]:
    yield from _walk(defn.get("layout"))
    for screen in (defn.get("screens") or {}).values():
        if isinstance(screen, dict):
            yield from _walk(screen.get("layout"))


def _has_control(node: dict) -> bool:
    for c in _walk(node):
        if c is node:
            continue
        if c.get("type") in _CONTROL_TYPES:
            return True
    return False


def _text_node_count(node: dict) -> int:
    return sum(1 for c in _walk(node) if c.get("type") == "text")


def _actions(defn: dict) -> Iterator[dict]:
    """Every action object (including the ops inside a batch)."""
    for comp in _all_components(defn):
        a = comp.get("action")
        if isinstance(a, dict):
            if a.get("op") == "batch":
                for op in a.get("ops") or []:
                    if isinstance(op, dict):
                        yield op
            else:
                yield a


def lint_definition(defn: dict) -> list[dict]:
    """Return advisory findings ``[{id, severity, message}]`` for ``defn``.

    Non-raising and side-effect-free; safe to call on any dict (a malformed one
    just yields fewer findings). The definition is the RAW (pre-expansion) one —
    lint speaks to what the agent WROTE (e.g. "use list_row"), not the expansion.
    """
    if not isinstance(defn, dict):
        return []
    out: list[dict] = []
    state = defn.get("state") or {}
    list_keys = {k for k, s in state.items()
                 if isinstance(s, dict) and s.get("type") == "list"}
    source_keys = {k for k, s in state.items()
                   if isinstance(s, dict) and s.get("source")}
    scalar_keys = ({k for k in state if k not in list_keys}
                   | set(defn.get("computed") or {}))
    serialized = json.dumps(defn, ensure_ascii=False)

    # ── L01 — a hand-rolled display row where list_row fits ───────────────────
    for comp in _all_components(defn):
        if comp.get("type") != "repeater":
            continue
        item = comp.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("row", "card") and not _has_control(item):
            out.append(_finding(
                "L01",
                f"repeater over '{comp.get('source')}' hand-builds its row from "
                "primitives. Use the `list_row` composite instead (title / "
                "subtitle / value / badge / thumb) — the system lays it out "
                "correctly; a hand-rolled row is where rows come out lopsided.",
            ))
        # ── L07 — a row that will crop (rows are height-capped) ──────────────
        if _text_node_count(item) > 2:
            out.append(_finding(
                "L07",
                f"the row for '{comp.get('source')}' has more than two text "
                "lines; list rows are height-capped and extra lines crop. Keep "
                "it to a title + one secondary line (use `list_row`).",
            ))
        # ── L04 — a repeater with no empty-state copy ────────────────────────
        if comp.get("source") in list_keys and not comp.get("empty"):
            out.append(_finding(
                "L04",
                f"the repeater over '{comp.get('source')}' has no `empty` text; "
                "add one so an empty list reads as intentional, not broken.",
            ))

    # ── L06 — a user list nobody can add to ───────────────────────────────────
    add_targets = {a.get("key") or a.get("into") for a in _actions(defn)
                   if a.get("op") in ("item_add", "vision")}
    # a `form` composite adds into its `into`
    for comp in _all_components(defn):
        if comp.get("type") == "form" and isinstance(comp.get("into"), str):
            add_targets.add(comp["into"])
    repeated = {c.get("source") for c in _all_components(defn)
                if c.get("type") == "repeater"}
    for lk in list_keys - source_keys:
        if lk in repeated and lk not in add_targets:
            out.append(_finding(
                "L06",
                f"list '{lk}' is shown but has no way to add rows. Add a "
                "`form`, an `item_add` input, or a `photo`+vision capture.",
            ))

    # ── L02 — a chart bound to a series that shadows a list ───────────────────
    from flowly.flowlets.queries import _shadow_series
    shadows = _shadow_series(defn)
    for comp in _all_components(defn):
        if comp.get("type") in catalog.SERIES_COMPONENTS:
            series = (comp.get("data") or {}).get("series")
            if isinstance(series, str) and series in shadows:
                out.append(_finding(
                    "L02",
                    f"chart '{comp.get('id')}' is bound to series '{series}', "
                    f"which just mirrors list '{shadows[series]['list']}'. Bind "
                    "the chart to the LIST instead ({\"list\": \"...\", "
                    "\"field\": \"...\"}) or use a `tracker_card` — a series "
                    "drifts when a row is deleted or added by photo.",
                ))

    # ── L03 — an undeclared {token} in display copy ───────────────────────────
    def _in_repeater(defn_: dict) -> set[int]:
        ids: set[int] = set()
        def mark(n: Any) -> None:
            if isinstance(n, dict):
                item = n.get("item")
                if isinstance(item, dict):
                    for c in _walk(item):
                        ids.add(id(c))
                for c in n.get("children") or []:
                    mark(c)
        mark(defn_.get("layout"))
        for s in (defn_.get("screens") or {}).values():
            if isinstance(s, dict):
                mark(s.get("layout"))
        return ids

    scoped = _in_repeater(defn)
    for comp in _all_components(defn):
        if id(comp) in scoped:
            continue  # inside a row template, {$.field} is expected
        for prop in _DISPLAY_TEXT_PROPS:
            v = comp.get(prop)
            if not isinstance(v, str):
                continue
            for tok in _TOKEN_RE.findall(v):
                if tok not in scalar_keys:
                    out.append(_finding(
                        "L03",
                        f"{comp.get('type')} text references '{{{tok}}}', which "
                        "is not a declared state/computed key — a typo or a "
                        "leaked template token. It will render blank.",
                    ))

    # ── L10 — a declared state key never referenced ───────────────────────────
    for key in state:
        # count>1 means the name appears somewhere beyond its own declaration.
        # substring collisions only inflate the count (→ we skip flagging a
        # used key), never the reverse — so no false "unused".
        if serialized.count(key) <= 1:
            out.append(_finding(
                "L10",
                f"state key '{key}' is declared but never used. Remove it, or "
                "bind/show/act on it.",
            ))

    # ── L11 — a drill screen nothing navigates to ─────────────────────────────
    navigated = {c.get("navigate") for c in _all_components(defn)
                 if isinstance(c.get("navigate"), str)}
    for sid in (defn.get("screens") or {}):
        if sid not in navigated:
            out.append(_finding(
                "L11",
                f"screen '{sid}' is declared but no repeater/table navigates to "
                "it — it's unreachable. Add `navigate` on a list, or drop it.",
            ))

    return out
