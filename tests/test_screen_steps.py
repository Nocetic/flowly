"""The screen step store: one waiting step per conversation, durable, bounded."""

from __future__ import annotations

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.screen_steps import HISTORY_PER_CONVERSATION, HISTORY_TOTAL, ScreenSteps

IDENTITY = {"hostId": "host", "botId": "bot", "profileId": "default"}


@pytest.fixture
def steps(tmp_path):
    clock = [1000.0]
    store = ScreenSteps(tmp_path, clock=lambda: clock[0])
    return store, clock


def test_a_step_waits_until_it_is_done_and_the_record_outlives_the_process(steps, tmp_path):
    store, clock = steps
    step, superseded = store.open(IDENTITY, "desktop:chat", "desktop", "chat", "  Sign in to  X ")
    assert superseded is None and step["task"] == "Sign in to X" and step["state"] == "waiting"
    assert store.waiting("desktop:chat")["id"] == step["id"]
    assert store.waiting("desktop:other") is None
    again = ScreenSteps(tmp_path, clock=lambda: clock[0])  # the gateway restarted
    assert [row["id"] for row in again.all_waiting()] == [step["id"]]
    clock[0] = 1100.0
    assert again.mark_taken(step["id"], "generation-1")["taken_generation"] == "generation-1"
    clock[0] = 1200.0
    done = again.resolve(step["id"], "done")
    assert done["state"] == "done" and done["resolved_at"] == 1200.0 and done["taken_at"] == 1100.0
    assert again.waiting("desktop:chat") is None
    assert again.resolve(step["id"], "withdrawn") is None  # the first end wins
    assert store.get(step["id"])["state"] == "done"


def test_a_later_step_supersedes_the_one_waiting_in_the_same_conversation(steps):
    store, _ = steps
    first, _ = store.open(IDENTITY, "desktop:chat", "desktop", "chat", "Sign in")
    elsewhere, _ = store.open(IDENTITY, "desktop:other", "desktop", "other", "Approve the payment")
    second, superseded = store.open(IDENTITY, "desktop:chat", "desktop", "chat", "Enter the code")
    assert superseded["id"] == first["id"] and superseded["state"] == "superseded"
    assert store.get(first["id"])["state"] == "superseded"
    assert store.waiting("desktop:chat")["id"] == second["id"]
    assert store.waiting("desktop:other")["id"] == elsewhere["id"]
    assert [row["state"] for row in store.recent("desktop:chat")] == ["superseded", "waiting"]


def test_what_clients_see_and_what_they_do_not(steps):
    store, _ = steps
    step, _ = store.open(IDENTITY, "desktop:chat", "desktop", "chat-42", "Sign in")
    (public,) = store.recent("desktop:chat")
    assert public == {"id": step["id"], "sessionKey": "desktop:chat", "task": "Sign in", "profileId": "default",
                      "state": "waiting", "createdAt": 1000.0, "takenAt": None, "resolvedAt": None}
    assert "chat-42" not in str(public) and "host" not in str(public)


def test_the_mark_that_ends_a_step_is_durable_and_only_a_waiting_steps(steps):
    store, _ = steps
    step, _ = store.open(IDENTITY, "desktop:chat", "desktop", "chat", "Sign in")
    assert store.get(step["id"])["watch_target"] is None
    assert store.watch(step["id"], "generation-1", 7)["watch_target"] == 7
    assert (store.get(step["id"])["watch_generation"], store.get(step["id"])["watch_target"]) == ("generation-1", 7)
    store.resolve(step["id"], "done")
    assert store.watch(step["id"], "generation-2", 9) is None
    with pytest.raises(ScreenError):
        store.watch(step["id"], "generation-2", -1)


def test_history_is_bounded_and_waiting_steps_are_never_pruned(steps):
    store, _ = steps
    for i in range(HISTORY_PER_CONVERSATION + 5):
        step, _ = store.open(IDENTITY, "desktop:chat", "desktop", "chat", f"Step {i}")
        store.resolve(step["id"], "withdrawn")
    kept = store.recent("desktop:chat", limit=1000)
    assert len(kept) == HISTORY_PER_CONVERSATION and kept[0]["task"] == "Step 5"
    for i in range(HISTORY_TOTAL + 3):
        step, _ = store.open(IDENTITY, f"desktop:c{i}", "desktop", f"c{i}", "Sign in")
        store.resolve(step["id"], "done")
    waiting, _ = store.open(IDENTITY, "desktop:live", "desktop", "live", "Sign in")
    store.open(IDENTITY, "desktop:c0", "desktop", "c0", "Sign in")
    assert store.waiting("desktop:live")["id"] == waiting["id"]
    assert len(store.all_waiting()) == 2


@pytest.mark.parametrize("bad", [
    lambda s: s.open({"hostId": "h"}, "desktop:chat", "desktop", "chat", "Sign in"),
    lambda s: s.open(IDENTITY, "desktop:chat", "desktop", "chat", "   "),
    lambda s: s.open(IDENTITY, "", "desktop", "chat", "Sign in"),
    lambda s: s.resolve("x", "waiting"),
])
def test_bad_input_is_refused(steps, bad):
    store, _ = steps
    with pytest.raises(ScreenError):
        bad(store)
