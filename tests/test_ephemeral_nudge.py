"""Ephemeral corrective nudges must never leak into persisted history.

The agent loop injects ``role:user`` corrective instructions mid-turn
(retry the tool, stop drifting, this-is-an-action-request) to steer the
model. They must reach the model for the retry but must NOT be saved to
the session — otherwise they surface in the transcript as if the user
typed them. We tag them with an internal ``_ephemeral_nudge`` marker,
strip underscore-prefixed keys before the provider call (so strict APIs
don't 400), and drop tagged messages at persistence.
"""

from flowly.agent.loop import _EPHEMERAL_NUDGE, _drop_ephemeral_nudges
from flowly.providers.openrouter_provider import _strip_internal_keys


def test_drop_ephemeral_nudges_removes_tagged_keeps_order():
    msgs = [
        {"role": "user", "content": "real user message"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
        {"role": "user", "content": "This is an action request...", _EPHEMERAL_NUDGE: True},
        {"role": "assistant", "content": "done"},
    ]
    out = _drop_ephemeral_nudges(msgs)
    assert [m.get("content") for m in out] == [
        "real user message",
        "",
        "result",
        "done",
    ]
    # The genuine user message and tool protocol survive untouched.
    assert out[0]["role"] == "user"
    assert out[1]["tool_calls"] == [{"id": "1"}]


def test_drop_ephemeral_nudges_noop_without_tags():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert _drop_ephemeral_nudges(msgs) == msgs


def test_strip_internal_keys_removes_underscore_fields():
    msgs = [
        {"role": "user", "content": "hi", _EPHEMERAL_NUDGE: True, "_other": 1},
        {"role": "assistant", "content": "ok", "tool_calls": [{"id": "x"}]},
    ]
    out = _strip_internal_keys(msgs)
    # Underscore-prefixed keys are gone from the payload…
    assert out[0] == {"role": "user", "content": "hi"}
    assert out[1] == {"role": "assistant", "content": "ok", "tool_calls": [{"id": "x"}]}


def test_strip_internal_keys_is_copy_based():
    """Originals keep the marker so persistence can still filter on it."""
    original = {"role": "user", "content": "hi", _EPHEMERAL_NUDGE: True}
    msgs = [original]
    _strip_internal_keys(msgs)
    assert original.get(_EPHEMERAL_NUDGE) is True  # untouched
    assert msgs[0] is original


def test_strip_internal_keys_noop_without_internal_fields():
    """No internal keys → return the same objects (no needless copies)."""
    msgs = [{"role": "user", "content": "hi"}]
    out = _strip_internal_keys(msgs)
    assert out[0] is msgs[0]
