"""In-memory continuation of a durable screen checkpoint in its original run.

The agent loop must await `wait` before its next model request. Only the live
origin execution can consume a resume request. No tool command is retained for
replay: the continuation receives a newly fenced page observation instead.
"""

from __future__ import annotations

import asyncio
import secrets

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.handoff import RunHandoffs


class ScreenRunCheckpoint:
    INTERVAL = 0.2

    def __init__(self, provider, store: RunHandoffs, execution: str, run_id: str, generation: str):
        self.provider, self.store, self.execution = provider, store, execution
        self.run_id, self.generation = run_id, generation
        self.provider.execution.bind_checkpoint(run_id, self.action_epoch)

    @classmethod
    async def start(cls, provider, identity: dict, session_key: str, run_id: str, generation: str):
        # Retain a claimed run on a failed checkpoint write: losing persistence
        # is not evidence that another task may inherit the browser.
        def prepare():
            provider.runtime.authority.claim_run(run_id, generation)
            store = RunHandoffs(provider.runtime.directory, provider.runtime.authority)
            execution = store.start(identity, session_key, run_id, generation)
            return store, execution

        preparation = asyncio.create_task(asyncio.to_thread(prepare))
        try:
            store, execution = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            try:
                store, execution = await asyncio.shield(preparation)
                await asyncio.to_thread(store.finish, execution, cancelled=True)
                await provider.execution.finish_run(run_id, generation)
            except Exception:
                # Failed persistence or unknown work requires explicit recovery.
                pass
            raise
        return cls(provider, store, execution, run_id, generation)

    def action_epoch(self, refresh_epoch: int | None = None) -> int:
        status = self.store.checkpoint(self.execution)
        if refresh_epoch is None:
            if status["state"] != "ready":
                raise ScreenError("SCREEN_HANDOFF_REQUIRED")
        elif status["state"] != "refreshing" or status["controlEpoch"] != refresh_epoch:
            raise ScreenError("STALE_OBSERVATION")
        return status["controlEpoch"]

    async def wait(self) -> dict | None:
        """Wait without model polling; cancellation never releases human input."""
        try:
            while True:
                status = await asyncio.to_thread(self.store.checkpoint, self.execution)
                if status["state"] == "ready":
                    return None
                epoch = await asyncio.to_thread(self.store.begin_refresh, self.execution)
                if epoch is not None:
                    result = await self.provider.request(self.run_id, secrets.token_urlsafe(24), self.generation,
                                                         "read_page", {}, _refresh_epoch=epoch)
                    if result.get("error_code") in {"STALE_OBSERVATION", "STALE_EPOCH", "HUMAN_HAS_CONTROL"}:
                        # Another takeover fences the observation. Wait for that
                        # handoff; never retry a write or infer another approval.
                        await asyncio.sleep(self.INTERVAL)
                        continue
                    if (result.get("error") or result.get("_screenObservation") != {
                            "generation": self.generation, "epoch": epoch}):
                        raise ScreenError("HANDOFF_RECOVERY_REQUIRED")
                    try:
                        await asyncio.to_thread(self.store.complete_refresh, self.execution, epoch)
                    except ScreenError as exc:
                        if exc.code != "STALE_OBSERVATION":
                            raise
                        continue
                    return {key: value for key, value in result.items() if key != "_screenObservation"}
                await asyncio.sleep(self.INTERVAL)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.finish, self.execution, cancelled=True)
            raise

    async def finish(self, *, cancelled: bool) -> None:
        # Mark the durable continuation terminal before retiring action admission.
        # Outstanding actual browser commands still drain in finish_run.
        await asyncio.to_thread(self.store.finish, self.execution, cancelled=cancelled)
        await self.provider.execution.finish_run(self.run_id, self.generation)
        self.provider.execution.unbind_checkpoint(self.run_id)
