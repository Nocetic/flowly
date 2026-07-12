"""Synthetic preview — render a flowlet against sample rows so the agent (and
the author loop) can SEE the result before a user does.

Most flowlet bugs only appear on a FULL screen: a lopsided row needs two texts,
an orphaned separator needs an empty field, a windowed chart needs dated rows, a
cropped row needs a long title. Empty lists hide all of it. So we fabricate a
few deterministic, EDGE-AWARE rows for every empty user list and resolve the
values map against them.

Deterministic (seeded by field name + row index, no randomness) so a given
definition always previews identically — the loop can diff it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

from flowly.flowlets.composites import expand_composites
from flowly.flowlets.queries import resolve_values

#: How many sample rows per empty list (enough to exercise the edges).
_N = 3
#: Day offsets for the sample rows — today, this week, last month — so a
#: windowed chart/metric shows both in- and out-of-window rows.
_DAY_OFFSETS = (0, -3, -40)
_LONG_TITLE = "Uzun bir örnek başlık — kırpma ve taşma testi için yeterince geniş"


def _sample_string(field: str, i: int) -> str:
    """A readable sample, edge-aware: row 0 gets a long unbroken value to test
    truncation; the LAST row leaves a non-primary string empty to test the
    missing-field path (collapsed rows / orphan separators)."""
    if i == 0 and field.lower() in ("title", "name", "description", "label"):
        return _LONG_TITLE
    if i == _N - 1 and field.lower() not in ("title", "name"):
        return ""  # exercise an empty optional
    return f"{field.capitalize()} {i + 1}"


def _sample_number(i: int) -> float:
    return (1234.5, 200.0, 0.0)[i % 3]  # a decimal, a round one, a zero


def synth_rows(item_schema: dict, now_ms: int, tz: tzinfo | None, n: int = _N) -> list[dict]:
    """Fabricate ``n`` deterministic sample rows for an item schema."""
    base = datetime.fromtimestamp(now_ms / 1000, tz)
    rows: list[dict] = []
    for i in range(n):
        row: dict[str, Any] = {"id": f"synth_{i}"}
        for field, ftype in item_schema.items():
            if field == "id":
                continue
            if ftype == "number":
                row[field] = _sample_number(i)
            elif ftype == "bool":
                row[field] = i % 2 == 0
            elif ftype == "date":
                off = _DAY_OFFSETS[i % len(_DAY_OFFSETS)]
                row[field] = (base + timedelta(days=off)).strftime("%Y-%m-%d")
            elif ftype == "image":
                continue  # no real attachment → leave absent (tests the no-photo path)
            else:  # string
                row[field] = _sample_string(field, i)
        rows.append(row)
    return rows


def preview_values(defn: dict, now_ms: int, tz: tzinfo | None = None) -> dict:
    """Resolve the ``values`` map with synthetic rows injected into every EMPTY
    user-owned list — what the flowlet would render with data.

    Never raises: a malformed definition falls back to a normal empty resolve.
    Returns the resolved values (state + computed + per-component series), the
    same shape a client gets.
    """
    try:
        expanded = expand_composites(defn)
        state = expanded.get("state") or {}
        state_map: dict[str, Any] = {}
        for key, spec in state.items():
            if (isinstance(spec, dict) and spec.get("type") == "list"
                    and not spec.get("source")):
                item = spec.get("item") or {}
                if isinstance(item, dict) and item:
                    state_map[key] = synth_rows(item, now_ms, tz)
        return resolve_values(defn, state_map, [], now_ms, tz)
    except Exception:  # noqa: BLE001 — a preview must never break the tool call
        try:
            return resolve_values(defn, {}, [], now_ms, tz)
        except Exception:  # noqa: BLE001
            return {}
