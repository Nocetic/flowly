"""Live data sources — schema validation (the engine is tested in test_sources_engine)."""

from __future__ import annotations

import pytest

from flowly.flowlets.schema import FlowletValidationError, validate_definition


def _repo(**over):
    d = {
        "catalog": 1, "name": "Repo",
        "state": {
            "repo": {"type": "string", "default": "Nocetic/flowly"},
            "commits": {"type": "list",
                        "item": {"title": "string", "who": "string", "at": "date"},
                        "source": True},
        },
        "sources": {
            "commits": {"kind": "agent",
                        "prompt": "the last 10 commits to {repo} in the past hour",
                        "into": "commits", "refresh": "15m", "limit": 10},
        },
        "layout": [
            {"id": "repo_in", "type": "input", "action": {"op": "set", "key": "repo"}},
            {"type": "repeater", "source": "commits", "empty": "No commits",
             "item": {"type": "row", "children": [
                 {"type": "text", "text": "{$.title}"},
                 {"type": "text", "text": "{$.who}"},
             ]}},
        ],
    }
    d.update(over)
    return d


def test_valid_agent_source():
    validate_definition(_repo())


def test_scalar_source_ok():
    """A source can fill a scalar too (e.g. a weather temp)."""
    d = {
        "catalog": 1, "name": "W",
        "state": {"temp": {"type": "number", "source": True}},
        "sources": {"temp": {"kind": "agent", "prompt": "current temp in C", "into": "temp"}},
        "layout": [{"type": "metric", "value": "temp", "label": "°C"}],
    }
    validate_definition(d)


@pytest.mark.parametrize("mut, match", [
    (lambda d: d["sources"]["commits"].update({"into": "ghost"}), "into"),
    (lambda d: d["sources"]["commits"].pop("prompt"), "prompt"),
    (lambda d: d["sources"]["commits"].update({"kind": "tool"}), "kind"),
    (lambda d: d["sources"]["commits"].update({"refresh": "1m"}), "≥"),
    (lambda d: d["sources"]["commits"].update({"refresh": "soon"}), "refresh"),
    (lambda d: d["sources"]["commits"].update({"limit": 0}), "limit"),
    (lambda d: d["state"].update({"dead": {"type": "string", "source": True}}), "no source writes"),
    (lambda d: d["state"]["commits"].update({"source": "yes"}), "source` must be"),
])
def test_source_schema_rejects(mut, match):
    d = _repo()
    mut(d)
    with pytest.raises(FlowletValidationError, match=match):
        validate_definition(d)


def test_two_sources_one_target_rejected():
    d = _repo()
    d["state"]["c2"] = {"type": "list", "item": {"title": "string"}, "source": True}
    d["sources"]["dup"] = {"kind": "agent", "prompt": "x", "into": "commits"}
    with pytest.raises(FlowletValidationError, match="both write"):
        validate_definition(d)


def test_user_action_on_source_key_rejected():
    d = _repo()
    d["layout"].append({"id": "add", "type": "button", "text": "x",
                        "action": {"op": "item_add", "key": "commits"}})
    with pytest.raises(FlowletValidationError, match="read-only"):
        validate_definition(d)


def test_manual_refresh_ok():
    d = _repo()
    d["sources"]["commits"]["refresh"] = "manual"
    validate_definition(d)


def test_too_many_sources():
    d = _repo()
    from flowly.flowlets import catalog
    for i in range(catalog.MAX_SOURCES + 1):
        d["state"][f"s{i}"] = {"type": "string", "source": True}
        d["sources"][f"s{i}"] = {"kind": "agent", "prompt": "x", "into": f"s{i}"}
    with pytest.raises(FlowletValidationError, match="too many sources"):
        validate_definition(d)
