"""Async action lifecycle over the durable per-profile screen authority."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError

T = TypeVar("T")


class ActionOutcomeUnknownError(ScreenError):
    """Backend lost its acknowledgement; recovery must prove the action ended."""

    def __init__(self):
        super().__init__("ACTION_OUTCOME_UNKNOWN")


class ScreenExecution:
    """Keep backend actions alive until their real acknowledgement arrives.

    Cancelling a gateway request or losing its viewer is not proof that a
    browser command stopped. Human takeover fences results immediately, then
    waits for that command's acknowledgement before enabling input. Backend
    adapters must bound their operations and await all issued commands.
    """

    def __init__(self, authority: ScreenAuthority):
        self.authority = authority
        self._pending: dict[asyncio.Task, str] = {}
        self._run_checks: dict[str, Callable[[int | None], int]] = {}

    def bind_checkpoint(self, run_id: str, check: Callable[[int | None], int]) -> None:
        if run_id in self._run_checks:
            raise ScreenError("SCREEN_BUSY")
        self._run_checks[run_id] = check

    def unbind_checkpoint(self, run_id: str) -> None:
        self._run_checks.pop(run_id, None)

    async def action(self, run_id: str, action_id: str, generation: str,
                     operation: Callable[[], Awaitable[T]], *, refresh_epoch: int | None = None,
                     fence: Callable[[], Awaitable[None]] | None = None) -> T:
        """Run one backend operation under an action admission.

        ``fence`` severs the backend connection (the adapter's disconnect):
        once it has run, nothing the operation queued can reach the browser,
        which is the proof an unacknowledged command needs before its
        admission ends. Without a fence an unknown outcome keeps the
        admission, as a live command might still be typing.
        """
        async def execute() -> T:
            check = self._run_checks.get(run_id)
            if refresh_epoch is not None and check is None:
                raise ScreenError("RESUME_NOT_AVAILABLE")
            expected_epoch = await asyncio.to_thread(check, refresh_epoch) if check is not None else None
            admission = await asyncio.to_thread(self.authority.begin_action, run_id, action_id, generation,
                                                expected_epoch=expected_epoch)
            try:
                result = await operation()
            except ActionOutcomeUnknownError:
                # The backend's acknowledgement is lost. Severing its
                # connection ends whatever it had queued; only then may the
                # admission end (its result never reaches the model).
                if fence is None:
                    raise
                try:
                    await fence()
                except Exception:
                    raise ActionOutcomeUnknownError() from None
                await asyncio.to_thread(self.authority.end_action, admission)
                raise
            except asyncio.CancelledError:
                # Gateway shutdown: the admission records this process, and a
                # dead process's admission is void (authority._void_dead_claims).
                raise
            except Exception:
                # The adapter must not return/raise until its command has
                # ended. Cancelling only the caller never cancels this task.
                await asyncio.to_thread(self.authority.end_action, admission)
                raise
            valid = await asyncio.to_thread(self.authority.end_action, admission)
            if not valid:
                # No screenshot/text from a fenced action may reach the model.
                raise ScreenError("STALE_OBSERVATION")
            return result

        task = asyncio.create_task(execute())
        self._pending[task] = run_id

        def finished(completed: asyncio.Task) -> None:
            self._pending.pop(completed, None)
            if not completed.cancelled():
                completed.exception()  # retrieve failures if caller disconnected

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def acquire(self, viewer: str, generation: str, epoch: int, *,
                      reclaim: bool = False, wait_seconds: float = 2) -> dict:
        state = await asyncio.to_thread(self.authority.acquire, viewer, generation, epoch, reclaim=reclaim)
        if state["control"] == "human-pending":
            pending = list(self._pending)
            if pending and wait_seconds > 0:
                # wait() never cancels a still-running browser command.
                await asyncio.wait(pending, timeout=min(wait_seconds, 5))
            try:
                state = await asyncio.to_thread(self.authority.confirm_acquire, viewer, generation, state["epoch"])
            except ScreenError as exc:
                if exc.code != "TAKEOVER_PENDING":
                    raise
        return state

    async def finish_run(self, run_id: str, generation: str) -> None:
        pending = [task for task, owner in self._pending.items() if owner == run_id]
        if pending:
            # The run lease includes cleanup of commands surviving cancellation.
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        await asyncio.to_thread(self.authority.finish_run, run_id, generation)
