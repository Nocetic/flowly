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
from flowly.session.control_access import SessionControlScope, pending_control_guard


# Type for surface notification callback
NotifyCallback = Callable[[ClarifyRequest], Awaitable[None]]

# Fired once a question stops waiting — answered here, answered on another
# surface, or timed out. Surfaces use it to retire the prompt they drew;
# without it a tray outlives its question and the answer typed into it is
# rejected as unknown. Args: (clarify_id, reason, session_key).
CloseCallback = Callable[[str, str, str], Awaitable[None]]


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
        self._control_scopes: dict[str, SessionControlScope] = {}
        self._notify_callbacks: list[NotifyCallback] = []
        self._close_callbacks: list[CloseCallback] = []

    def add_notify_callback(self, callback: NotifyCallback) -> None:
        """Register a surface callback for clarify notifications."""
        self._notify_callbacks.append(callback)

    def add_close_callback(self, callback: CloseCallback) -> None:
        """Register a surface callback for "this question is over"."""
        self._close_callbacks.append(callback)

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

        if pending.id in self._pending:
            raise ValueError('This request is already pending.')
        loop = asyncio.get_running_loop()
        from flowly.agent.run_abort import CURRENT_RUN_ID

        pending.run_id = CURRENT_RUN_ID.get()
        scope = SessionControlScope.capture(pending.session_key)
        self._control_scopes[pending.id] = scope
        future: asyncio.Future[str] = loop.create_future()
        self._futures[pending.id] = future
        self._pending[pending.id] = pending

        reason = "cancelled"
        try:
            async with asyncio.timeout(max(0, pending.expires_at - time.time())):
                logger.info(
                    f"[ClarifyManager] Notifying {len(self._notify_callbacks)} surface(s) for {pending.id}"
                )
                for cb in self._notify_callbacks:
                    try:
                        from flowly.live_voice.events import event_access_scope

                        with event_access_scope(scope):
                            await cb(pending)
                    except Exception as e:
                        logger.error('[ClarifyManager] Notify callback failed ({})', type(e).__name__)
                answer = await future
            reason = "answered"
            logger.info(f"[ClarifyManager] {pending.id} answered")
            return answer
        except asyncio.TimeoutError:
            logger.info(f"[ClarifyManager] {pending.id} timed out")
            reason = "timeout"
            return None
        finally:
            if not future.done():
                future.cancel()
            self._futures.pop(pending.id, None)
            self._pending.pop(pending.id, None)
            self._control_scopes.pop(pending.id, None)
            from flowly.live_voice.events import EventAccess, event_access_scope

            with event_access_scope(EventAccess(scopes=(scope,), canonical=False)):
                await self._fire_close(pending, reason)

    async def _fire_close(self, pending: ClarifyRequest, reason: str) -> None:
        """Tell every surface the question is over so it can drop its prompt.

        Never lets a surface failure escape — the agent's answer must survive
        a broken client.
        """
        for cb in self._close_callbacks:
            try:
                await cb(pending.id, reason, pending.session_key or "")
            except Exception as error:
                logger.error('[ClarifyManager] Close callback failed ({})', type(error).__name__)

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
        pending = self._pending.get(clarify_id)
        if future is None or future.done() or pending is None or pending.expires_at <= time.time():
            return False
        with pending_control_guard(self._control_scopes, pending) as allowed:
            if not allowed:
                return False
            future.set_result(answer)
            return True

    def get_pending(self, clarify_id: str) -> ClarifyRequest | None:
        pending = self._pending.get(clarify_id)
        if pending is None:
            return None
        with pending_control_guard(self._control_scopes, pending) as allowed:
            return pending if allowed else None

    def list_pending(self) -> list[ClarifyRequest]:
        now = time.time()
        return [p for p in self._pending.values() if p.expires_at > now and self.get_pending(p.id) is not None]


# Module-level singleton — shared across agent loop, channels, and gateway
_manager: ClarifyManager | None = None


def get_clarify_manager() -> ClarifyManager:
    global _manager
    if _manager is None:
        _manager = ClarifyManager()
    return _manager
