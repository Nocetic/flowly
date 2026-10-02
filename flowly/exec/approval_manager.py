"""Centralized approval manager with async Future-based waiting."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

from loguru import logger

from flowly.exec.types import PendingApproval, ExecApprovalDecision
from flowly.session.control_access import SessionControlScope, pending_control_guard


# Type for channel notification callback
NotifyCallback = Callable[[PendingApproval], Awaitable[None]]

# Fired once an approval stops waiting — decided here, decided on another
# surface, or timed out. Surfaces use it to retire the prompt they drew;
# without it a card outlives its request and its own countdown then fires a
# decision against an id this manager has already forgotten. Mirrors
# ``flowly.clarify.manager.CloseCallback``.
# Args: (approval_id, reason, session_key) where reason is the decision that
# settled it ("allow-once" / "allow-always" / "deny") or "timeout".
CloseCallback = Callable[[str, str, str], Awaitable[None]]


class ApprovalManager:
    """
    Manages exec approval requests across all channels.

    When an approval is needed:
    1. Creates an asyncio.Future for the decision
    2. Notifies all registered channels (Telegram, Gateway, etc.)
    3. Awaits the Future (agent is paused here)
    4. Any channel can resolve the Future via resolve()
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[ExecApprovalDecision]] = {}
        self._pending: dict[str, PendingApproval] = {}
        self._control_scopes: dict[str, SessionControlScope] = {}
        self._notify_callbacks: list[NotifyCallback] = []
        self._close_callbacks: list[CloseCallback] = []

    def add_notify_callback(self, callback: NotifyCallback) -> None:
        """Register a channel callback for approval notifications."""
        self._notify_callbacks.append(callback)

    def add_close_callback(self, callback: CloseCallback) -> None:
        """Register a channel callback for "this approval is settled"."""
        self._close_callbacks.append(callback)

    async def request_and_wait(
        self,
        pending: PendingApproval,
    ) -> ExecApprovalDecision | None:
        """
        Notify channels and wait for a decision.

        Returns the decision, or None on timeout.
        The calling coroutine is PAUSED until resolve() is called
        or the timeout expires.

        Cron short-circuit: if this request originated inside a scheduled
        (cron) run there's no user available to click approve/deny, so
        ``approvals.cron_mode`` decides the outcome synchronously:

          * ``"deny"`` (default, safe) — reject without notifying anyone,
            keeps scheduled runs from hanging on an unanswerable prompt.
          * ``"approve"`` — grant ``allow_once``, trust the schedule.
          * ``"ask"`` — fall through to the normal notify + wait flow
            (opt-in only; useful when a push to a paired device can resolve
            approvals for headless runs).

        This gate lives here — not in the exec executor — so EVERY tool
        that uses the approval manager (google_drive, email, linear,
        calendar, tasks, contacts, exec) gets the same cron policy for
        free. Previously only exec was guarded, so drive/gmail/linear
        hung forever when a cron agent tried them.
        """
        cron_decision = self._cron_mode_decision(pending)
        if cron_decision is not None:
            return cron_decision

        if pending.id in self._pending:
            raise ValueError('This request is already pending.')
        loop = asyncio.get_running_loop()
        from flowly.agent.run_abort import CURRENT_RUN_ID

        pending.run_id = CURRENT_RUN_ID.get()
        scope = SessionControlScope.capture(pending.session_key)
        self._control_scopes[pending.id] = scope
        future: asyncio.Future[ExecApprovalDecision] = loop.create_future()
        self._futures[pending.id] = future
        self._pending[pending.id] = pending

        # Assigned on every exit path so the close callbacks describe what
        # actually happened. "cancelled" is the honest default: it covers the
        # awaiting task being torn down mid-wait (gateway shutdown), which is
        # neither a decision nor a timeout.
        reason = "cancelled"
        # Surfaces are told in the background. A decision must never wait for
        # them: the gateway used to deliver the request to every registered
        # phone, one HTTP call after another, before reading the decision, so
        # an approval given in seconds took effect ~20 s later. The request
        # stays bounded by the same deadline through the cancellation below.
        logger.info(f"[ApprovalManager] Notifying {len(self._notify_callbacks)} channel(s) for {pending.id}")
        notifier = asyncio.create_task(self._notify(pending, scope), name=f"approval-notify-{pending.id}")
        try:
            async with asyncio.timeout(max(0, pending.expires_at - time.time())):
                decision = await future
            logger.info(f"[ApprovalManager] {pending.id} resolved: {decision}")
            reason = str(decision)
            return decision
        except asyncio.TimeoutError:
            logger.info(f"[ApprovalManager] {pending.id} timed out")
            reason = "timeout"
            return None
        finally:
            if not future.done():
                future.cancel()
            self._futures.pop(pending.id, None)
            self._pending.pop(pending.id, None)
            self._control_scopes.pop(pending.id, None)
            # Never retire a card before the request that drew it has gone
            # out: stop telling surfaces that have not been told yet, and let
            # a delivery already in flight finish, then close.
            if not notifier.done():
                notifier.cancel()
            await asyncio.gather(notifier, return_exceptions=True)
            from flowly.live_voice.events import EventAccess, event_access_scope

            with event_access_scope(EventAccess(scopes=(scope,), canonical=False)):
                await self._fire_close(pending, reason)

    async def _notify(self, pending: PendingApproval, scope: SessionControlScope) -> None:
        """Tell every surface about the request, one failure never stopping the rest."""
        from flowly.live_voice.events import event_access_scope

        for cb in self._notify_callbacks:
            try:
                with event_access_scope(scope):
                    await cb(pending)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error('[ApprovalManager] Notify callback failed ({})', type(e).__name__)

    async def _fire_close(self, pending: PendingApproval, reason: str) -> None:
        """Tell every channel the approval is settled so it can drop its card.

        Never lets a channel failure escape — the agent's decision must survive
        a broken client.
        """
        for cb in self._close_callbacks:
            try:
                await cb(pending.id, reason, pending.session_key or "")
            except Exception as error:
                logger.error('[ApprovalManager] Close callback failed ({})', type(error).__name__)

    @staticmethod
    def _cron_mode_decision(pending: PendingApproval) -> ExecApprovalDecision | None:
        """Resolve an approval synchronously if we're inside a cron run.

        Returns ``"allow-once"`` for cron_mode=approve, ``"deny"`` for
        cron_mode=deny, or ``None`` to fall through to normal notify.
        Non-cron contexts always fall through.
        """
        try:
            from flowly.cron.context import in_cron_context
            if not in_cron_context():
                return None
        except Exception:
            return None

        # Default deny — safe for unattended runs.
        mode = "deny"
        try:
            from flowly.config.loader import load_config
            cfg = load_config()
            mode = str(getattr(cfg.tools.exec, "cron_mode", "deny") or "deny").lower()
        except Exception as e:
            logger.debug(
                f"[ApprovalManager] cron_mode lookup failed, defaulting to deny: {e}"
            )

        if mode == "approve":
            logger.warning(
                f"[ApprovalManager] Cron auto-approving '{pending.request.command[:60]}' "
                f"(cron_mode=approve)"
            )
            return "allow-once"
        if mode == "ask":
            # Explicit opt-in — let the request go through the normal
            # notify flow so a paired device can resolve it.
            return None

        logger.info(
            f"[ApprovalManager] Cron auto-denying '{pending.request.command[:60]}' "
            f"(cron_mode=deny)"
        )
        return "deny"

    def resolve(self, approval_id: str, decision: ExecApprovalDecision) -> bool:
        """
        Resolve a pending approval. Called from any channel (Telegram button, Gateway RPC, etc.).

        Returns True if the approval was found and resolved.
        """
        future = self._futures.get(approval_id)
        pending = self._pending.get(approval_id)
        if future is None or future.done() or pending is None or pending.expires_at <= time.time():
            return False
        with pending_control_guard(self._control_scopes, pending) as allowed:
            if not allowed:
                return False
            future.set_result(decision)
            return True

    def get_pending(self, approval_id: str) -> PendingApproval | None:
        pending = self._pending.get(approval_id)
        if pending is None:
            return None
        with pending_control_guard(self._control_scopes, pending) as allowed:
            return pending if allowed else None

    def list_pending(self) -> list[PendingApproval]:
        now = time.time()
        return [p for p in self._pending.values() if p.expires_at > now and self.get_pending(p.id) is not None]


# Module-level singleton — shared across agent loop, channels, and gateway
_manager: ApprovalManager | None = None


def get_approval_manager() -> ApprovalManager:
    global _manager
    if _manager is None:
        _manager = ApprovalManager()
    return _manager
