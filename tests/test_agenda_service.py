"""The agenda runner (flowly/agenda/service.py), with the agent turn faked."""

from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from flowly.agenda.budget import ProactiveBudget
from flowly.agenda.service import AgendaService
from flowly.objectives.store import ObjectiveStore

IST = ZoneInfo("Europe/Istanbul")
CHAT = "desktop:chat-1"


def _ms(day: int, hour: int, minute: int = 0) -> int:
    return int(dt.datetime(2026, 10, day, hour, minute, tzinfo=IST).timestamp() * 1000)


class Harness:
    def __init__(self, home, budget_path, *, reply: str = "Found two laptops under 40k.", fail: bool = False):
        self.now = _ms(13, 12)
        self.reply = reply
        self.fail = fail
        self.wakes: list[tuple[dict, str]] = []
        self.reviews: list[str] = []
        self.notified: list[tuple[str, str, str]] = []
        self.busy: set[str] = set()
        self.conversations = [{"key": CHAT, "title": "Laptop", "updated_at": "2026-10-12T10:00:00"}]
        self.service = AgendaService(
            profile="default",
            agent_name="Flowly",
            publish_wake=self.publish,
            is_busy=lambda key: key in self.busy,
            run_review=self.review,
            list_conversations=lambda: self.conversations,
            notify=self.notify,
            home=home,
            budget=ProactiveBudget(budget_path),
            clock=lambda: self.now / 1000,
            turn_timeout=2,
        )
        self.service.store.update_settings({"timezone": "Europe/Istanbul", "reviewTime": "08:30"})
        self.service.store.mark_review("2026-10-13")  # review covered separately

    async def publish(self, item: dict, prompt: str) -> None:
        self.wakes.append((item, prompt))
        if self.fail:
            return  # the turn never reports back

        async def finish():
            text = self.reply
            silent = text == "[SILENT]"
            self.service.turn_finished(item["id"], delivered=not silent, text="" if silent else text,
                                       session_key=item["sessionKey"])

        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(finish()))

    async def review(self, prompt: str) -> None:
        self.reviews.append(prompt)

    async def notify(self, session_key: str, text: str, item_id: str) -> None:
        self.notified.append((session_key, text, item_id))

    def follow_up(self, *, due: int | None = None, key: str = CHAT) -> dict:
        return self.service.store.schedule(
            kind="follow_up", due_at=due if due is not None else self.now - 1, note="Check the laptop prices",
            session_key=key,
        )


async def run_pass(h: "Harness") -> None:
    """One agenda pass, and the wake-ups it started, to the end."""
    await h.service.tick()
    await h.service.settle()


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path / "home", tmp_path / "budget.json")


@pytest.mark.asyncio
async def test_a_due_follow_up_wakes_the_agent_in_its_conversation_and_reaches_the_owner(harness):
    item = harness.follow_up()
    await run_pass(harness)

    assert len(harness.wakes) == 1
    woke, prompt = harness.wakes[0]
    assert woke["id"] == item["id"] and woke["sessionKey"] == CHAT
    assert "Check the laptop prices" in prompt and "[SILENT]" in prompt
    assert harness.notified == [(CHAT, "Found two laptops under 40k.", item["id"])]
    store = harness.service.store
    assert store.pending() == []
    assert store.snapshot()["history"][-1]["outcome"] == "delivered"
    assert store.conversation(CHAT)["unanswered"] == 1
    assert harness.service.budget.counts("default", zone=IST, now_ms=harness.now) == (1, 1)


@pytest.mark.asyncio
async def test_a_silent_turn_reaches_nobody_and_gives_its_slot_back(tmp_path):
    h = Harness(tmp_path / "home", tmp_path / "budget.json", reply="[SILENT]")
    h.follow_up()
    await run_pass(h)
    assert h.notified == []
    assert h.service.store.snapshot()["history"][-1]["outcome"] == "silent"
    assert h.service.store.conversation(CHAT).get("unanswered", 0) == 0
    assert h.service.budget.counts("default", zone=IST, now_ms=h.now) == (0, 0)


@pytest.mark.asyncio
async def test_a_turn_that_never_reports_back_is_retried_later(tmp_path):
    h = Harness(tmp_path / "home", tmp_path / "budget.json", fail=True)
    h.follow_up()
    await run_pass(h)
    pending = h.service.store.pending()
    assert len(pending) == 1 and pending[0]["attempts"] == 1 and pending[0]["dueAt"] > h.now
    assert h.service.budget.counts("default", zone=IST, now_ms=h.now) == (0, 0)


@pytest.mark.asyncio
async def test_quiet_hours_move_the_wake_up_to_the_morning_without_a_turn(harness):
    harness.now = _ms(13, 23)
    item = harness.follow_up()
    await run_pass(harness)
    assert harness.wakes == []
    pending = harness.service.store.pending()[0]
    assert pending["id"] == item["id"] and pending["dueAt"] == _ms(14, 9)
    assert pending["deferredReason"] == "quiet_hours"


@pytest.mark.asyncio
async def test_a_busy_conversation_is_not_interrupted(harness):
    harness.busy.add(CHAT)
    harness.follow_up()
    await run_pass(harness)
    assert harness.wakes == []
    assert harness.service.store.pending()[0]["deferredReason"] == "conversation_busy"


@pytest.mark.asyncio
async def test_the_daily_limit_holds_across_wake_ups(harness):
    for _ in range(3):
        harness.follow_up(key=f"desktop:chat-{_}")
    for _ in range(3):
        await run_pass(harness)
    assert len(harness.wakes) == 2  # normal: two a day
    left = harness.service.store.pending()
    assert len(left) == 1 and left[0]["deferredReason"] == "agent_daily_limit"


@pytest.mark.asyncio
async def test_unanswered_messages_hold_the_next_one_back(harness):
    harness.follow_up()
    await run_pass(harness)
    harness.now += 60 * 60 * 1000
    harness.follow_up()
    await run_pass(harness)
    assert len(harness.wakes) == 1
    assert harness.service.store.pending()[0]["deferredReason"] == "waiting_for_reply"

    # The owner answers: their silence there is over.
    harness.service.owner_wrote(CHAT)
    assert harness.service.store.pending() == []  # their reply settled that follow-up


@pytest.mark.asyncio
async def test_a_goal_check_in_carries_the_goal_and_stops_once_it_is_closed(harness, tmp_path):
    goals: ObjectiveStore = harness.service.goals
    goal = goals.create(title="Work out every day this week", category="health", session_key=CHAT,
                        plan=["Run 30 min"], cadence="every evening")
    harness.service.store.schedule(kind="objective", due_at=harness.now - 1, note="Day 1",
                                   session_key=CHAT, objective_id=goal["id"])
    await run_pass(harness)
    assert "Work out every day this week" in harness.wakes[0][1]
    assert "objective(action=\"log\")" in harness.wakes[0][1]

    harness.service.store.schedule(kind="objective", due_at=harness.now - 1, note="Day 2",
                                   session_key=CHAT, objective_id=goal["id"])
    goals.set_status(goal["id"], "paused")
    await run_pass(harness)
    assert len(harness.wakes) == 1
    assert harness.service.store.snapshot()["history"][-1]["reason"] == "goal_not_active"


@pytest.mark.asyncio
async def test_off_drops_every_wake_up(harness):
    harness.service.store.update_settings({"level": "off"})
    harness.follow_up()
    await run_pass(harness)
    assert harness.wakes == []
    assert harness.service.store.snapshot()["history"][-1]["reason"] == "proactivity_off"


@pytest.mark.asyncio
async def test_the_daily_review_runs_once_after_its_time_and_only_when_there_is_something(tmp_path):
    h = Harness(tmp_path / "home", tmp_path / "budget.json")
    h.service.store._transaction(lambda data: data.pop("lastReviewDay", None))
    h.now = _ms(13, 8)
    await run_pass(h)
    assert h.reviews == []  # before the review time

    h.now = _ms(13, 9)
    await run_pass(h)
    assert h.reviews == []  # nothing to review: no goals, the owner never wrote
    assert h.service.store.snapshot()["lastReviewDay"] == "2026-10-13"

    h.service.owner_wrote(CHAT)
    h.now = _ms(14, 9)
    await run_pass(h)
    await run_pass(h)
    assert len(h.reviews) == 1
    prompt = h.reviews[0]
    assert "[Daily review]" in prompt and CHAT in prompt and "You may still write first 2 time(s)" in prompt


@pytest.mark.asyncio
async def test_a_turn_finishing_after_the_wait_still_counts(harness):
    item = harness.follow_up()
    harness.service.turn_finished(item["id"], delivered=True, text="Late hello", session_key=CHAT)
    assert harness.service.store.conversation(CHAT)["unanswered"] == 1
    assert harness.service.store.snapshot()["history"][-1]["outcome"] == "delivered"


@pytest.mark.asyncio
async def test_wake_ups_in_different_conversations_run_together_but_never_two_in_one(tmp_path):
    h = Harness(tmp_path / "home", tmp_path / "budget.json")
    h.service.store.update_settings({"level": "high"})
    release = asyncio.Event()
    started: list[str] = []

    async def slow_publish(item, prompt):
        started.append(item["sessionKey"])

        async def finish():
            await release.wait()
            h.service.turn_finished(item["id"], delivered=False, text="", session_key=item["sessionKey"])

        asyncio.ensure_future(finish())

    h.service._publish_wake = slow_publish
    h.follow_up(key="desktop:chat-1")
    h.follow_up(key="desktop:chat-1")
    h.follow_up(key="desktop:chat-2")
    h.follow_up(key="desktop:chat-3")
    await h.service.tick()
    await asyncio.sleep(0.05)
    # Two at once, from two different conversations; the rest wait their turn.
    assert sorted(started) == ["desktop:chat-1", "desktop:chat-2"]
    await h.service.tick()
    await asyncio.sleep(0.05)
    assert len(started) == 2  # still at the limit, and chat-1 is busy
    release.set()
    await h.service.settle()
    await h.service.tick()
    await asyncio.sleep(0.05)
    assert sorted(started[2:]) == ["desktop:chat-1", "desktop:chat-3"]
    await h.service.settle()
