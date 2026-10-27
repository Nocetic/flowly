"""Per-run cooperative cancellation for chat turns.

The transport layers only know a ``run_id``.  This controller turns that
stable identifier into one cancellation contract shared by relay and direct
gateway chats:

* a durable, bounded "stop requested" marker for stream/tool-loop checks;
* immediate cancellation of the provider/tool awaitable currently owned by the run;
* preemption: a message the user sends while the run works cuts the running
  tool at once, without ending the run (the agent reads the message next);
* no cancellation leakage to unrelated concurrent runs.

The controller intentionally does not cancel the whole chat task.  The caller
must be allowed to persist the partial transcript and emit one authoritative
``state:"final", aborted:true`` terminal event.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TypeVar

T = TypeVar("T")
CURRENT_RUN_ID: ContextVar[str | None] = ContextVar('flowly_current_run_id', default=None)


class RunAbortedError(Exception):
    """Raised when a cancellable operation belongs to a stopped chat run."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run aborted: {run_id}")
        self.run_id = run_id


class RunPreemptedError(Exception):
    """Raised when an operation yields to a message the user sent during the run."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run preempted: {run_id}")
        self.run_id = run_id


class RunAbortController:
    """Track stop requests and the cancellable operation for each chat run."""

    def __init__(self, *, max_recent: int = 64) -> None:
        if max_recent < 1:
            raise ValueError("max_recent must be at least 1")
        self._max_recent = max_recent
        self._requested: set[str] = set()
        self._request_order: deque[str] = deque()
        self._active: dict[str, set[asyncio.Task[object]]] = {}
        self._preemptible: dict[str, set[asyncio.Task[object]]] = {}
        self._preempted: set[asyncio.Task[object]] = set()

    def request(self, run_id: str) -> bool:
        """Request cancellation for ``run_id``.

        Returns ``False`` only for an empty identifier.  Repeated requests are
        idempotent but still cancel any operation registered after the first
        request, which closes the request/register race.
        """
        if not run_id:
            return False

        if run_id not in self._requested:
            self._requested.add(run_id)
            self._request_order.append(run_id)
            self._prune_requested()

        for task in tuple(self._active.get(run_id, ())):
            if not task.done():
                task.cancel()
        return True

    def preempt(self, run_id: str) -> int:
        """Cut the run's preemptible operations; the run itself goes on.

        Returns how many were cut. An operation started without
        ``preemptible`` (a send, a write, a wait for the user's own answer)
        finishes: cutting it would leave "did it go out?" unknown.
        """
        cut = 0
        for task in tuple(self._preemptible.get(run_id, ())):
            if not task.done():
                self._preempted.add(task)
                task.cancel()
                cut += 1
        return cut

    def is_requested(self, run_id: str) -> bool:
        """Return whether the user requested cancellation for ``run_id``."""
        return bool(run_id) and run_id in self._requested

    def _prune_requested(self) -> None:
        """Bound old markers without evicting a run whose operation is active."""
        scanned = 0
        while (
            len(self._request_order) > self._max_recent
            and scanned < len(self._request_order)
        ):
            candidate = self._request_order.popleft()
            if self._active.get(candidate):
                self._request_order.append(candidate)
                scanned += 1
                continue
            self._requested.discard(candidate)
            scanned = 0

    async def run_cancellable(
        self,
        run_id: str,
        operation: Callable[[], Awaitable[T]],
        *,
        preemptible: bool = False,
    ) -> T:
        """Run one operation and bind its task to ``run_id``.

        ``operation`` is a factory, rather than an already-created coroutine,
        so a request that arrived before dispatch can fail closed without
        leaking an un-awaited coroutine. A ``preemptible`` one is also cut by
        :meth:`preempt` and then raises :class:`RunPreemptedError`.
        """
        if not run_id:
            return await operation()
        if self.is_requested(run_id):
            raise RunAbortedError(run_id)

        token = CURRENT_RUN_ID.set(run_id)
        try:
            task: asyncio.Task[T] = asyncio.create_task(
                operation(),
                name=f"flowly-run-operation:{run_id}",
            )
        finally:
            CURRENT_RUN_ID.reset(token)
        active = self._active.setdefault(run_id, set())
        active.add(task)
        if preemptible:
            self._preemptible.setdefault(run_id, set()).add(task)

        # A request can land between the pre-check and registration.
        if self.is_requested(run_id):
            task.cancel()

        try:
            return await task
        except asyncio.CancelledError:
            if self.is_requested(run_id):
                raise RunAbortedError(run_id) from None
            # Stop wins over a message; a cancel of the caller itself is no preemption.
            if task in self._preempted and not _caller_cancelling():
                raise RunPreemptedError(run_id) from None
            raise
        finally:
            active.discard(task)
            if not active:
                self._active.pop(run_id, None)
            preemptible_tasks = self._preemptible.get(run_id)
            if preemptible_tasks is not None:
                preemptible_tasks.discard(task)
                if not preemptible_tasks:
                    self._preemptible.pop(run_id, None)
            self._preempted.discard(task)
            self._prune_requested()


def _caller_cancelling() -> bool:
    current = asyncio.current_task()
    return bool(current and current.cancelling())
