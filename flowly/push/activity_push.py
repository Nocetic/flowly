"""Agent activity pushes: who is at work, for a phone's lock screen.

The phone's chat island (an iOS Live Activity) follows the host's agents
while the app runs. Once the app is suspended, only a push can keep it true.
This module follows every agent on the host — the main agent in process, and
named agents through the profile host's events — and pushes a compact,
platform-neutral snapshot through the relay whenever who works changes:

* the **lead**: the latest run the user started, any agent, with its stage,
  whether it waits on the user, and how it ended;
* the other **agents** at work, longest first;
* the lead's **subagents**.

Only work the user started counts (the apps' own conversations); routines,
heartbeats, channels and the host's internal hand-offs never wake a phone.
The relay owns everything Apple-specific (``live-activity-push.ts`` there).
No conversation or task title, message, tool argument or result is ever sent:
the snapshot passes the relay and the platform's push service, and the agent
cannot know whether the phone hides titles (Chat Lock).

A device hands its tokens over with ``push.register`` (see
``relay_push.PushRegistry.set_activity``): its island's update token, which
takes updates and the end, and its push-to-start token, which can open the
island when work starts while the app is not running.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

#: The apps' own conversations. Anything else with a prefix is a channel,
#: a routine or internal; a bare id is a gateway's or named agent's own.
FOREGROUND_PREFIXES = ("ios:", "android:", "desktop:", "web:")
#: The host's own hand-offs, which nobody wrote in.
INTERNAL_PREFIXES = ("desktop:profile-inbox:", "desktop:profile-task:", "desktop:voice-work:")

#: How long a settled lead stays, so its finish is seen.
LEAD_DONE_BEAT_MS = 4_000
#: How long another agent that finished stays.
AGENT_DONE_BEAT_MS = 1_600
#: A run nothing was heard from for this long lost its end to a dropped feed.
SILENCE_LIMIT_MS = 20 * 60_000
#: Ended subagents are kept this long, for the lead that started them.
SUBAGENT_KEEP_MS = 10 * 60_000
#: The most of each list sent; the rest are counts.
LIST_LIMIT = 3
#: Least time between two routine updates (the platform budgets them).
MIN_UPDATE_INTERVAL = 4.0
#: A push-to-start is not repeated within this long unless the phone hands
#: over the started island's token first.
START_COOLDOWN = 10 * 60.0
#: Seconds until the island marks itself possibly out of date without news.
STALE_AFTER_SECONDS = 15 * 60


def is_foreground(session_key: str) -> bool:
    """Whether work in ``session_key`` is work the user started."""
    key = (session_key or "").strip()
    if not key or key.startswith(INTERNAL_PREFIXES):
        return False
    if ":" not in key:
        return True
    return key.startswith(FOREGROUND_PREFIXES)


def stage_for_tool(name: str) -> tuple[str, str]:
    """``(stage, cue)`` a running tool puts its run in."""
    from flowly.activity.steps import describe_step

    kind = describe_step(name, {}).get("kind", "other")
    if kind == "exec":
        return "running", "terminal"
    if kind in {"search", "web", "memory"}:
        return "searching", "search"
    if kind in {"agent", "bot"}:
        return "delegating", "other"
    if kind == "media":
        return "shaping", "other"
    return "working", "other"


_OUTCOMES = {"completed": "done", "silent": "done", "error": "failed", "aborted": "stopped"}


@dataclass
class Identity:
    """An agent as the island draws it."""

    name: str
    display_name: str
    bot_id: str = ""
    seed: int | None = None
    shape: str = ""
    tone: str = ""
    is_default: bool = False

    def wire(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name, "displayName": self.display_name, "shape": self.shape,
            "tone": self.tone, "isDefault": self.is_default,
        }
        if self.bot_id:
            value["botId"] = self.bot_id
        if self.seed is not None:
            value["seed"] = self.seed
        return value


@dataclass
class _Run:
    agent: str
    session_key: str
    started_ms: int
    heard_ms: int
    stage: str = "thinking"
    cue: str | None = None
    calls: dict[str, tuple[str, str]] = field(default_factory=dict)
    awaiting: dict[str, str] = field(default_factory=dict)
    outcome: str | None = None
    ended_ms: int | None = None

    @property
    def live(self) -> bool:
        return self.outcome is None


@dataclass
class _Subagent:
    id: str
    agent: str
    parent: str
    glyph: str | None
    hue: int | None
    state: str
    started_ms: int
    ended_ms: int | None = None


class ActivityTracker:
    """Pure state: who works, from turn, tool, prompt and subagent reports.
    Times are epoch milliseconds, passed in."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], _Run] = {}
        self._subagents: dict[str, _Subagent] = {}

    # ── reports ──────────────────────────────────────────────────────────

    def begin(self, agent: str, session_key: str, now: int) -> _Run | None:
        if not is_foreground(session_key):
            return None
        key = (agent, session_key)
        run = self._runs.get(key)
        if run is None or not run.live:
            run = _Run(agent=agent, session_key=session_key, started_ms=now, heard_ms=now)
            self._runs[key] = run
        run.heard_ms = now
        return run

    def _live(self, agent: str, session_key: str, now: int, adopt: bool) -> _Run | None:
        run = self._runs.get((agent, session_key))
        if run is not None and run.live:
            run.heard_ms = now
            return run
        # A turn already going when this feed started (or whose begin it never
        # saw, as for a named agent): its first sign of life adopts it.
        return self.begin(agent, session_key, now) if adopt else None

    def tool(self, agent: str, session_key: str, call_id: str, name: str, started: bool, now: int) -> None:
        run = self._live(agent, session_key, now, adopt=started)
        if run is None:
            return
        if started:
            run.calls[call_id or name] = stage_for_tool(name)
        else:
            run.calls.pop(call_id or name, None)
        if run.calls:
            run.stage, run.cue = list(run.calls.values())[-1]
        else:
            run.stage, run.cue = "thinking", None

    def composing(self, agent: str, session_key: str, now: int) -> None:
        run = self._live(agent, session_key, now, adopt=True)
        if run is not None and not run.calls:
            run.stage, run.cue = "composing", None

    def prompt(self, session_key: str, prompt_id: str, kind: str, now: int) -> None:
        for run in self._runs.values():
            if run.session_key == session_key and run.live and prompt_id:
                run.awaiting[prompt_id] = kind
                run.heard_ms = now

    def resolve(self, session_key: str, prompt_id: str, now: int) -> None:
        for run in self._runs.values():
            if run.session_key == session_key and run.awaiting.pop(prompt_id, None) is not None:
                run.heard_ms = now

    def end(self, agent: str, session_key: str, outcome: str, now: int) -> None:
        run = self._runs.get((agent, session_key))
        if run is None or not run.live:
            return
        run.outcome = _OUTCOMES.get(outcome, outcome if outcome in {"done", "failed", "stopped"} else "done")
        run.ended_ms = now
        run.calls.clear()
        run.awaiting.clear()
        run.cue = None

    def subagent(self, agent: str, view: dict[str, Any], now: int) -> None:
        """One subagent run view (``subagent.*``). System runs and delegated
        tasks are not the agent's subagents."""
        if not isinstance(view, dict) or view.get("origin") == "system" or view.get("kind") == "delegate":
            return
        sub_id = str(view.get("threadId") or view.get("runId") or "")[:64]
        parent = str(view.get("parentSessionKey") or "")
        if not sub_id or not parent:
            return
        status = str(view.get("status") or "")
        state = {"running": "working", "ok": "finished", "error": "failed", "timeout": "failed"}.get(status, "stopped")
        mark = view.get("mark") if isinstance(view.get("mark"), dict) else {}
        hue = mark.get("hue")
        existing = self._subagents.get(sub_id)
        self._subagents[sub_id] = _Subagent(
            id=sub_id, agent=agent, parent=parent,
            glyph=mark.get("glyph") if isinstance(mark.get("glyph"), str) else None,
            hue=hue if isinstance(hue, int) and 0 <= hue <= 359 else None,
            state=state,
            started_ms=existing.started_ms if existing else now,
            ended_ms=None if state == "working" else (existing.ended_ms if existing and existing.ended_ms else now),
        )

    # ── reading ──────────────────────────────────────────────────────────

    def prune(self, now: int) -> None:
        for key, run in tuple(self._runs.items()):
            if now - run.heard_ms >= SILENCE_LIMIT_MS:
                del self._runs[key]
            elif not run.live and run.ended_ms is not None and now - run.ended_ms >= LEAD_DONE_BEAT_MS \
                    and not self._working_subagents(run):
                del self._runs[key]
        for sub_id, sub in tuple(self._subagents.items()):
            if sub.ended_ms is not None and now - sub.ended_ms >= SUBAGENT_KEEP_MS:
                del self._subagents[sub_id]

    def _working_subagents(self, run: _Run) -> list[_Subagent]:
        return [s for s in self._subagents.values()
                if s.agent == run.agent and s.parent == run.session_key and s.state == "working"]

    def _lead(self, now: int) -> _Run | None:
        candidates = [run for run in self._runs.values()
                      if run.live or now - (run.ended_ms or 0) < LEAD_DONE_BEAT_MS or self._working_subagents(run)]
        return max(candidates, key=lambda run: run.started_ms, default=None)

    def working(self, now: int) -> bool:
        if any(run.live and now - run.heard_ms < SILENCE_LIMIT_MS for run in self._runs.values()):
            return True
        lead = self._lead(now)
        return lead is not None and bool(self._working_subagents(lead))

    def snapshot(self, now: int, identities: dict[str, Identity]) -> dict[str, Any] | None:
        """The platform-neutral snapshot (schema 1, without its ``host``), or
        None when nobody is shown."""
        self.prune(now)
        lead = self._lead(now)

        def identity(name: str) -> Identity:
            return identities.get(name) or Identity(name=name, display_name=name, is_default=name == "default")

        agents: list[dict[str, Any]] = []
        by_agent: dict[str, list[_Run]] = {}
        for run in self._runs.values():
            if lead is not None and run.agent == lead.agent:
                continue
            by_agent.setdefault(run.agent, []).append(run)
        for name, runs in by_agent.items():
            live = [run for run in runs if run.live]
            if live:
                latest = max(live, key=lambda run: run.heard_ms)
                agents.append({**identity(name).wire(), "since": min(run.started_ms for run in live),
                               **({"cue": latest.cue} if latest.cue else {}), "done": False})
            else:
                ended = max(run.ended_ms or 0 for run in runs)
                if now - ended < AGENT_DONE_BEAT_MS:
                    agents.append({**identity(name).wire(), "since": ended, "done": True})
        agents.sort(key=lambda agent: (agent["since"], agent["name"]))
        if lead is None and not agents:
            return None

        subagents: list[dict[str, Any]] = []
        if lead is not None:
            mine = [s for s in self._subagents.values() if s.agent == lead.agent and s.parent == lead.session_key
                    and (s.state == "working" or (s.ended_ms or 0) >= lead.started_ms - 5_000)]
            mine.sort(key=lambda s: (s.state != "working", -s.started_ms))
            # Titles say what the conversation is about: never sent (see the
            # module docstring); the phone shows "Delegated task".
            subagents = [{"id": s.id, "state": s.state, **({"glyph": s.glyph} if s.glyph else {}),
                          **({"hue": s.hue} if s.hue is not None else {})} for s in mine]

        snapshot: dict[str, Any] = {
            "v": 1,
            "agents": agents[:LIST_LIMIT],
            "moreAgents": max(0, len(agents) - LIST_LIMIT),
            "subagents": subagents[:LIST_LIMIT],
            "moreSubagents": max(0, len(subagents) - LIST_LIMIT),
        }
        if lead is not None:
            awaiting = None
            if lead.live and lead.awaiting:
                awaiting = "approval" if "approval" in lead.awaiting.values() else "answer"
            snapshot["lead"] = {
                "agent": identity(lead.agent).wire(),
                "conversationId": _conversation_id(lead.session_key),
                "stage": lead.stage,
                **({"awaiting": awaiting} if awaiting else {}),
                **({"outcome": lead.outcome, "endedAt": lead.ended_ms} if lead.outcome else {}),
                "startedAt": lead.started_ms,
            }
        return snapshot

    def next_change(self, now: int) -> int | None:
        """When the snapshot next changes with no report: a beat ending."""
        moments = []
        for run in self._runs.values():
            if run.ended_ms is not None:
                moments += [run.ended_ms + AGENT_DONE_BEAT_MS, run.ended_ms + LEAD_DONE_BEAT_MS]
            moments.append(run.heard_ms + SILENCE_LIMIT_MS)
        future = [moment for moment in moments if moment > now]
        return min(future) if future else None


def _conversation_id(session_key: str) -> str:
    """The id the phone opens a conversation by: a relay chat's without its
    ``web:`` prefix, any other key as it is."""
    return session_key[4:] if session_key.startswith("web:") else session_key


def _identities() -> dict[str, Identity]:
    from flowly.profile import list_profiles

    found: dict[str, Identity] = {}
    for info in list_profiles():
        name = getattr(info, "name", "")
        if not name:
            continue
        display = (getattr(info, "display_name", "") or "").strip()
        is_default = bool(getattr(info, "is_default", False))
        found[name] = Identity(
            name=name,
            display_name=display or ("Flowly" if is_default else name),
            bot_id=getattr(info, "bot_id", "") or "",
            seed=getattr(info, "mark_seed", None),
            shape=getattr(info, "mark_shape", "") or "",
            tone=getattr(info, "mark_tone", "") or "",
            is_default=is_default,
        )
    return found


Sender = Callable[[str, str, dict[str, Any]], Awaitable[int]]


class ActivityPublisher:
    """Feeds the tracker from the host's events and pushes snapshots to every
    device that handed over a Live Activity token. Reports may come from any
    thread; pushes go out from the event loop, coalesced."""

    def __init__(
        self,
        *,
        registry: Any = None,
        sender: Sender | None = None,
        identities: Callable[[], dict[str, Identity]] = _identities,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._tracker = ActivityTracker()
        self._lock = threading.Lock()
        self._registry = registry
        self._sender = sender
        self._identities_source = identities
        self._identities: dict[str, Identity] = {}
        self._identities_at = 0.0
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._kick: asyncio.Event | None = None
        self._urgent = False
        self._task: asyncio.Task | None = None
        #: Per device (alert pushId): what it was last sent, and when.
        self._sent: dict[str, tuple[str, float]] = {}
        #: Per device: when a push-to-start went out.
        self._started: dict[str, float] = {}

    # ── wiring ───────────────────────────────────────────────────────────

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._kick = asyncio.Event()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run(), name="activity-push")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def _now(self) -> int:
        return int(self._clock() * 1000)

    def _changed(self, urgent: bool = False) -> None:
        loop, kick = self._loop, self._kick
        if loop is None or kick is None or loop.is_closed():
            return
        if urgent:
            self._urgent = True
        try:
            loop.call_soon_threadsafe(kick.set)
        except RuntimeError:  # the loop closed under us
            pass

    # ── reports: the main agent, in process ──────────────────────────────

    def on_turn(self, event: str, task: Any, outcome: str) -> None:
        """``ActivityRecorder`` listener: the main agent's turns."""
        session_key = str(getattr(task, "session_key", "") or "")
        trigger = getattr(task, "trigger", None) or {}
        if trigger.get("kind") != "owner":
            return
        with self._lock:
            if event == "begin":
                self._tracker.begin("default", session_key, self._now())
            elif event == "end":
                self._tracker.end("default", session_key, outcome, self._now())
        self._changed(urgent=True)

    async def on_tool_event(self, event_name: str, data: dict[str, Any]) -> None:
        """The main agent's ``tool.start`` / ``tool.complete``."""
        self._tool("default", event_name, data)

    def _tool(self, agent: str, event_name: str, data: dict[str, Any]) -> None:
        if event_name not in {"tool.start", "tool.complete"} or not isinstance(data, dict):
            return
        session_key = str(data.get("sessionKey") or "")
        with self._lock:
            self._tracker.tool(agent, session_key, str(data.get("toolCallId") or ""),
                               str(data.get("name") or ""), event_name == "tool.start", self._now())
        self._changed()

    async def on_approval_requested(self, pending: Any) -> None:
        self._prompt(getattr(pending, "session_key", "") or "", getattr(pending, "id", ""), "approval")

    async def on_clarify_requested(self, pending: Any) -> None:
        self._prompt(getattr(pending, "session_key", "") or "", getattr(pending, "id", ""), "answer")

    async def on_prompt_closed(self, prompt_id: str, reason: str, session_key: str) -> None:
        with self._lock:
            self._tracker.resolve(session_key or "", prompt_id, self._now())
        self._changed(urgent=True)

    def _prompt(self, session_key: str, prompt_id: str, kind: str) -> None:
        with self._lock:
            self._tracker.prompt(session_key, str(prompt_id or ""), kind, self._now())
        self._changed(urgent=True)

    async def on_subagent_event(self, event_name: str, data: dict[str, Any], agent: str = "default") -> None:
        if not event_name.startswith("subagent.") or not isinstance(data, dict):
            return
        with self._lock:
            self._tracker.subagent(agent, data, self._now())
        self._changed(urgent=event_name != "subagent.progress")

    # ── reports: named agents, through the profile host ──────────────────

    async def on_profile_event(self, envelope: dict[str, Any]) -> None:
        """``ProfileHost.subscribe_events`` callback. The main agent's events
        are heard in process and skipped here."""
        profile = str(envelope.get("profile") or "")
        event = str(envelope.get("type") or "")
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        if not profile or profile == "default":
            return
        session_key = str(data.get("sessionKey") or "")
        if event.startswith("subagent."):
            await self.on_subagent_event(event, data, agent=profile)
            return
        if event in {"tool.start", "tool.complete"}:
            self._tool(profile, event, data)
            return
        if event in {"exec.approval.requested", "agent.clarify.requested", "plan.approval.requested"}:
            kind = "answer" if event == "agent.clarify.requested" else "approval"
            self._prompt(session_key, str(data.get("id") or data.get("planId") or ""), kind)
            return
        if event in {"exec.approval.closed", "agent.clarify.closed"}:
            await self.on_prompt_closed(str(data.get("id") or ""), "", session_key)
            return
        with self._lock:
            now = self._now()
            if event == "agent":
                self._tracker.composing(profile, session_key, now)
            elif event == "chat":
                state = data.get("state")
                if state in {"final", "aborted", "error"}:
                    outcome = ("aborted" if state == "aborted" or data.get("aborted") is True
                               else "error" if state == "error" else "completed")
                    self._tracker.end(profile, session_key, outcome, now)
                elif state in {"iteration_step", "tool_progress", "run_started"}:
                    self._tracker.begin(profile, session_key, now)
                else:
                    return
            else:
                return
        self._changed(urgent=event == "chat")

    # ── pushing ──────────────────────────────────────────────────────────

    async def _run(self) -> None:
        assert self._kick is not None
        while True:
            with self._lock:
                upcoming = self._tracker.next_change(self._now())
            timeout = None if upcoming is None else max(0.05, upcoming / 1000 - self._clock())
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            urgent, self._urgent = self._urgent, False
            if not urgent:
                # Coalesce a burst of tool calls into one routine update.
                await asyncio.sleep(MIN_UPDATE_INTERVAL)
            try:
                await self.publish()
            except Exception:  # noqa: BLE001 — pushes never fail the host
                logger.opt(exception=True).debug("[activity-push] publish failed")

    def _registry_or_default(self) -> Any:
        if self._registry is None:
            from flowly.push.relay_push import get_push_registry

            self._registry = get_push_registry()
        return self._registry

    def _identity_map(self) -> dict[str, Identity]:
        if self._clock() - self._identities_at > 60:
            try:
                self._identities = self._identities_source()
            except Exception:  # noqa: BLE001 — names are presentation only
                logger.opt(exception=True).debug("[activity-push] profiles unreadable")
            self._identities_at = self._clock()
        return self._identities

    async def publish(self) -> None:
        """Push the current snapshot to every device with an island token."""
        devices = [sub for sub in self._registry_or_default().list()
                   if isinstance(sub.get("activity"), dict) or isinstance(sub.get("activityStart"), dict)]
        if not devices:
            return
        with self._lock:
            now = self._now()
            working = self._tracker.working(now)
            snapshot = self._tracker.snapshot(now, self._identity_map())
        sender = self._sender
        if sender is None:
            from flowly.push.relay_push import send_activity

            sender = send_activity
        await asyncio.gather(*(self._push(device, snapshot, working, now, sender) for device in devices))

    async def _push(self, device: dict[str, Any], snapshot: dict[str, Any] | None, working: bool,
                    now: int, sender: Sender) -> None:
        device_id = str(device.get("pushId") or "")
        ident = str(device.get("gatewayId") or "")
        if not device_id or not ident:
            return
        host = {"serverId": ident} if device.get("kind") == "relay" else {"gatewayId": ident}
        update = device.get("activity") if isinstance(device.get("activity"), dict) else None
        start = device.get("activityStart") if isinstance(device.get("activityStart"), dict) else None
        last, _at = self._sent.get(device_id, ("", 0.0))

        if snapshot is None:
            # Nobody is shown any more: end the island it was last sent to.
            if update is None or last in {"", "end"}:
                return
            body = self._body("end", now, {**self._empty(), "host": host})
            status = await sender(update["pushId"], update["pushSecret"], body)
            self._sent[device_id] = ("end", self._clock())
            # An ended island takes no more updates; the next one hands over its own.
            self._registry_or_default().set_activity(device_id, "activity", "", "")
            self._drop_if_dead(device_id, "activity", update, status)
            return

        full = {**snapshot, "host": host}
        fingerprint = repr(sorted((k, repr(v)) for k, v in full.items()))
        if update is not None:
            if fingerprint == last:
                return
            status = await sender(update["pushId"], update["pushSecret"],
                                  self._body("update", now, full))
            self._sent[device_id] = (fingerprint, self._clock())
            self._drop_if_dead(device_id, "activity", update, status)
            return
        if start is not None and working and self._clock() - self._started.get(device_id, 0.0) > START_COOLDOWN:
            status = await sender(start["pushId"], start["pushSecret"], self._body("start", now, full))
            self._started[device_id] = self._clock()
            self._sent[device_id] = (fingerprint, self._clock())
            self._drop_if_dead(device_id, "activityStart", start, status)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"v": 1, "agents": [], "moreAgents": 0, "subagents": [], "moreSubagents": 0}

    @staticmethod
    def _body(event: str, now: int, snapshot: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"event": event, "timestamp": now, "snapshot": snapshot}
        if event != "end":
            body["staleAfterSeconds"] = STALE_AFTER_SECONDS
        else:
            body["dismissAfterSeconds"] = 0
        return body

    def _drop_if_dead(self, device_id: str, kind: str, registration: dict[str, Any], status: int) -> None:
        from flowly.push.relay_push import DEAD_REGISTRATION

        if status in DEAD_REGISTRATION:
            self._registry_or_default().drop_activity(device_id, kind, str(registration.get("pushId") or ""))
            logger.info(f"[activity-push] dropped dead {kind} registration of {device_id[:8]}")


_publisher: ActivityPublisher | None = None


def get_activity_publisher() -> ActivityPublisher:
    global _publisher
    if _publisher is None:
        _publisher = ActivityPublisher()
    return _publisher


def wire_activity_push(*, profile_host: Any = None, approvals: Any = None, questions: Any = None) -> ActivityPublisher:
    """Hear the host's work and start pushing. Call once, from the running loop;
    the main agent's tool and subagent events are passed in by their wiring
    (``on_tool_event``, ``on_subagent_event``)."""
    from flowly.activity import get_activity_recorder

    publisher = get_activity_publisher()
    publisher.attach(asyncio.get_running_loop())
    get_activity_recorder().add_listener(publisher.on_turn)
    if approvals is not None:
        approvals.add_notify_callback(publisher.on_approval_requested)
        approvals.add_close_callback(publisher.on_prompt_closed)
    if questions is not None:
        questions.add_notify_callback(publisher.on_clarify_requested)
        questions.add_close_callback(publisher.on_prompt_closed)
    if profile_host is not None:
        profile_host.subscribe_events(publisher.on_profile_event)
    return publisher
