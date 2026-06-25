"""In-flight stream registry + chat.inflight feature RPC: lets a client that
left a chat mid-stream fetch the partial text on return."""

from __future__ import annotations

import pytest

from flowly.agent import inflight


@pytest.fixture(autouse=True)
def _clear_registry():
    inflight._runs.clear()
    yield
    inflight._runs.clear()


def test_begin_append_get():
    inflight.begin("web:c1", "run1")
    inflight.append("web:c1", "run1", "Hello")
    inflight.append("web:c1", "run1", " world")
    assert inflight.get("web:c1") == {"runId": "run1", "text": "Hello world", "user": "", "iterations": []}


def test_begin_records_user_message():
    # The triggering user message rides along so a re-entering client can
    # restore the whole turn (user bubble + partial assistant), not just text.
    inflight.begin("web:c1", "run1", "research X for me")
    inflight.append("web:c1", "run1", "On it…")
    assert inflight.get("web:c1") == {
        "runId": "run1", "text": "On it…", "user": "research X for me", "iterations": [],
    }


def test_get_unknown_is_none():
    assert inflight.get("web:nope") is None


def test_iterations_accumulate_and_return():
    inflight.begin("desktop:c1", "run1", "make it html")
    inflight.append("desktop:c1", "run1", "Working…")
    inflight.append_iteration("desktop:c1", "run1", {"role": "assistant", "iterationIdx": 0})
    inflight.append_iteration("desktop:c1", "run1", {"role": "tool", "iterationIdx": 0, "name": "artifact"})
    got = inflight.get("desktop:c1")
    assert got["user"] == "make it html"
    assert got["text"] == "Working…"
    assert [e["role"] for e in got["iterations"]] == ["assistant", "tool"]


def test_iterations_capped_to_max():
    inflight.begin("desktop:c1", "run1")
    for i in range(inflight._MAX_ITERATIONS + 50):
        inflight.append_iteration("desktop:c1", "run1", {"i": i})
    iters = inflight.get("desktop:c1")["iterations"]
    assert len(iters) == inflight._MAX_ITERATIONS
    # Oldest dropped, newest kept.
    assert iters[-1]["i"] == inflight._MAX_ITERATIONS + 49


def test_finish_clears():
    inflight.begin("web:c1", "run1")
    inflight.append("web:c1", "run1", "hi")
    inflight.finish("web:c1", "run1")
    assert inflight.get("web:c1") is None


def test_append_without_begin_still_tracks():
    inflight.append("web:c2", "run9", "late")
    assert inflight.get("web:c2") == {"runId": "run9", "text": "late", "user": "", "iterations": []}


def test_new_run_replaces_old():
    inflight.begin("web:c1", "run1")
    inflight.append("web:c1", "run1", "old")
    inflight.begin("web:c1", "run2")
    inflight.append("web:c1", "run2", "new")
    assert inflight.get("web:c1") == {"runId": "run2", "text": "new", "user": "", "iterations": []}


def test_stale_finish_does_not_wipe_newer_run():
    inflight.begin("web:c1", "run2")
    inflight.append("web:c1", "run2", "current")
    # A late finish from the previous run must not clear run2.
    inflight.finish("web:c1", "run1")
    assert inflight.get("web:c1") == {"runId": "run2", "text": "current", "user": "", "iterations": []}


def test_empty_session_key_noop():
    inflight.begin("", "run1")
    inflight.append("", "run1", "x")
    assert inflight._runs == {}


@pytest.mark.asyncio
async def test_chat_inflight_rpc():
    from flowly.channels import feature_rpc
    inflight.begin("web:c1", "run1")
    inflight.append("web:c1", "run1", "partial answer")

    result, restart = await feature_rpc.dispatch("chat.inflight", {"sessionKey": "web:c1"})
    assert restart is False
    assert result["inflight"] == {"runId": "run1", "text": "partial answer", "user": "", "iterations": []}

    empty, _ = await feature_rpc.dispatch("chat.inflight", {"sessionKey": "web:none"})
    assert empty["inflight"] is None


def test_chat_inflight_in_feature_methods():
    from flowly.channels import feature_rpc
    assert "chat.inflight" in feature_rpc.FEATURE_METHODS
