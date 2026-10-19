"""Who is at work, pushed to a phone's chat island (iOS Live Activity).

The tracker turns the host's turn, tool, prompt and subagent reports into a
platform-neutral snapshot; the publisher sends it through the relay to every
device that handed over an island token. Nothing about what a conversation
is about — titles, messages, arguments — ever leaves the host this way.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from flowly.push import activity_push as ap
from flowly.push.activity_push import ActivityPublisher, ActivityTracker, Identity, is_foreground
from flowly.push.relay_push import PushRegistry

T0 = 1_800_000_000_000


def ids() -> dict[str, Identity]:
    return {
        "default": Identity(name="default", display_name="Flowly", is_default=True),
        "ada": Identity(name="ada", display_name="Ada", bot_id="bot-ada", seed=3, shape="ghost", tone="violet"),
        "bo": Identity(name="bo", display_name="Bo", bot_id="bot-bo"),
    }


# ── foreground ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["ios:a", "android:a", "desktop:abc", "web:abc", "desktop:profile-room:r", "abc123"])
def test_work_the_user_started_counts(key: str) -> None:
    assert is_foreground(key)


@pytest.mark.parametrize("key", ["", "cron:daily", "heartbeat:main", "subagent:t", "telegram:42",
                                 "desktop:profile-inbox:x", "desktop:profile-task:y", "desktop:voice-work:c"])
def test_background_work_never_wakes_a_phone(key: str) -> None:
    assert not is_foreground(key)


# ── the tracker ─────────────────────────────────────────────────────────


def test_a_turn_leads_with_its_stage_and_the_running_tools_cue() -> None:
    tracker = ActivityTracker()
    tracker.begin("default", "web:c1", T0)
    tracker.tool("default", "web:c1", "call-1", "exec", True, T0 + 1)
    snap = tracker.snapshot(T0 + 2, ids())
    assert snap["lead"] == {
        "agent": {"name": "default", "displayName": "Flowly", "shape": "", "tone": "", "isDefault": True},
        "conversationId": "c1", "stage": "running", "startedAt": T0,
    }
    tracker.tool("default", "web:c1", "call-1", "exec", False, T0 + 3)
    assert tracker.snapshot(T0 + 4, ids())["lead"]["stage"] == "thinking"


def test_search_tools_and_writing_have_their_stages() -> None:
    tracker = ActivityTracker()
    tracker.tool("ada", "ios:c", "c1", "web_search", True, T0)
    assert tracker.snapshot(T0, ids())["lead"]["stage"] == "searching"
    tracker.tool("ada", "ios:c", "c1", "web_search", False, T0 + 1)
    tracker.composing("ada", "ios:c", T0 + 2)
    assert tracker.snapshot(T0 + 2, ids())["lead"]["stage"] == "composing"


def test_a_request_for_the_user_shows_approval_over_a_question() -> None:
    tracker = ActivityTracker()
    tracker.begin("default", "ios:c", T0)
    tracker.prompt("ios:c", "q1", "answer", T0 + 1)
    assert tracker.snapshot(T0 + 1, ids())["lead"]["awaiting"] == "answer"
    tracker.prompt("ios:c", "a1", "approval", T0 + 2)
    assert tracker.snapshot(T0 + 2, ids())["lead"]["awaiting"] == "approval"
    tracker.resolve("ios:c", "a1", T0 + 3)
    tracker.resolve("ios:c", "q1", T0 + 3)
    assert "awaiting" not in tracker.snapshot(T0 + 3, ids())["lead"]


@pytest.mark.parametrize(("outcome", "shown"), [("completed", "done"), ("silent", "done"),
                                                 ("error", "failed"), ("aborted", "stopped")])
def test_a_settled_lead_says_how_and_leaves_after_its_beat(outcome: str, shown: str) -> None:
    tracker = ActivityTracker()
    tracker.begin("default", "ios:c", T0)
    tracker.end("default", "ios:c", outcome, T0 + 10)
    lead = tracker.snapshot(T0 + 11, ids())["lead"]
    assert (lead["outcome"], lead["endedAt"]) == (shown, T0 + 10)
    assert not tracker.working(T0 + 11)
    assert tracker.next_change(T0 + 11) == T0 + 10 + ap.AGENT_DONE_BEAT_MS
    assert tracker.snapshot(T0 + 10 + ap.LEAD_DONE_BEAT_MS, ids()) is None


def test_the_latest_run_leads_and_the_others_are_agents_longest_first() -> None:
    tracker = ActivityTracker()
    tracker.tool("bo", "desktop:x", "1", "exec", True, T0)
    tracker.tool("ada", "ios:y", "2", "web_search", True, T0 + 5)
    tracker.begin("default", "web:z", T0 + 9)
    snap = tracker.snapshot(T0 + 10, ids())
    assert snap["lead"]["agent"]["name"] == "default"
    assert [(a["name"], a.get("cue"), a["since"]) for a in snap["agents"]] == [
        ("bo", "terminal", T0), ("ada", "search", T0 + 5)]
    assert snap["agents"][0]["botId"] == "bot-bo"


def test_an_agent_that_finished_stays_for_its_beat_then_goes() -> None:
    tracker = ActivityTracker()
    tracker.begin("default", "web:z", T0)
    tracker.begin("ada", "ios:y", T0 + 1)
    tracker.end("ada", "ios:y", "completed", T0 + 2)
    # A later lead shows Ada among the others, after the main agent (at work longer).
    tracker.begin("bo", "ios:w", T0 + 3)
    snap = tracker.snapshot(T0 + 3, ids())
    assert [(a["name"], a["done"]) for a in snap["agents"]] == [("default", False), ("ada", True)]
    assert [a["name"] for a in tracker.snapshot(T0 + 2 + ap.AGENT_DONE_BEAT_MS, ids())["agents"]] == ["default"]


def test_lists_are_capped_and_counted() -> None:
    tracker = ActivityTracker()
    for i in range(6):
        tracker.begin(f"agent{i}", f"ios:{i}", T0 + i)
    snap = tracker.snapshot(T0 + 10, {})
    assert len(snap["agents"]) == 3
    assert snap["moreAgents"] == 2


def test_subagents_are_the_leads_without_titles_and_keep_it_working() -> None:
    tracker = ActivityTracker()
    tracker.begin("default", "web:c", T0)
    view = {"runId": "r1", "threadId": "t1", "parentSessionKey": "web:c", "status": "running",
            "label": "Compare flights to Lisbon", "mark": {"glyph": "hex", "hue": 200}, "origin": "agent"}
    tracker.subagent("default", view, T0 + 1)
    tracker.subagent("default", {**view, "threadId": "t2", "runId": "r2", "parentSessionKey": "web:other"}, T0 + 1)
    tracker.subagent("default", {**view, "threadId": "t3", "runId": "r3", "origin": "system"}, T0 + 1)
    tracker.end("default", "web:c", "completed", T0 + 2)
    snap = tracker.snapshot(T0 + 2 + ap.LEAD_DONE_BEAT_MS, ids())
    assert snap["subagents"] == [{"id": "t1", "state": "working", "glyph": "hex", "hue": 200}]
    assert "Compare" not in repr(snap)
    assert tracker.working(T0 + 2 + ap.LEAD_DONE_BEAT_MS)
    tracker.subagent("default", {**view, "status": "ok"}, T0 + 10_000)
    assert not tracker.working(T0 + 10_001)
    assert tracker.snapshot(T0 + 10_000 + ap.LEAD_DONE_BEAT_MS, ids()) is None


def test_a_relay_conversation_is_opened_by_its_id() -> None:
    tracker = ActivityTracker()
    tracker.begin("ada", "raw-id", T0)
    assert tracker.snapshot(T0, ids())["lead"]["conversationId"] == "raw-id"


def test_a_run_silent_too_long_is_dropped() -> None:
    tracker = ActivityTracker()
    tracker.begin("ada", "ios:c", T0)
    assert tracker.working(T0 + ap.SILENCE_LIMIT_MS - 1)
    assert tracker.snapshot(T0 + ap.SILENCE_LIMIT_MS, ids()) is None


def test_a_tool_ending_never_starts_a_run() -> None:
    tracker = ActivityTracker()
    tracker.tool("ada", "ios:c", "c1", "exec", False, T0)
    tracker.tool("ada", "cron:job", "c2", "exec", True, T0)
    assert tracker.snapshot(T0, ids()) is None


# ── the publisher ───────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = T0 / 1000

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def phone(tmp_path):
    registry = PushRegistry(tmp_path / "push_subs.json")
    registry.register(push_id="phone", push_secret="s", gateway_id="srv-1", kind="relay")
    sent: list[tuple[str, dict]] = []
    answers: dict[str, int] = {}

    async def sender(push_id: str, secret: str, body: dict) -> int:
        sent.append((push_id, body))
        return answers.get(push_id, 200)

    clock = Clock()
    publisher = ActivityPublisher(registry=registry, sender=sender, identities=ids, clock=clock)
    return SimpleNamespace(registry=registry, sent=sent, answers=answers, clock=clock, publisher=publisher)


def owner_turn(session_key: str = "web:c1", kind: str = "owner") -> SimpleNamespace:
    return SimpleNamespace(session_key=session_key, trigger={"kind": kind})


@pytest.mark.asyncio
async def test_nothing_is_sent_to_a_phone_without_an_island_token(phone) -> None:
    phone.publisher.on_turn("begin", owner_turn(), "")
    await phone.publisher.publish()
    assert phone.sent == []


@pytest.mark.asyncio
async def test_a_phone_with_an_island_gets_updates_once_per_change_then_the_end(phone) -> None:
    phone.registry.set_activity("phone", "activity", "act-1", "sa")
    phone.publisher.on_turn("begin", owner_turn(), "")
    await phone.publisher.publish()
    await phone.publisher.publish()
    assert [(pid, body["event"]) for pid, body in phone.sent] == [("act-1", "update")]
    body = phone.sent[0][1]
    assert body["snapshot"]["host"] == {"serverId": "srv-1"}
    assert body["snapshot"]["lead"]["conversationId"] == "c1"
    assert body["staleAfterSeconds"] == ap.STALE_AFTER_SECONDS

    phone.publisher.on_turn("end", owner_turn(), "completed")
    await phone.publisher.publish()
    assert phone.sent[-1][1]["snapshot"]["lead"]["outcome"] == "done"

    phone.clock.now += ap.LEAD_DONE_BEAT_MS / 1000
    await phone.publisher.publish()
    assert phone.sent[-1][1]["event"] == "end"
    assert phone.sent[-1][1]["dismissAfterSeconds"] == 0
    # An ended island takes no more updates.
    assert "activity" not in phone.registry.list()[0]


@pytest.mark.asyncio
async def test_routines_and_channels_never_reach_the_island(phone) -> None:
    phone.registry.set_activity("phone", "activity", "act-1", "sa")
    phone.publisher.on_turn("begin", owner_turn("cron:job", kind="routine"), "")
    phone.publisher.on_turn("begin", owner_turn("telegram:1", kind="channel"), "")
    await phone.publisher.publish()
    assert phone.sent == []


@pytest.mark.asyncio
async def test_a_phone_with_only_a_start_token_gets_one_start(phone) -> None:
    phone.registry.set_activity("phone", "activityStart", "start-1", "ss")
    phone.publisher.on_turn("begin", owner_turn(), "")
    await phone.publisher.publish()
    phone.publisher.on_turn("begin", owner_turn("web:c2"), "")
    await phone.publisher.publish()
    assert [(pid, body["event"]) for pid, body in phone.sent] == [("start-1", "start")]


@pytest.mark.asyncio
async def test_a_gateway_phone_gets_its_gateway_as_host(phone) -> None:
    phone.registry.register(push_id="gw-phone", push_secret="s", gateway_id="gw-1", kind="gateway")
    phone.registry.set_activity("gw-phone", "activity", "act-g", "sg")
    phone.publisher.on_turn("begin", owner_turn(), "")
    await phone.publisher.publish()
    assert phone.sent[0][1]["snapshot"]["host"] == {"gatewayId": "gw-1"}


@pytest.mark.asyncio
async def test_a_dead_island_token_is_dropped(phone) -> None:
    phone.registry.set_activity("phone", "activity", "act-1", "sa")
    phone.answers["act-1"] = 410
    phone.publisher.on_turn("begin", owner_turn(), "")
    await phone.publisher.publish()
    assert "activity" not in phone.registry.list()[0]


@pytest.mark.asyncio
async def test_named_agents_are_heard_through_the_profile_host(phone) -> None:
    phone.registry.set_activity("phone", "activity", "act-1", "sa")
    envelope = {"profile": "ada", "type": "tool.start",
                "data": {"sessionKey": "ios:a", "toolCallId": "c1", "name": "exec", "args": {"command": "rm -rf /"}}}
    await phone.publisher.on_profile_event(envelope)
    # The main agent's own events come in process; never twice.
    await phone.publisher.on_profile_event({**envelope, "profile": "default"})
    await phone.publisher.publish()
    snap = phone.sent[-1][1]["snapshot"]
    assert snap["lead"]["agent"]["name"] == "ada"
    assert snap["lead"]["stage"] == "running"
    assert "rm -rf" not in repr(phone.sent)

    await phone.publisher.on_profile_event({"profile": "ada", "type": "chat",
                                            "data": {"sessionKey": "ios:a", "state": "final", "aborted": True}})
    await phone.publisher.publish()
    assert phone.sent[-1][1]["snapshot"]["lead"]["outcome"] == "stopped"


@pytest.mark.asyncio
async def test_prompts_reach_the_island(phone) -> None:
    phone.registry.set_activity("phone", "activity", "act-1", "sa")
    phone.publisher.on_turn("begin", owner_turn("ios:c"), "")
    await phone.publisher.on_approval_requested(SimpleNamespace(session_key="ios:c", id="ap-1"))
    await phone.publisher.publish()
    assert phone.sent[-1][1]["snapshot"]["lead"]["awaiting"] == "approval"
    await phone.publisher.on_prompt_closed("ap-1", "approved", "ios:c")
    await phone.publisher.publish()
    assert "awaiting" not in phone.sent[-1][1]["snapshot"]["lead"]


@pytest.mark.asyncio
async def test_the_publisher_coalesces_reports_from_the_running_loop(phone, monkeypatch) -> None:
    monkeypatch.setattr(ap, "MIN_UPDATE_INTERVAL", 0.01)
    phone.registry.set_activity("phone", "activity", "act-1", "sa")
    phone.publisher.attach(asyncio.get_running_loop())
    try:
        phone.publisher.on_turn("begin", owner_turn(), "")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if phone.sent:
                break
        assert [body["event"] for _pid, body in phone.sent] == ["update"]
    finally:
        await phone.publisher.close()


# ── registrations ───────────────────────────────────────────────────────


def test_re_registering_a_phone_keeps_its_island_tokens(tmp_path) -> None:
    registry = PushRegistry(tmp_path / "subs.json")
    registry.register(push_id="phone", push_secret="s", gateway_id="srv", kind="relay")
    assert registry.set_activity("phone", "activity", "act", "sa")
    registry.register(push_id="phone", push_secret="s2", gateway_id="srv", kind="relay")
    assert registry.list()[0]["activity"] == {"pushId": "act", "pushSecret": "sa"}
    registry.drop_activity("phone", "activity", "older")
    assert "activity" in registry.list()[0]
    registry.drop_activity("phone", "activity", "act")
    assert "activity" not in PushRegistry(tmp_path / "subs.json").list()[0]
    assert not registry.set_activity("stranger", "activity", "x", "y")
    assert not registry.set_activity("phone", "nonsense", "x", "y")


def test_push_register_takes_island_tokens(tmp_path, monkeypatch) -> None:
    from flowly.channels import feature_rpc
    from flowly.push import relay_push

    registry = PushRegistry(tmp_path / "subs.json")
    monkeypatch.setattr(relay_push, "_registry", registry)
    feature_rpc.push_register({"pushId": "phone", "pushSecret": "s", "gatewayId": "srv", "kind": "relay",
                               "activityPushId": "act", "activityPushSecret": "sa",
                               "activityStartPushId": "start", "activityStartPushSecret": "ss"})
    sub = registry.list()[0]
    assert sub["activity"] == {"pushId": "act", "pushSecret": "sa"}
    assert sub["activityStart"] == {"pushId": "start", "pushSecret": "ss"}
    # An older app sends no activity keys: they stay. An empty one drops it.
    feature_rpc.push_register({"pushId": "phone", "pushSecret": "s", "gatewayId": "srv", "kind": "relay"})
    assert "activity" in registry.list()[0]
    feature_rpc.push_register({"pushId": "phone", "pushSecret": "s", "gatewayId": "srv", "kind": "relay",
                               "activityPushId": ""})
    assert "activity" not in registry.list()[0]


def test_the_recorder_tells_its_listeners_each_turn() -> None:
    from flowly.activity.recorder import ActivityRecorder

    heard: list[tuple[str, str, str]] = []
    recorder = ActivityRecorder()
    recorder.add_listener(lambda event, task, outcome: heard.append((event, task.session_key, outcome)))
    recorder.add_listener(lambda *_: 1 / 0)  # a broken listener never fails a turn
    task = recorder.begin(session_key="web:c", task_id="run-1", trigger={"kind": "owner"}, request="hi")
    recorder.end(task, outcome="completed")
    assert heard == [("begin", "web:c", ""), ("end", "web:c", "completed")]
    assert recorder.begin(session_key="heartbeat:x", task_id="r2", trigger={"kind": "owner"}, request="") is None
    assert len(heard) == 2
