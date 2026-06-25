"""Centralized clarify manager with async Future-based waiting.

Structurally a sibling of ``flowly.exec.approval_manager``: where the
approval manager resolves to an allow/deny *decision*, this one resolves
to a free-text *answer*. The agent coroutine awaits a Future that any
connected surface (desktop, TUI, mobile, chat channel) can complete.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from loguru import logger

from flowly.clarify.types import ClarifyRequest


# Type for surface notification callback
NotifyCallback = Callable[[ClarifyRequest], Awaitable[None]]


class ClarifyManager:
    """
    Manages agent clarify requests across all surfaces.

    When the agent asks a question:
    1. Creates an asyncio.Future for the answer
    2. Notifies all registered surfaces (desktop gateway, TUI, channels)
    3. Awaits the Future (agent is paused here)
    4. Any surface can resolve the Future via resolve()
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[str]] = {}
        self._pending: dict[str, ClarifyRequest] = {}
        self._notify_callbacks: list[NotifyCallback] = []

    def add_notify_callback(self, callback: NotifyCallback) -> None:
        """Register a surface callback for clarify notifications."""
        self._notify_callbacks.append(callback)

    async def request_and_wait(self, pending: ClarifyRequest) -> str | None:
        """
        Notify surfaces and wait for an answer.

        Returns the user's answer text, or ``None`` on timeout.
        The calling coroutine is PAUSED until resolve() is called or the
        timeout expires.

        Cron short-circuit: a scheduled run has no human at a surface to
        answer, so a clarify there can only hang. Inside a cron context we
        return ``None`` immediately (treated as "no answer") so the agent
        proceeds on its best judgement instead of blocking the schedule.
        """
        if self._in_cron_context():
            logger.info(
                "[ClarifyManager] Skipping clarify inside cron run "
                "(no surface to answer) — returning no answer"
            )
            return None

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._futures[pending.id] = future
        self._pending[pending.id] = pending

        logger.info(
            f"[ClarifyManager] Notifying {len(self._notify_callbacks)} "
            f"surface(s) for {pending.id}"
        )
        for cb in self._notify_callbacks:
            try:
                await cb(pending)
            except Exception as e:
                logger.error(
                    f"[ClarifyManager] Notify callback failed: {e}",
                    exc_info=True,
                )

        timeout = max(0, pending.expires_at - time.time())
        try:
            answer = await asyncio.wait_for(future, timeout=timeout)
            logger.info(f"[ClarifyManager] {pending.id} answered")
            return answer
        except asyncio.TimeoutError:
            logger.info(f"[ClarifyManager] {pending.id} timed out")
            return None
        finally:
            self._futures.pop(pending.id, None)
            self._pending.pop(pending.id, None)

    @staticmethod
    def _in_cron_context() -> bool:
        try:
            from flowly.cron.context import in_cron_context
            return bool(in_cron_context())
        except Exception:
            return False

    def resolve(self, clarify_id: str, answer: str) -> bool:
        """
        Resolve a pending clarify. Called from any surface (Gateway RPC,
        TUI panel, chat reply, ...).

        Returns True if the clarify was found and resolved.
        """
        future = self._futures.get(clarify_id)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def get_pending(self, clarify_id: str) -> ClarifyRequest | None:
        return self._pending.get(clarify_id)

    def list_pending(self) -> list[ClarifyRequest]:
        now = time.time()
        return [p for p in self._pending.values() if p.expires_at > now]


# Module-level singleton — shared across agent loop, channels, and gateway
_manager: ClarifyManager | None = None


def get_clarify_manager() -> ClarifyManager:
    global _manager
    if _manager is None:
        _manager = ClarifyManager()
    return _manager
