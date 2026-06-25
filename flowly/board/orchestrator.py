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
    BoardError,
    BoardStore,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_TODO,
    TERMINAL_STATUSES,
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

    # -- core execution -----------------------------------------------------

    async def _execute(self, card_id: str) -> tuple[str, Optional[str]]:
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

            self._store.set_status(card_id, STATUS_IN_PROGRESS)
            task: asyncio.Task = asyncio.ensure_future(
                self._spawn(
                    self._task_text(card),
                    label=card.id,
                    origin_channel=card.origin_channel,
                    origin_chat_id=card.origin_chat_id,
                    model=self._model,
                )
            )
            self._tasks[card_id] = task
            try:
                result = await task
                self._store.set_status(card_id, STATUS_DONE, result=_summarize(result))
                return ("done", _summarize(result))
            except asyncio.CancelledError:
                self._store.set_status(card_id, STATUS_CANCELLED, error="cancelled")
                return ("cancelled", None)
            except Exception as exc:
                # Failure is retryable: send the card back to todo with the
                # error recorded, rather than burying it in a terminal state.
                self._store.add_note(card_id, "system", f"run failed: {exc}")
                self._store.set_status(card_id, STATUS_TODO, error=str(exc), clear_run_id=True)
                return ("failed", str(exc))
            finally:
                self._tasks.pop(card_id, None)

    # -- public API ---------------------------------------------------------

    async def run_card(self, card_id: str, *, deliver: bool = True) -> dict[str, Any]:
        """Run a single existing card sequentially.

        ``deliver=True`` (the default, used by async/desktop-initiated runs)
        pushes the result DIRECTLY to the card's origin channel — no LLM
        relay turn. ``deliver=False`` is used by the agent's ``board_run``
        tool, which runs the card inline and returns the result itself, so
        the agent incorporates it in the same turn (no second turn, no
        "please don't call tools" prompt).
        """
        card = self._store.get_card(card_id)
        if card is None:
            raise BoardError(f"card not found: {card_id!r}")
        if self.is_running(card_id):
            raise BoardError(f"card already running: {card_id!r}")
        if card.status in TERMINAL_STATUSES:
            raise BoardError(f"card is {card.status}, nothing to run")

        outcome, payload = await self._execute(card_id)
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

        self._store.set_status(parent.id, STATUS_DONE, result=summary)
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
        task = self._tasks.get(card_id)
        if task is not None and not task.done():
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
            self._store.set_status(card_id, STATUS_CANCELLED, error="cancelled")
            return True
        return False
