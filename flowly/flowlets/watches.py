"""Watches — declarative, LLM-free reactive rules attached to a flowlet.

A definition may carry a top-level ``watches`` array. Each entry is a rule the
bot evaluates *deterministically* (no model call) on two triggers:

  * a periodic heartbeat (:meth:`WatchEngine.run_heartbeat`, 60s) that walks
    every flowlet — catches ``schedule`` times, ``stale`` inactivity, and any
    time-guarded ``condition``; and
  * immediately after a client tap mutates state
    (:meth:`WatchEngine.evaluate_one`) — so a ``goal`` celebration or a
    threshold nudge lands the instant the user crosses it, not up to 60s later.

When a rule fires it sends a push / desktop reminder through the injected
``notify`` callback (backed by :func:`flowly.push.flowlet_push.notify_flowlet`)
and — only if the rule opts in with ``also: {op: "agent", ...}`` — wakes the
agent through the injected runner (throttled hard: a model call must never be
cheap to trigger on a tight loop).

The decision is edge-triggered and cooldown-gated: a condition that stays true
fires once on the false→true crossing, not on every tick. Per-watch runtime
state (``last_fired_ms`` / ``last_cond``) lives in the store so the behaviour
survives restarts and is shared between the heartbeat and the tap path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, tzinfo
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.flowlets import catalog
from flowly.flowlets.queries import eval_expr, render_template, resolve_values
from flowly.flowlets.store import FlowletStore
from flowly.flowlets.store import now_ms as _now_ms

# ── Pure helpers (no I/O — unit-tested directly) ──────────────────────────────

_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # datetime.weekday()

#: `{key}` substitution for notify title/body — the same templating computed
#: `cases` text uses (single implementation lives in queries).
render = render_template

NotifyFn = Callable[[str, str, str], Awaitable[None]]
AgentFn = Callable[[dict, str], Awaitable[None]]


def _parse_hhmm(s: Any) -> int | None:
    """"HH:MM" → minutes since local midnight, or None if malformed."""
    if not isinstance(s, str) or ":" not in s:
        return None
    hh, _, mm = s.partition(":")
    try:
        return int(hh) * 60 + int(mm)
    except ValueError:
        return None


def _cooldown_ms(watch: dict) -> int:
    cd = watch.get("cooldownMinutes")
    if isinstance(cd, int) and not isinstance(cd, bool) and cd >= 0:
        return cd * 60_000
    default = catalog.WATCH_DEFAULT_COOLDOWN_MIN.get(watch.get("trigger", ""), 360)
    return default * 60_000


def _eval_bool(expr: Any, values: dict) -> bool:
    """Evaluate a `when` expression to a bool. Fails safe to False on any error
    (unresolved name, non-numeric state) so a watch never fires spuriously."""
    if not expr:
        return False
    try:
        return eval_expr(str(expr), values) != 0
    except Exception:
        return False


def _eval_cond(watch: dict, values: dict, now_min: int) -> bool:
    """`when` gated by an optional `after` time-of-day guard."""
    if not _eval_bool(watch.get("when"), values):
        return False
    after = _parse_hhmm(watch.get("after"))
    if after is not None and now_min < after:
        return False
    return True


def _decide(
    watch: dict,
    values: dict,
    ws: dict,
    now_ms: int,
    tz: tzinfo | None,
    activity_ms: int | None,
) -> tuple[bool, bool | None]:
    """Pure fire/no-fire decision for a single watch.

    Returns ``(fire, new_last_cond)``. ``new_last_cond`` is the boolean edge
    state to persist for ``condition``/``goal`` triggers, or ``None`` for
    triggers that don't track an edge (``schedule``/``stale``) — the caller
    leaves the stored value untouched in that case.
    """
    trigger = watch.get("trigger")
    last_fired = ws.get("last_fired_ms")
    last_cond = bool(ws.get("last_cond"))
    tracks_edge = trigger in ("condition", "goal")

    if watch.get("once") and last_fired is not None:
        return False, (last_cond if tracks_edge else None)

    dt = datetime.fromtimestamp(now_ms / 1000, tz)
    now_min = dt.hour * 60 + dt.minute

    days = watch.get("days")
    if days and _WEEKDAY[dt.weekday()] not in {str(d).lower() for d in days}:
        # Off-day: don't fire and — crucially — don't consume the edge, so a
        # condition that becomes true on an off-day still fires on the next
        # allowed day rather than being silently swallowed.
        return False, (last_cond if tracks_edge else None)

    cooldown_ms = _cooldown_ms(watch)

    if trigger == "schedule":
        every = watch.get("everyMinutes")
        if every:
            if last_fired is None or now_ms - last_fired >= int(every) * 60_000:
                return True, None
            return False, None
        at_min = _parse_hhmm(watch.get("at"))
        if at_min is None or now_min < at_min:
            return False, None
        if last_fired is not None:
            last_dt = datetime.fromtimestamp(last_fired / 1000, tz)
            if last_dt.date() == dt.date():
                return False, None  # already fired today (catch-up, once/day)
        return True, None

    if tracks_edge:
        cond = _eval_cond(watch, values, now_min)
        rising = cond and not last_cond
        if not rising:
            return False, cond
        if last_fired is not None and now_ms - last_fired < cooldown_ms:
            return False, cond  # record the edge but stay quiet during cooldown
        return True, cond

    if trigger == "stale":
        if activity_ms is None:
            return False, None
        idle_ms = int(watch.get("idleMinutes", 0)) * 60_000
        if now_ms - activity_ms < idle_ms:
            return False, None
        if last_fired is not None:
            if now_ms - last_fired < cooldown_ms:
                return False, None
            if last_fired >= activity_ms:
                return False, None  # no fresh activity since the last reminder
        return True, None

    return False, None


# ── Engine (I/O; async) ───────────────────────────────────────────────────────


class WatchEngine:
    """Evaluates flowlet watches and fires their notifications.

    A single :class:`asyncio.Lock` serialises evaluation so the heartbeat and a
    concurrent tap can never both read ``last_cond`` before either persists —
    which would let one edge fire twice. Notification / agent I/O happens
    *after* the lock is released, keeping the critical section tiny.
    """

    def __init__(
        self,
        store: FlowletStore,
        *,
        notify: NotifyFn | None = None,
        agent_runner: AgentFn | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._notify = notify
        self._agent_runner = agent_runner
        self._tz = tz
        self._lock = asyncio.Lock()

    async def evaluate_one(
        self, flowlet_id: str, *, reason: str = "tap", now_ms: int | None = None
    ) -> list[str]:
        """Evaluate one flowlet's watches (e.g. right after a client tap).
        Returns the ids of watches that fired."""
        fl = self._store.get(flowlet_id)
        if not fl:
            return []
        return await self._evaluate(fl, reason=reason, now_ms=now_ms)

    async def evaluate_all(self, *, now_ms: int | None = None) -> list[str]:
        """Heartbeat pass: evaluate every flowlet that declares watches. One bad
        flowlet never aborts the sweep."""
        fired: list[str] = []
        for fl in self._store.list(limit=500):
            defn = fl.get("definition") or {}
            if not defn.get("watches"):
                continue
            try:
                fired += await self._evaluate(fl, reason="heartbeat", now_ms=now_ms)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[flowlet] watch eval failed for {}: {}", fl.get("id"), exc)
        return fired

    async def _evaluate(
        self, flowlet: dict, *, reason: str, now_ms: int | None
    ) -> list[str]:
        defn = flowlet.get("definition") or {}
        watches = defn.get("watches") or []
        if not watches:
            return []
        fid = flowlet["id"]
        now = now_ms if now_ms is not None else _now_ms()

        # ── critical section: decide + persist edge/cooldown state ────────────
        pending: list[tuple[str, str, dict | None, bool]] = []
        async with self._lock:
            state_map = self._store.get_state(fid)
            events = self._store.get_events(fid)
            values = resolve_values(defn, state_map, events, now, self._tz)
            ws_all = self._store.get_watch_state(fid)
            activity = self._store.last_activity_ms(fid)
            if activity is None:
                activity = flowlet.get("created_at")

            for w in watches:
                wid = w.get("id")
                if not isinstance(wid, str):
                    continue
                ws = ws_all.get(wid, {})
                try:
                    fire, new_cond = _decide(w, values, ws, now, self._tz, activity)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[flowlet] watch '{}' decide error: {}", wid, exc)
                    continue

                if new_cond is not None and new_cond != bool(ws.get("last_cond")):
                    self._store.set_watch_state(fid, wid, last_cond=new_cond)
                if not fire:
                    continue

                prev_fired = ws.get("last_fired_ms")
                self._store.set_watch_state(fid, wid, last_fired_ms=now, last_cond=new_cond)

                notify = w.get("notify") or {}
                title = render(notify.get("title"), values) or (flowlet.get("name") or "Flowlet")
                body = render(notify.get("body"), values)
                also = w.get("also")
                wake = bool(also) and (
                    prev_fired is None
                    or now - prev_fired >= catalog.WATCH_AGENT_MIN_COOLDOWN_MIN * 60_000
                )
                pending.append((title, body, also if wake else None, True))
                # DEBUG + opaque ids only: a fired watch is an internal event and
                # the flowlet name is user content — neither belongs in prod logs.
                logger.debug("[flowlet] watch '{}' fired on {} ({})", wid, fid, reason)

        if not pending:
            return []

        # ── side effects: outside the lock (network push / agent turn) ────────
        fired_ids: list[str] = []
        for title, body, also, _ in pending:
            fired_ids.append(fid)
            if self._notify is not None:
                try:
                    await self._notify(fid, title, body)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[flowlet] watch notify failed for {}: {}", fid, exc)
            if also and self._agent_runner is not None:
                msg = str(also.get("message") or "").strip()
                if msg:
                    try:
                        await self._agent_runner(flowlet, msg)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[flowlet] watch agent wake failed for {}: {}", fid, exc)
        return fired_ids

    async def run_heartbeat(
        self, *, interval_s: int = 60, stop_event: asyncio.Event | None = None
    ) -> None:
        """Tick every ``interval_s`` until cancelled or ``stop_event`` is set."""
        logger.info("[flowlet] watch heartbeat started (every {}s)", interval_s)
        try:
            while not (stop_event is not None and stop_event.is_set()):
                try:
                    fired = await self.evaluate_all()
                    if fired:
                        logger.debug("[flowlet] heartbeat fired {} watch(es)", len(fired))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[flowlet] heartbeat tick error: {}", exc)
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            pass
        logger.info("[flowlet] watch heartbeat stopped")
