"""Runs the agent's agenda: wakes it when a follow-up is due, once a day lets
it plan, and keeps every message it writes first inside the owner's limits.

One service per agent process (the main gateway and each named agent's
runtime). Every 30 seconds it looks at the due wake-ups, one at a time:

1. :mod:`flowly.agenda.policy` decides from the owner's settings and the
   conversation's history whether it may run now, must wait, or is dropped.
2. A slot is reserved in the budget all agents share (:mod:`.budget`).
3. The wake-up enters the agent as a hidden system turn in the conversation
   it is about (``channel="system"``, ``sender_id="agenda"``), the same way a
   finished background task reports back. The agent sees the conversation,
   may use its tools, and either writes the message or answers ``[SILENT]``.
4. The agent loop reports how the turn ended (:meth:`AgendaService.turn_finished`):
   a delivered message keeps its slot, counts as unanswered until the owner
   writes, and rings the owner's phone; a silent turn gives the slot back and
   leaves no trace in the conversation.

Once a day, after the review time, the agent reviews its goals and the
owner's recent conversations in an isolated session and plans the day's
follow-ups with its tools. It cannot write to the owner from there.

See ``docs/engineering/agent-goals.md``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.agenda import policy
from flowly.agenda.budget import ProactiveBudget
from flowly.agenda.prompts import review_prompt, wake_prompt
from flowly.agenda.store import AgendaStore, get_agenda_store, is_owner_conversation
from flowly.agenda.timeparse import owner_zone
from flowly.objectives.store import ObjectiveStore, get_objective_store

AGENDA_SENDER = "agenda"
#: Inbound metadata naming the wake-up a system turn belongs to.
AGENDA_ITEM_KEY = "agenda_item"
#: Inbound metadata naming the goal a check-in is about, for the card under it.
AGENDA_GOAL_KEY = "agenda_goal"
REVIEW_SESSION = "agenda:review"
TICK_SECONDS = 30.0
#: How long one wake-up's turn may take before it counts as failed.
TURN_TIMEOUT_SECONDS = 10 * 60
#: Wake-ups running at once, never two in one conversation. One that
#: researches for minutes does not hold up another conversation's.
MAX_CONCURRENT_WAKES = 2
REVIEW_TIMEOUT_SECONDS = 10 * 60
#: Without goals, a review runs only while the owner has written this recently.
REVIEW_RECENCY_MS = 7 * 24 * 60 * 60 * 1000
REVIEW_CONVERSATIONS = 8
#: Conversations whose new messages ring the owner's phone from here. A relay
#: chat is pushed by the relay itself; a messaging channel notifies natively.
_PHONE_PUSH_PREFIXES = ("desktop:", "ios:", "android:", "cli:", "tui:")

PublishWake = Callable[[dict[str, Any], str], Awaitable[None]]
RunReview = Callable[[str], Awaitable[Any]]


class AgendaService:
    def __init__(
        self,
        *,
        profile: str,
        agent_name: str,
        publish_wake: PublishWake,
        is_busy: Callable[[str], bool],
        run_review: RunReview,
        list_conversations: Callable[[], list[dict[str, Any]]],
        notify: Callable[[str, str, str], Awaitable[None]] | None = None,
        home: Path | None = None,
        budget: ProactiveBudget | None = None,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = TICK_SECONDS,
        turn_timeout: float = TURN_TIMEOUT_SECONDS,
    ) -> None:
        self.profile = profile
        self.agent_name = agent_name
        self.store: AgendaStore = get_agenda_store(home)
        self.goals: ObjectiveStore = get_objective_store(home)
        self.budget = budget or ProactiveBudget()
        self._publish_wake = publish_wake
        self._is_busy = is_busy
        self._run_review = run_review
        self._list_conversations = list_conversations
        self._notify = notify or notify_owner
        self._clock = clock
        self._tick_seconds = tick_seconds
        self._turn_timeout = turn_timeout
        self._task: asyncio.Task | None = None
        self._turns: dict[str, asyncio.Future] = {}
        self._tick_lock = asyncio.Lock()
        #: Running wake-ups: item id -> (its task, its conversation).
        self._running: dict[str, tuple[asyncio.Task, str]] = {}

    # ── lifecycle ────────────────────────────────────────────────────

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    def zone(self) -> dt.tzinfo:
        return owner_zone(self.store.settings().get("timezone"))

    async def start(self) -> None:
        try:
            recovered = self.store.recover_interrupted()
            if recovered:
                logger.info(f"[agenda] {recovered} interrupted wake-up(s) rescheduled")
        except Exception as exc:  # noqa: BLE001 — a bad file must not stop the gateway
            logger.warning(f"[agenda] recovery skipped: {exc}")
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for future in self._turns.values():
            if not future.done():
                future.cancel()
        self._turns.clear()
        for task, _session in self._running.values():
            task.cancel()
        self._running.clear()

    async def settle(self) -> None:
        """Wait for the wake-ups that are running now (tests, shutdown)."""
        while self._running:
            await asyncio.gather(*(task for task, _session in list(self._running.values())), return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._tick_seconds)
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[agenda] tick failed: {exc}")

    # ── owner signals ────────────────────────────────────────────────

    def owner_wrote(self, session_key: str) -> None:
        """The agent loop saw the owner write in ``session_key``."""
        if not is_owner_conversation(session_key):
            return
        try:
            cancelled = self.store.owner_wrote(session_key, self.now_ms())
            if cancelled:
                logger.info(f"[agenda] owner wrote in {session_key}; {cancelled} follow-up(s) no longer needed")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[agenda] owner signal skipped: {exc}")

    def turn_finished(
        self, item_id: str, *, delivered: bool, text: str, session_key: str, failed: bool = False,
    ) -> None:
        """The agent loop finished the system turn of wake-up ``item_id``."""
        future = self._turns.get(item_id)
        if future is not None and not future.done():
            if failed:
                future.set_exception(RuntimeError("the wake-up turn failed"))
            else:
                future.set_result((delivered, text))
            return
        if failed:
            return
        # The service stopped waiting (timeout, restart) but the turn still
        # spoke: the owner got the message, so count it.
        if delivered:
            try:
                self.store.delivered(session_key, self.now_ms())
                self.store.settle(item_id, "delivered", reason="late", preview=_preview(text))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[agenda] late outcome skipped: {exc}")

    # ── one pass ─────────────────────────────────────────────────────

    async def tick(self) -> None:
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            now = self.now_ms()
            try:
                await self._maybe_review(now)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[agenda] review failed: {exc}")
            busy_sessions = {session for _task, session in self._running.values()}
            for item in self.store.due(now):
                if len(self._running) >= MAX_CONCURRENT_WAKES:
                    break  # the next pass picks up the rest
                if item["sessionKey"] in busy_sessions or item["id"] in self._running:
                    continue  # one wake-up per conversation at a time
                if await self._consider(item, now):
                    busy_sessions.add(item["sessionKey"])

    def _facts(self, item: dict[str, Any], now: int) -> tuple[policy.Facts, dict[str, Any], dt.tzinfo]:
        settings = self.store.settings()
        zone = owner_zone(settings.get("timezone"))
        convo = self.store.conversation(item["sessionKey"])
        by_agent, by_all = self.budget.counts(self.profile, zone=zone, now_ms=now)
        goal_status = None
        if item.get("kind") == "objective":
            goal = self.goals.get(item.get("objectiveId") or "")
            goal_status = goal.get("status") if goal else None
        facts = policy.Facts(
            now_ms=now,
            zone=zone,
            level=settings.get("level", policy.DEFAULT_LEVEL),
            quiet_start=settings.get("quietStart", policy.DEFAULT_QUIET_START),
            quiet_end=settings.get("quietEnd", policy.DEFAULT_QUIET_END),
            unanswered=int(convo.get("unanswered") or 0),
            last_proactive_ms=convo.get("lastProactiveAt"),
            sent_today_by_agent=by_agent,
            sent_today_by_all=by_all,
            session_busy=bool(self._is_busy(item["sessionKey"])),
            objective_status=goal_status,
            is_objective=item.get("kind") == "objective",
            owner_last_active_ms=self.store.last_owner_at(),
        )
        return facts, settings, zone

    async def _consider(self, item: dict[str, Any], now: int) -> bool:
        """Act on one due wake-up. True when its turn was started."""
        facts, settings, zone = self._facts(item, now)
        decision = policy.decide(facts)
        if decision.action == "drop":
            self.store.settle(item["id"], "dropped", reason=decision.reason)
            logger.info(f"[agenda] {item['id']} dropped: {decision.reason}")
            return False
        if decision.action == "defer":
            self.store.defer(item["id"], int(decision.until_ms or now + policy.BUSY_RETRY_MS), decision.reason)
            return False

        level = facts.level if facts.level in policy.LEVELS else policy.DEFAULT_LEVEL
        token = self.budget.reserve(
            self.profile, zone=zone, now_ms=now,
            agent_cap=policy.DAILY_CAP_BY_LEVEL[level], global_cap=policy.GLOBAL_DAILY_CAP,
        )
        if token is None:  # another agent took the last slot in between
            self.store.defer(item["id"], policy.next_day_start(now, zone, facts.quiet_end), "daily_limit")
            return False
        started = self.store.start(item["id"])
        if started is None:
            self.budget.release(token)
            return False
        task = asyncio.create_task(self._wake(started, token, now, zone))
        self._running[started["id"]] = (task, started["sessionKey"])
        task.add_done_callback(lambda _t, item_id=started["id"]: self._running.pop(item_id, None))
        return True

    async def _wake(self, item: dict[str, Any], token: str, now: int, zone: dt.tzinfo) -> None:
        goal = self.goals.get(item["objectiveId"]) if item.get("objectiveId") else None
        prompt = wake_prompt(
            item,
            goal=goal,
            conversation=self.store.conversation(item["sessionKey"]),
            now_ms=now,
            zone=zone,
            agent_name=self.agent_name,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._turns[item["id"]] = future
        try:
            await self._publish_wake(item, prompt)
            delivered, text = await asyncio.wait_for(future, timeout=self._turn_timeout)
        except asyncio.CancelledError:
            self.budget.release(token)
            raise
        except Exception as exc:  # noqa: BLE001 — timeout or a turn that never came back
            self.budget.release(token)
            outcome = self.store.retry_or_give_up(item["id"], f"turn_failed: {type(exc).__name__}")
            logger.warning(f"[agenda] {item['id']} turn failed ({type(exc).__name__}); {outcome}")
            return
        finally:
            self._turns.pop(item["id"], None)

        if delivered:
            self.budget.confirm(token)
            self.store.delivered(item["sessionKey"], self.now_ms())
            self.store.settle(item["id"], "delivered", preview=_preview(text))
            logger.info(f"[agenda] {item['id']} delivered in {item['sessionKey']}")
            try:
                await self._notify(item["sessionKey"], text, item["id"])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[agenda] phone notification skipped: {exc}")
        else:
            self.budget.release(token)
            self.store.settle(item["id"], "silent")
            logger.info(f"[agenda] {item['id']} stayed silent")

    # ── daily review ─────────────────────────────────────────────────

    async def _maybe_review(self, now: int) -> bool:
        snapshot = self.store.snapshot()
        settings = snapshot["settings"]
        if settings.get("level") == "off":
            return False
        zone = owner_zone(settings.get("timezone"))
        local = dt.datetime.fromtimestamp(now / 1000, zone)
        day = local.date().isoformat()
        if snapshot.get("lastReviewDay") == day:
            return False
        hour, minute = policy.parse_clock(settings.get("reviewTime", ""), "08:30")
        if (local.hour, local.minute) < (hour, minute):
            return False

        goals = [g for g in self.goals.list(include_closed=False) if g.get("status") == "active"]
        last_owner = snapshot.get("lastOwnerAt")
        # Marked first: a review that crashes is not retried every 30 seconds.
        self.store.mark_review(day, now)
        if not goals and (not last_owner or now - int(last_owner) > REVIEW_RECENCY_MS):
            return False

        level = settings.get("level") if settings.get("level") in policy.LEVELS else policy.DEFAULT_LEVEL
        by_agent, by_all = self.budget.counts(self.profile, zone=zone, now_ms=now)
        remaining = max(0, min(policy.DAILY_CAP_BY_LEVEL[level] - by_agent, policy.GLOBAL_DAILY_CAP - by_all))
        pending = self.store.pending()
        next_checks = {
            i["objectiveId"]: int(i["dueAt"]) for i in pending if i.get("objectiveId")
        }
        conversations = [
            c for c in self._list_conversations() if is_owner_conversation(str(c.get("key") or ""))
        ][:REVIEW_CONVERSATIONS]
        prompt = review_prompt(
            now_ms=now,
            zone=zone,
            settings=settings,
            goals=goals,
            next_checks=next_checks,
            pending=pending,
            conversations=conversations,
            stats=snapshot.get("conversations") or {},
            last_owner_at=last_owner,
            remaining_today=remaining,
        )
        logger.info(f"[agenda] daily review ({len(goals)} goal(s), {len(conversations)} conversation(s))")
        await asyncio.wait_for(self._run_review(prompt), timeout=REVIEW_TIMEOUT_SECONDS)
        return True


def _preview(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")[:200]


async def notify_owner(session_key: str, text: str, item_id: str) -> None:
    """Ring the owner's phone for a message the agent wrote first.

    A relay chat (``web:``) is pushed by the relay when the message lands; a
    messaging channel (Telegram, …) notifies natively. Only conversations the
    agent serves itself are pushed from here, through the one notification
    path (:mod:`flowly.push.notifications`), which skips the phone while the
    owner is at a computer showing chats.
    """
    if not session_key.startswith(_PHONE_PUSH_PREFIXES):
        return
    from flowly.push import notifications
    from flowly.push.approval_push import phone_conversation

    await notifications.deliver(notifications.Notice(
        kind="chat",
        key=notifications.event_key("chat", "agenda", item_id),
        title=notifications.agent_name(),
        body=_preview(text) or "Sent you a message.",
        data={"agenda": "1"},
        conversation_id=phone_conversation(session_key),
    ))
