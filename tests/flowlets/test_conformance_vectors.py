"""The core interpreter satisfies the shared conformance vectors.

``flowly/flowlets/conformance/vectors.json`` is the contract every interpreter
(Python here, TS on Desktop, Swift on iOS, Kotlin on Android) runs in its own
test suite. Each app keeps a byte-identical copy, so a change here must land in
all four.
"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from flowly.flowlets.queries import (
    _UnresolvedNameError,
    eval_expr,
    is_visible,
    passes_where,
    render_template,
    validate_expr,
)

DOC = json.loads((Path(__file__).parents[2] / "flowly/flowlets/conformance/vectors.json").read_text())


def _clock(v: dict) -> tuple[int, ZoneInfo]:
    return v.get("nowMs", DOC["nowMs"]), ZoneInfo(v.get("tz", DOC["tz"]))


@pytest.mark.parametrize("v", DOC["expr"], ids=lambda v: v["expr"])
def test_expr(v):
    now, tz = _clock(v)
    ns = {**v.get("values", {}), "__now__": now, "__tz__": tz}
    if v["expect"] == "unresolved":
        with pytest.raises(_UnresolvedNameError):
            eval_expr(v["expr"], ns)
    else:
        validate_expr(v["expr"])
        assert eval_expr(v["expr"], ns) == pytest.approx(v["expect"])


@pytest.mark.parametrize("v", DOC["visibleWhen"], ids=lambda v: v["expr"] or "<empty>")
def test_visible_when_fails_open(v):
    now, tz = _clock(v)
    assert is_visible(v["expr"], v["values"], now, tz) is v["expect"]


@pytest.mark.parametrize("v", DOC["where"], ids=lambda v: f"{v['expr']}|{v['item']}")
def test_where_fails_closed(v):
    now, tz = _clock(v)
    assert passes_where(v["expr"], v["item"], now, tz) is v["expect"]


@pytest.mark.parametrize("v", DOC["template"], ids=lambda v: f"{v['text']}|{v.get('locale')}")
def test_template(v):
    assert render_template(v["text"], v["values"], v.get("locale")) == v["expect"]


@pytest.mark.parametrize("expr", DOC["invalid"])
def test_invalid_expressions_are_rejected(expr):
    with pytest.raises(ValueError):
        validate_expr(expr)
