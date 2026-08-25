"""BoardOrchestrator — executes board cards, alone or in parallel.

Execution model
---------------
The orchestrator is the **single writer** of the board. To run work it
delegates to an injected ``spawn_fn`` (in production a thin wrapper over
``SubagentManager.spawn(wait=True)``) which runs a full agent turn on the
card's text and returns the result string. The orchestrator owns the
asyncio tasks for in-flight cards, so:

* concurrency is capped by an internal semaphore (``MAX_PARALLEL``);
* a card can be cancelled by cancelling its task (``cancel_card``);
* **subagents never touch ``board.db``** — they only execute and return;
  the orchestrator writes every status change.

Decomposition is intentionally NOT done here: the calling agent (already an
LLM) splits a goal into ``subtasks`` and passes them in. That keeps this
component LLM-free and fully unit-testable with a fake ``spawn_fn``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from flowly.board.store import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_READY,
    STATUS_TODO,
    STATUS_WAITING,
    TERMINAL_STATUSES,
    BoardError,
    BoardStore,
)

# A spawn function: run the given task to completion, return its result text.
SpawnFn = Callable[..., Awaitable[str]]
# A notify function: deliver a short message to a channel/chat (best-effort).
NotifyFn = Callable[[str, str, str], Awaitable[None]]

# Upper bound on the result stored on a card (and surfaced in every board
# snapshot). The bound exists ONLY because the result rides in every snapshot
# poll (and the board DB) — without it, a runaway agent dump (e.g. echoing a
# multi-MB file) would be re-sent on every poll. 200k chars (~40k words) covers
# any real task report in full; it's not a content limit, just a sanity guard.
_RESULT_CAP = 200_000


def _summarize(result: Optional[str]) -> str:
    text = (result or "").strip()
    if len(text) > _RESULT_CAP:
        return text[:_RESULT_CAP].rstrip() + "…"
    return text


class BoardOrchestrator:
    MAX_PARALLEL = 5
    MAX_PER_PROFILE = 1
    LEASE_SECONDS = 60.0
    HEARTBEAT_SECONDS = 20.0
    RECOVERY_SECONDS = 15.0

    def __init__(
        self,
        store: BoardStore,
        spawn_fn: SpawnFn,
        *,
        notify: Optional[NotifyFn] = None,
        on_finished: Optional[Callable[[Any, str], Awaitable[None]]] = None,
        model: Optional[str] = None,
    ):
        self._store = store
        self._spawn = spawn_fn
        self._notify = notify
        # Fired (card, outcome) whenever a card reaches done/failed, regardless
        # of ``deliver`` — used for out-of-band wakes (e.g. an APNs push so the
        # board UI's task result reaches the phone when it's closed).
        self._on_finished = on_finished
        self._model = model
        self._sem = asyncio.Semaphore(self.MAX_PARALLEL)
        # card_id -> the asyncio task running its spawn (for cancellation)
        self._tasks: dict[str, asyncio.Task] = {}
        # Explicit UI runs are reserved before their coroutine is scheduled.
        # Without this tiny handshake two fast Run clicks both received
        # "started" while neither task had reached ``claim_card`` yet.
        self._manual_tasks: dict[str, asyncio.Task] = {}
        self._profile_semaphores: dict[str, asyncio.Semaphore] = {}
        self._cancel_requests: set[str] = set()
        self._dispatch_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_wake = asyncio.Event()
        self._dispatcher_task: asyncio.Task | None = None
        self._stopping = False

    # -- helpers ------------------------------------------------------------

    async def _notify_safe(self, channel: str, chat_id: str, text: str) -> None:
        if not self._notify or not channel:
            return
        try:
            await self._notify(channel, chat_id, text)
        except Exception as exc:  # pragma: no cover - notify is best-effort
            logger.warning(f"[board] notify failed: {exc}")

    async def _on_finished_safe(self, card, outcome: str) -> None:
        if not self._on_finished or card is None:
            return
        try:
            await self._on_finished(card, outcome)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning(f"[board] on_finished failed: {exc}")

    @staticmethod
    def _task_text(card) -> str:
        if card.body:
            return f"{card.title}\n\n{card.body}"
        return card.title

    def is_running(self, card_id: str) -> bool:
        t = self._tasks.get(card_id)
        return t is not None and not t.done()

    def _profile_semaphore(self, profile: str) -> asyncio.Semaphore:
        key = profile or "default"
        semaphore = self._profile_semaphores.get(key)
        if semaphore is None:
            limit = self.MAX_PARALLEL if key == "default" else self.MAX_PER_PROFILE
            semaphore = asyncio.Semaphore(limit)
            self._profile_semaphores[key] = semaphore
        return semaphore

    async def _heartbeat(self, card_id: str, claim_token: str) -> None:
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_SECONDS)
                if not self._store.heartbeat_claim(
                    card_id,
                    claim_token,
                    lease_seconds=self.LEASE_SECONDS,
                ):
                    return
        except asyncio.CancelledError:
            raise

    # -- core execution -----------------------------------------------------

    async def _execute(
        self,
        card_id: str,
        *,
        ignore_schedule: bool = False,
    ) -> tuple[str, Optional[str]]:
        """Run one card to a terminal state. Returns (outcome, payload).

        outcome ∈ {"done", "failed", "cancelled"}. The orchestrator is the
        sole writer here — the spawned worker only returns a string.
        """
        async with self._sem:
            # Honor a cancellation that landed while queued on the semaphore.
            card = self._store.get_card(card_id)
            if card is None:
                return ("failed", "card not found")
            if card.status == STATUS_CANCELLED:
                return ("cancelled", None)

            worker = card.assignee_profile or "default"
            async with self._profile_semaphore(worker):
                claimed = self._store.claim_card(
                    card_id,
                    worker=worker,
                    lease_seconds=self.LEASE_SECONDS,
                    ignore_schedule=ignore_schedule,
                )
                if claimed is None or not claimed.claim_token:
                    return ("failed", "card is not eligible to run")
                spawn_kwargs = {
                    "label": claimed.id,
                    "origin_channel": claimed.origin_channel,
                    "origin_chat_id": claimed.origin_chat_id,
                    "model": self._model,
                }
                if claimed.assignee_profile:
                    spawn_kwargs.update({
                        "profile": claimed.assignee_profile,
                        "task_id": claimed.id,
                        "claim_token": claimed.claim_token,
                    })
                task: asyncio.Task = asyncio.ensure_future(
                    self._spawn(self._task_text(claimed), **spawn_kwargs)
                )
                heartbeat = asyncio.create_task(
                    self._heartbeat(claimed.id, claimed.claim_token),
                    name=f"board-heartbeat:{claimed.id}",
                )
                self._tasks[card_id] = task
                try:
                    result = await task
                    self._store.finish_claim(
                        card_id,
                        claimed.claim_token,
                        outcome="done",
                        result=_summarize(result),
                        actor=worker,
                    )
                    return ("done", _summarize(result))
                except asyncio.CancelledError:
                    user_cancelled = card_id in self._cancel_requests
                    outcome = "cancelled" if user_cancelled else "failed"
                    message = "cancelled" if user_cancelled else "runtime shutting down"
                    self._store.finish_claim(
                        card_id,
                        claimed.claim_token,
                        outcome=outcome,
                        error=message,
                        actor=worker,
                    )
                    return (outcome, None if user_cancelled else message)
                except Exception as exc:
                    retry_delay = min(300.0, 5.0 * (2 ** max(0, claimed.attempt_count - 1)))
                    self._store.finish_claim(
                        card_id,
                        claimed.claim_token,
                        outcome="failed",
                        error=str(exc),
                        retry_delay=retry_delay,
                        actor=worker,
                    )
                    return ("failed", str(exc))
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    self._tasks.pop(card_id, None)
                    self._cancel_requests.discard(card_id)
                    self.wake_dispatcher()

    # -- public API ---------------------------------------------------------

    def _validate_run(self, card_id: str) -> Any:
        card = self._store.get_card(card_id)
        if card is None:
            raise BoardError(f"card not found: {card_id!r}")
        dispatch_task = self._dispatch_tasks.get(card_id)
        if (
            card.claim_token
            or self.is_running(card_id)
            or card_id in self._manual_tasks
            or (dispatch_task is not None and not dispatch_task.done())
        ):
            raise BoardError(f"card already running: {card_id!r}")
        if card.status in TERMINAL_STATUSES:
            raise BoardError(f"card is {card.status}, nothing to run")
        if card.status not in {STATUS_TODO, STATUS_READY, STATUS_WAITING}:
            raise BoardError(f"card is {card.status}, not eligible to run")
        return card

    def start_card(self, card_id: str, *, deliver: bool = False) -> Any:
        """Reserve and start an explicit UI run, returning only if accepted.

        The old action handler returned ``started`` before the coroutine had
        checked the card.  A stale claim or a double click therefore failed
        only in a background log.  Reserving synchronously restores the
        original Board contract: success means this process accepted exactly
        one run; validation errors reach the caller immediately.
        """
        card = self._validate_run(card_id)

        async def _run() -> dict[str, Any]:
            return await self.run_card(
                card_id,
                deliver=deliver,
                ignore_schedule=True,
                _prevalidated=True,
            )

        task = asyncio.create_task(_run(), name=f"board-manual:{card_id}")
        self._manual_tasks[card_id] = task

        def _done(finished: asyncio.Task) -> None:
            self._manual_tasks.pop(card_id, None)
            if not finished.cancelled() and finished.exception() is not None:
                logger.error("[board] explicit card {} failed: {}", card_id, finished.exception())

        task.add_done_callback(_done)
        return card

    async def run_card(
        self,
        card_id: str,
        *,
        deliver: bool = True,
        ignore_schedule: bool = False,
        _prevalidated: bool = False,
    ) -> dict[str, Any]:
        """Run a single existing card sequentially.

        ``deliver=True`` (the default, used by async/desktop-initiated runs)
        pushes the result DIRECTLY to the card's origin channel — no LLM
        relay turn. ``deliver=False`` is used by the agent's ``board_run``
        tool, which runs the card inline and returns the result itself, so
        the agent incorporates it in the same turn (no second turn, no
        "please don't call tools" prompt).
        """
        card = self._store.get_card(card_id) if _prevalidated else self._validate_run(card_id)
        if card is None:
            raise BoardError(f"card not found: {card_id!r}")

        outcome, payload = await self._execute(card_id, ignore_schedule=ignore_schedule)
        card = self._store.get_card(card_id)
        title = card.title if card else card_id
        # Out-of-band finish hook (push, etc.) — fires regardless of deliver, so
        # a UI-run card (deliver=False) still wakes the phone when it completes.
        await self._on_finished_safe(card, outcome)
        if deliver:
            if outcome == "done":
                await self._notify_safe(
                    card.origin_channel, card.origin_chat_id,
                    f"Background task '{title}' finished.\n\nResult:\n{payload}\n\n"
                    "Relay this result to the user naturally and concisely, in "
                    "their language. The task is already complete.",
                )
            elif outcome == "failed":
                await self._notify_safe(
                    card.origin_channel, card.origin_chat_id,
                    f"Background task '{title}' failed: {payload}. Tell the user briefly.",
                )
        return {
            "ok": outcome == "done",
            "outcome": outcome,
            "result": payload,
            "card": card.to_dict() if card else None,
        }

    async def run_goal(
        self,
        goal: str,
        subtasks: list[str],
        *,
        origin_channel: str = "",
        origin_chat_id: str = "",
        deliver: bool = True,
    ) -> dict[str, Any]:
        """Decompose a goal into child cards and run them in parallel.

        ``subtasks`` is supplied by the calling agent (the decomposer).
        Children run under the concurrency cap; one consolidated report is
        sent when all reach a terminal state.
        """
        goal = (goal or "").strip()
        if not goal:
            raise BoardError("goal is required")
        clean = [s.strip() for s in (subtasks or []) if s and s.strip()]
        if not clean:
            raise BoardError("at least one subtask is required")

        parent = self._store.add_card(
            goal,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            created_by="agent",
        )
        children = [
            self._store.add_card(
                st,
                parent_id=parent.id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                created_by="agent",
            )
            for st in clean
        ]
        self._store.set_status(parent.id, STATUS_IN_PROGRESS)

        results = await asyncio.gather(*[self._execute(c.id) for c in children])
        done = sum(1 for o, _ in results if o == "done")
        failed = sum(1 for o, _ in results if o == "failed")
        cancelled = sum(1 for o, _ in results if o == "cancelled")

        parts = [f"{done}/{len(children)} done"]
        if failed:
            parts.append(f"{failed} failed")
        if cancelled:
            parts.append(f"{cancelled} cancelled")
        summary = ", ".join(parts)

        parent = self._store.set_status(parent.id, STATUS_DONE, result=summary)
        # Parallel board goals should wake the app once when the aggregate
        # parent finishes. Child cards are intentionally quiet to avoid sending
        # one push per subtask.
        await self._on_finished_safe(parent, "done")
        if deliver:
            await self._notify_safe(
                origin_channel, origin_chat_id,
                f"Background goal '{goal}' finished — {summary}. Tell the user "
                "briefly that it's done.",
            )
        return {
            "ok": True,
            "parentId": parent.id,
            "summary": summary,
            "done": done,
            "failed": failed,
            "cancelled": cancelled,
            "childIds": [c.id for c in children],
        }

    async def cancel_card(self, card_id: str) -> bool:
        """Cancel a running card (or mark a queued/active card cancelled).

        When a task is in flight we cancel it and await its unwind so the
        card is written to ``cancelled`` before this returns — callers (e.g.
        the gateway) can then read back an accurate status immediately.
        """
        task = self._manual_tasks.get(card_id) or self._tasks.get(card_id)
        if task is not None and not task.done():
            self._cancel_requests.add(card_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - worker error during cancel
                pass
            # Ensure terminal even if the unwind raced the finally block.
            card = self._store.get_card(card_id)
            if card is not None and card.status not in TERMINAL_STATUSES:
                self._store.set_status(card_id, STATUS_CANCELLED, error="cancelled")
            return True
        card = self._store.get_card(card_id)
        if card is not None and card.status not in TERMINAL_STATUSES:
            if card.claim_token:
                self._store.finish_claim(
                    card_id,
                    card.claim_token,
                    outcome="cancelled",
                    error="cancelled",
                    actor="user",
                )
            else:
                self._store.set_status(
                    card_id,
                    STATUS_CANCELLED,
                    error="cancelled",
                    actor="user",
                )
            return True
        return False

    # -- durable dispatcher ------------------------------------------------

    def wake_dispatcher(self) -> None:
        self._dispatch_wake.set()

    def assign_card(
        self,
        card_id: str,
        profile: str,
        *,
        actor: str = "user",
        expected_revision: int | None = None,
    ):
        """Validate an assignee against the primary profile directory."""
        from flowly.profile import ensure_profile_bot_id, profile_exists, validate_profile_name

        profile = (profile or "").strip()
        try:
            if profile != "default":
                validate_profile_name(profile)
        except ValueError as exc:
            raise BoardError(str(exc)) from exc
        if not profile_exists(profile):
            raise BoardError(f"unknown bot: {profile!r}")
        try:
            descriptor = ensure_profile_bot_id(profile)
        except (FileNotFoundError, ValueError) as exc:
            raise BoardError(f"unknown bot: {profile!r}") from exc
        card = self._store.assign_card(
            card_id,
            profile=profile,
            bot_id=descriptor.bot_id,
            actor=actor,
            # Assignment is metadata, not consent to execute.  Keep the
            # existing Board UX: a newly assigned card remains in Backlog
            # until the user explicitly presses Run or moves it to Ready.
            ready=False,
            expected_revision=expected_revision,
        )
        return card

    def unassign_card(
        self,
        card_id: str,
        *,
        actor: str = "user",
        expected_revision: int | None = None,
    ):
        card = self._store.unassign_card(
            card_id,
            actor=actor,
            expected_revision=expected_revision,
        )
        self.wake_dispatcher()
        return card

    def start_dispatcher(self) -> None:
        if self._dispatcher_task is not None and not self._dispatcher_task.done():
            return
        self._stopping = False
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(),
            name="board-dispatcher",
        )

    async def stop_dispatcher(self) -> None:
        self._stopping = True
        self._dispatch_wake.set()
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            await asyncio.gather(self._dispatcher_task, return_exceptions=True)
            self._dispatcher_task = None
        running = list(self._dispatch_tasks.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        self._dispatch_tasks.clear()
        manual = list(self._manual_tasks.values())
        for task in manual:
            task.cancel()
        await asyncio.gather(*manual, return_exceptions=True)
        self._manual_tasks.clear()

    async def dispatch_once(self) -> int:
        self._store.recover_expired_claims()
        available = max(0, self.MAX_PARALLEL - len(self._dispatch_tasks))
        if available == 0:
            return 0
        busy_profiles = {
            card.assignee_profile
            for card_id in self._dispatch_tasks
            if (card := self._store.get_card(card_id)) is not None
            and card.assignee_profile
        }
        started = 0
        for card in self._store.list_dispatchable(
            limit=available,
            exclude_profiles=tuple(busy_profiles),
        ):
            if (
                card.id in self._dispatch_tasks
                or card.id in self._manual_tasks
                or self.is_running(card.id)
            ):
                continue
            task = asyncio.create_task(
                self.run_card(card.id, deliver=False, _prevalidated=True),
                name=f"board-dispatch:{card.id}",
            )
            self._dispatch_tasks[card.id] = task

            def _done(finished: asyncio.Task, *, card_id: str = card.id) -> None:
                self._dispatch_tasks.pop(card_id, None)
                if not finished.cancelled() and finished.exception() is not None:
                    logger.error(
                        "[board] dispatched card {} failed: {}",
                        card_id,
                        finished.exception(),
                    )
                self.wake_dispatcher()

            task.add_done_callback(_done)
            started += 1
        return started

    async def _dispatch_loop(self) -> None:
        while not self._stopping:
            self._dispatch_wake.clear()
            await self.dispatch_once()
            try:
                await asyncio.wait_for(
                    self._dispatch_wake.wait(),
                    timeout=self.RECOVERY_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
