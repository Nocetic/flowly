"""The skill's hand-written reference cannot drift from the running catalog:
every component and action op is documented, nothing it names is invented,
and every complete example in it validates."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from flowly.flowlets import catalog
from flowly.flowlets.schema import validate_definition

MD = (Path(__file__).parents[2] / "flowly/skills/flowlets/references/catalog.md").read_text()


def _mentioned(name: str) -> bool:
    return re.search(r'[`"]%s[`"]' % re.escape(name), MD) is not None


def test_every_component_and_op_is_documented():
    assert [c for c in catalog.COMPONENTS if not _mentioned(c)] == []
    assert [o for o in catalog.ACTION_OPS if not _mentioned(o)] == []
    assert [t for t in catalog.WATCH_TRIGGERS if not _mentioned(t)] == []


def test_nothing_documented_is_invented():
    types = set(re.findall(r'"type":\s*"([a-z_]+)"', MD))
    assert types - set(catalog.COMPONENTS) - set(catalog.STATE_TYPES) - set(catalog.ITEM_FIELD_TYPES) == set()
    assert set(re.findall(r'"op":\s*"([a-z_]+)"', MD)) - set(catalog.ACTION_OPS) == set()
    assert set(re.findall(r'"trigger":\s*"([a-z_]+)"', MD)) - set(catalog.WATCH_TRIGGERS) == set()


def _examples() -> list[dict]:
    out = []
    for block in re.findall(r"```json\n(.*?)```", MD, re.S):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "layout" in d and "name" in d:
            out.append(d)
    return out


@pytest.mark.parametrize("example", _examples(), ids=lambda d: d["name"])
def test_complete_examples_validate(example):
    validate_definition(example)
