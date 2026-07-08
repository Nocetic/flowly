"""Watches — schema validation, the pure `_decide` decision, and the engine.

The decision logic is a pure function, so most of the behaviour (edge-trigger,
cooldown, once, daily de-dupe, staleness, day filter) is tested directly with
fabricated timestamps and no store/async. A handful of end-to-end engine tests
then confirm the store persistence, notify firing, edge consumption across two
calls, and the throttled agent escape hatch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flowly.flowlets import catalog
from flowly.flowlets.schema import FlowletValidationError, validate_definition
from flowly.flowlets.watches import (
    WatchEngine,
    _decide,
    _parse_hhmm,
    render,
)

UTC = timezone.utc
MIN = 60_000
HOUR = 60 * MIN


def at(hour: int, minute: int = 0, *, day: int = 8) -> int:
    """Epoch-ms for 2026-07-DD HH:MM UTC (used with tz=UTC in _decide)."""
    return int(datetime(2026, 7, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


# ── schema ────────────────────────────────────────────────────────────────────

BASE = {
    "catalog": 1,
    "name": "Water",
    "state": {
        "glasses": {"type": "number", "default": 0},
        "goal": {"type": "number", "default": 8},
        "done": {"type": "bool", "default": False},
    },
    "layout": [{"type": "text", "text": "hi"}],
}


def _with_watches(watches):
    d = dict(BASE)
    d["watches"] = watches
    return d


@pytest.mark.parametrize(
    "watch",
    [
        {"id": "n", "trigger": "condition", "when": "glasses < goal",
         "after": "18:00", "notify": {"title": "Drink", "body": "{glasses}/{goal}"}},
        {"id": "d", "trigger": "schedule", "at": "20:00", "days": ["mon", "wed"],
         "notify": {"title": "Summary"}},
        {"id": "g", "trigger": "goal", "when": "glasses >= goal",
         "notify": {"title": "Done"}, "once": True},
        {"id": "s", "trigger": "stale", "idleMinutes": 180, "notify": {"title": "?"}},
        {"id": "b", "trigger": "condition", "when": "done or glasses >= goal",
         "notify": {"title": "x"}},
        {"id": "w", "trigger": "schedule", "everyMinutes": 60, "notify": {"title": "x"},
         "also": {"op": "agent", "message": "summarize the day"}},
    ],
)
def test_schema_accepts_valid_watches(watch):
    validate_definition(_with_watches([watch]))


@pytest.mark.parametrize(
    "watch",
    [
        {"trigger": "condition", "when": "glasses < goal", "notify": {"title": "x"}},  # no id
        {"id": "a", "trigger": "bogus", "notify": {"title": "x"}},                      # bad trigger
        {"id": "a", "trigger": "condition", "when": "typo < goal", "notify": {"title": "x"}},
        {"id": "a", "trigger": "condition", "when": "glasses < goal"},                  # no notify
        {"id": "a", "trigger": "schedule", "at": "25:00", "notify": {"title": "x"}},
        {"id": "a", "trigger": "schedule", "notify": {"title": "x"}},                    # no at/every
        {"id": "a", "trigger": "stale", "idleMinutes": 0, "notify": {"title": "x"}},
        {"id": "a", "trigger": "schedule", "at": "20:00", "notify": {"title": "x"},
         "also": {"op": "set"}},                                                         # also.op != agent
        {"id": "a", "trigger": "condition", "when": "glasses.attr", "notify": {"title": "x"}},
    ],
)
def test_schema_rejects_invalid_watches(watch):
    with pytest.raises(FlowletValidationError):
        validate_definition(_with_watches([watch]))


def test_schema_rejects_duplicate_ids():
    with pytest.raises(FlowletValidationError):
        validate_definition(_with_watches([
            {"id": "dup", "trigger": "schedule", "at": "20:00", "notify": {"title": "a"}},
            {"id": "dup", "trigger": "schedule", "at": "21:00", "notify": {"title": "b"}},
        ]))


def test_schema_rejects_too_many():
    many = [
        {"id": f"w{i}", "trigger": "schedule", "at": "20:00", "notify": {"title": "x"}}
        for i in range(catalog.MAX_WATCHES + 1)
    ]
    with pytest.raises(FlowletValidationError):
        validate_definition(_with_watches(many))


# ── _parse_hhmm + render ──────────────────────────────────────────────────────

def test_parse_hhmm():
    assert _parse_hhmm("20:00") == 1200
    assert _parse_hhmm("00:00") == 0
    assert _parse_hhmm("18:30") == 18 * 60 + 30
    assert _parse_hhmm("nope") is None
    assert _parse_hhmm(None) is None


def test_render_templating():
    vals = {"glasses": 3.0, "goal": 8, "ratio": 0.5, "done": True}
    assert render("{glasses}/{goal}", vals) == "3/8"
    assert render("{ratio}", vals) == "0.5"
    assert render("{done}", vals) == "yes"
    assert render("{missing} left", vals) == "{missing} left"  # unknown left verbatim
    assert render("", vals) == ""
    assert render(None, vals) == ""


# ── _decide: schedule ─────────────────────────────────────────────────────────

def test_schedule_at_fires_once_per_day():
    w = {"id": "d", "trigger": "schedule", "at": "20:00", "notify": {"title": "x"}}
    # before the time → no
    assert _decide(w, {}, {}, at(19, 59), UTC, None) == (False, None)
    # at/after, never fired → fire
    assert _decide(w, {}, {}, at(20, 0), UTC, None) == (True, None)
    # already fired earlier today → no
    ws = {"last_fired_ms": at(20, 0)}
    assert _decide(w, {}, ws, at(22, 0), UTC, None) == (False, None)
    # fired yesterday → fire again today (catch-up)
    ws = {"last_fired_ms": at(20, 0, day=7)}
    assert _decide(w, {}, ws, at(20, 1), UTC, None) == (True, None)


def test_schedule_every_minutes():
    w = {"id": "e", "trigger": "schedule", "everyMinutes": 60, "notify": {"title": "x"}}
    assert _decide(w, {}, {}, at(12, 0), UTC, None) == (True, None)  # never fired
    ws = {"last_fired_ms": at(12, 0)}
    assert _decide(w, {}, ws, at(12, 30), UTC, None) == (False, None)  # 30m < 60m
    assert _decide(w, {}, ws, at(13, 1), UTC, None) == (True, None)    # 61m ≥ 60m


def test_schedule_days_filter():
    # 2026-07-08 is a Wednesday.
    wed = at(20, 0, day=8)
    from flowly.flowlets.watches import _WEEKDAY
    assert _WEEKDAY[datetime.fromtimestamp(wed / 1000, UTC).weekday()] == "wed"
    on = {"id": "d", "trigger": "schedule", "at": "20:00", "days": ["wed"], "notify": {"title": "x"}}
    off = {"id": "d", "trigger": "schedule", "at": "20:00", "days": ["mon"], "notify": {"title": "x"}}
    assert _decide(on, {}, {}, wed, UTC, None) == (True, None)
    assert _decide(off, {}, {}, wed, UTC, None) == (False, None)


# ── _decide: condition / goal (edge-triggered) ────────────────────────────────

def test_condition_rising_edge_only():
    w = {"id": "c", "trigger": "condition", "when": "glasses < goal", "notify": {"title": "x"}}
    vals_true = {"glasses": 3, "goal": 8}
    vals_false = {"glasses": 8, "goal": 8}
    # false→true: fire, record cond True
    assert _decide(w, vals_true, {"last_cond": False}, at(12), UTC, None) == (True, True)
    # stays true: no re-fire, cond stays True
    assert _decide(w, vals_true, {"last_cond": True, "last_fired_ms": at(12)}, at(13), UTC, None) == (False, True)
    # drops to false: no fire, cond resets to False
    assert _decide(w, vals_false, {"last_cond": True, "last_fired_ms": at(12)}, at(14), UTC, None) == (False, False)
    # rises again: fires again (edge)
    assert _decide(w, vals_true, {"last_cond": False, "last_fired_ms": at(12)}, at(20), UTC, None) == (True, True)


def test_condition_cooldown_blocks_but_records_edge():
    w = {"id": "c", "trigger": "condition", "when": "glasses < goal",
         "cooldownMinutes": 360, "notify": {"title": "x"}}
    vals = {"glasses": 3, "goal": 8}
    # rising, but last fired 10 min ago (< 6h cooldown) → no fire, but edge recorded
    ws = {"last_cond": False, "last_fired_ms": at(12, 0)}
    assert _decide(w, vals, ws, at(12, 10), UTC, None) == (False, True)


def test_condition_after_guard():
    w = {"id": "c", "trigger": "condition", "when": "glasses < goal",
         "after": "18:00", "notify": {"title": "x"}}
    vals = {"glasses": 3, "goal": 8}
    # true condition but before 18:00 → treated as not-yet-true
    assert _decide(w, vals, {"last_cond": False}, at(17, 0), UTC, None) == (False, False)
    # after 18:00 → fires
    assert _decide(w, vals, {"last_cond": False}, at(18, 30), UTC, None) == (True, True)


def test_goal_once():
    w = {"id": "g", "trigger": "goal", "when": "glasses >= goal",
         "once": True, "notify": {"title": "x"}}
    vals = {"glasses": 8, "goal": 8}
    assert _decide(w, vals, {"last_cond": False}, at(12), UTC, None) == (True, True)
    # already fired once → never again (edge state preserved unchanged)
    assert _decide(w, vals, {"last_cond": True, "last_fired_ms": at(12)}, at(20), UTC, None) == (False, True)


def test_condition_days_off_day_preserves_edge():
    # On an off day the edge must NOT be consumed, so it still fires next allowed day.
    w = {"id": "c", "trigger": "condition", "when": "glasses < goal",
         "days": ["mon"], "notify": {"title": "x"}}
    vals = {"glasses": 3, "goal": 8}
    wed = at(12, 0, day=8)  # Wednesday, an off day for a mon-only watch
    fire, new_cond = _decide(w, vals, {"last_cond": False}, wed, UTC, None)
    assert fire is False
    assert new_cond is False  # edge preserved (unchanged), not consumed


# ── _decide: stale ────────────────────────────────────────────────────────────

def test_stale_fires_after_idle():
    w = {"id": "s", "trigger": "stale", "idleMinutes": 180, "notify": {"title": "x"}}
    now = at(20, 0)
    # activity 200 min ago → stale → fire
    assert _decide(w, {}, {}, now, UTC, now - 200 * MIN) == (True, None)
    # activity 100 min ago → not stale → no
    assert _decide(w, {}, {}, now, UTC, now - 100 * MIN) == (False, None)
    # no activity ever → no fire
    assert _decide(w, {}, {}, now, UTC, None) == (False, None)


def test_stale_no_refire_without_new_activity():
    w = {"id": "s", "trigger": "stale", "idleMinutes": 180,
         "cooldownMinutes": 60, "notify": {"title": "x"}}
    now = at(20, 0)
    activity = now - 200 * MIN
    fired_at = now - 10 * MIN
    # fired 10 min ago, still no new activity → no re-fire (cooldown, and no
    # activity newer than the last fire)
    assert _decide(w, {}, {"last_fired_ms": fired_at}, now, UTC, activity) == (False, None)
    # a fresh log AFTER the last fire, then 200 min of new idleness → fire again
    new_activity = now + 5 * MIN          # activity strictly after fired_at (=now-10m)
    later = new_activity + 200 * MIN      # 200 min idle since that fresh activity
    assert _decide(w, {}, {"last_fired_ms": fired_at}, later, UTC, new_activity) == (True, None)


# ── engine (end-to-end) ───────────────────────────────────────────────────────


class _Capture:
    def __init__(self):
        self.notifications: list[tuple[str, str, str]] = []
        self.agent_calls: list[tuple[str, str]] = []

    async def notify(self, flowlet_id, title, body):
        self.notifications.append((flowlet_id, title, body))

    async def agent(self, flowlet, message):
        self.agent_calls.append((flowlet["id"], message))


def _goal_def():
    return {
        "catalog": 1, "name": "Water",
        "state": {"glasses": {"type": "number", "default": 0},
                  "goal": {"type": "number", "default": 8}},
        "layout": [{"type": "text", "text": "hi"}],
        "watches": [{"id": "hit", "trigger": "goal", "when": "glasses >= goal",
                     "notify": {"title": "Goal!", "body": "{glasses}/{goal} 🎉"}}],
    }


async def test_engine_fires_goal_once_then_edge_consumed(store):
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, tz=UTC)
    f = store.create("Water", _goal_def())
    fid = f["id"]

    # below goal → nothing
    store.set_state(fid, "glasses", 3)
    assert await eng.evaluate_one(fid) == []
    assert cap.notifications == []

    # cross the goal → fires once, body templated
    store.set_state(fid, "glasses", 8)
    fired = await eng.evaluate_one(fid)
    assert fired == [fid]
    assert cap.notifications == [(fid, "Goal!", "8/8 🎉")]

    # still at goal → edge consumed, no re-fire
    assert await eng.evaluate_one(fid) == []
    assert len(cap.notifications) == 1

    # persisted edge state survives
    ws = store.get_watch_state(fid)["hit"]
    assert ws["last_cond"] is True
    assert ws["last_fired_ms"] is not None


async def test_engine_reset_and_refire(store):
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, tz=UTC)
    d = _goal_def()
    d["watches"][0]["cooldownMinutes"] = 0  # exercise pure edge behaviour, no cooldown gate
    f = store.create("Water", d)
    fid = f["id"]

    store.set_state(fid, "glasses", 8)
    await eng.evaluate_one(fid)
    assert len(cap.notifications) == 1

    # drop below and cross again → fires a second time
    store.set_state(fid, "glasses", 2)
    await eng.evaluate_one(fid)  # consume the falling edge
    store.set_state(fid, "glasses", 8)
    await eng.evaluate_one(fid)
    assert len(cap.notifications) == 2


async def test_engine_evaluate_all_skips_watchless(store):
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, tz=UTC)
    # one with a watch (already at goal via default? no — set it), one without
    f1 = store.create("Water", _goal_def())
    store.set_state(f1["id"], "glasses", 8)
    store.create("Plain", {"catalog": 1, "name": "Plain",
                           "state": {}, "layout": [{"type": "text", "text": "x"}]})
    fired = await eng.evaluate_all()
    assert fired == [f1["id"]]


async def test_engine_agent_escape_throttled(store):
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, agent_runner=cap.agent, tz=UTC)
    d = _goal_def()
    d["watches"][0]["also"] = {"op": "agent", "message": "log the win"}
    f = store.create("Water", d)
    fid = f["id"]
    store.set_state(fid, "glasses", 8)
    await eng.evaluate_one(fid)
    assert cap.agent_calls == [(fid, "log the win")]
    # firing again within the agent min-cooldown would not re-wake the agent,
    # but the edge is consumed anyway so no second fire happens here.
    assert len(cap.notifications) == 1


async def test_delete_stops_watch_and_clears_state(store):
    """Deleting a flowlet must cascade its watch state and stop the heartbeat
    from ever firing it again (no lingering reminders)."""
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, tz=UTC)
    d = {"catalog": 1, "name": "X", "state": {},
         "layout": [{"type": "text", "text": "x"}],
         "watches": [{"id": "ping", "trigger": "schedule", "everyMinutes": 1,
                      "notify": {"title": "hi"}}]}
    f = store.create("X", d)
    fid = f["id"]

    # fires once, writes watch state
    assert await eng.evaluate_all() == [fid]
    assert "ping" in store.get_watch_state(fid)

    # delete → watch state cascaded away, heartbeat no longer fires it
    assert store.delete(fid) is True
    assert store.get_watch_state(fid) == {}
    cap.notifications.clear()
    assert await eng.evaluate_all() == []
    assert cap.notifications == []


async def test_engine_schedule_daily(store):
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, tz=UTC)
    d = {"catalog": 1, "name": "Daily", "state": {},
         "layout": [{"type": "text", "text": "x"}],
         "watches": [{"id": "morning", "trigger": "schedule", "at": "09:00",
                      "notify": {"title": "Good morning"}}]}
    f = store.create("Daily", d)
    fid = f["id"]
    # before 09:00 → nothing
    assert await eng.evaluate_one(fid, now_ms=at(8, 0)) == []
    # at/after 09:00 → fire
    assert await eng.evaluate_one(fid, now_ms=at(9, 30)) == [fid]
    # same day again → no repeat
    assert await eng.evaluate_one(fid, now_ms=at(11, 0)) == []
    assert len(cap.notifications) == 1


# ── notify.compose (agent-written notifications) ─────────────────────────────


def _compose_def(**notify_extra):
    return {
        "catalog": 1, "name": "Water",
        "state": {"glasses": {"type": "number", "default": 0},
                  "goal": {"type": "number", "default": 8}},
        "layout": [{"type": "text", "text": "hi"}],
        "watches": [{"id": "hit", "trigger": "goal", "when": "glasses >= goal",
                     "notify": {"title": "Goal!", "body": "{glasses}/{goal}",
                                "compose": True, **notify_extra}}],
    }


def test_schema_compose_bool_only():
    validate_definition(_compose_def())
    bad = _compose_def()
    bad["watches"][0]["notify"]["compose"] = "yes"
    with pytest.raises(FlowletValidationError, match="compose"):
        validate_definition(bad)


async def test_compose_runs_agent_and_skips_static_notify(store):
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, agent_runner=cap.agent, tz=UTC)
    f = store.create("Water", _compose_def())
    store.set_state(f["id"], "glasses", 8)
    assert await eng.evaluate_one(f["id"]) == [f["id"]]
    # the agent composed + sent it — no static push
    assert cap.notifications == []
    assert len(cap.agent_calls) == 1
    fid, prompt = cap.agent_calls[0]
    assert fid == f["id"]
    assert f["id"] in prompt              # flowlet_id for the notify call
    assert "glasses" in prompt            # live data snapshot
    assert "[SILENT]" in prompt           # stays out of the chat


async def test_compose_falls_back_without_runner(store):
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, tz=UTC)  # no agent_runner
    f = store.create("Water", _compose_def())
    store.set_state(f["id"], "glasses", 8)
    await eng.evaluate_one(f["id"])
    assert cap.notifications == [(f["id"], "Goal!", "8/8")]  # static fallback


async def test_compose_falls_back_when_runner_raises(store):
    cap = _Capture()

    async def broken(flowlet, message):
        raise RuntimeError("model down")

    eng = WatchEngine(store, notify=cap.notify, agent_runner=broken, tz=UTC)
    f = store.create("Water", _compose_def())
    store.set_state(f["id"], "glasses", 8)
    await eng.evaluate_one(f["id"])
    assert cap.notifications == [(f["id"], "Goal!", "8/8")]


async def test_compose_throttled_uses_static(store):
    """Within the agent min-cooldown window compose degrades to the static
    push — a model call must never be cheap to trigger on a tight loop."""
    cap = _Capture()
    eng = WatchEngine(store, notify=cap.notify, agent_runner=cap.agent, tz=UTC)
    d = _compose_def()
    d["watches"][0]["cooldownMinutes"] = 0  # watch itself refires freely
    f = store.create("Water", d)
    fid = f["id"]
    store.set_state(fid, "glasses", 8)
    await eng.evaluate_one(fid)             # 1st: composed
    store.set_state(fid, "glasses", 2)
    await eng.evaluate_one(fid)             # falling edge
    store.set_state(fid, "glasses", 9)
    await eng.evaluate_one(fid)             # 2nd fire, inside 30-min window
    assert len(cap.agent_calls) == 1        # no second model call
    assert len(cap.notifications) == 1      # static push took over
