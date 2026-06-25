"""Centralized approval manager with async Future-based waiting."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

from loguru import logger

from flowly.exec.types import PendingApproval, ExecApprovalDecision


# Type for channel notification callback
NotifyCallback = Callable[[PendingApproval], Awaitable[None]]


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
        self._notify_callbacks: list[NotifyCallback] = []

    def add_notify_callback(self, callback: NotifyCallback) -> None:
        """Register a channel callback for approval notifications."""
        self._notify_callbacks.append(callback)

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

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ExecApprovalDecision] = loop.create_future()
        self._futures[pending.id] = future
        self._pending[pending.id] = pending

        # Notify all registered channels
        logger.info(f"[ApprovalManager] Notifying {len(self._notify_callbacks)} channel(s) for {pending.id}")
        for cb in self._notify_callbacks:
            try:
                await cb(pending)
                logger.info(f"[ApprovalManager] Notify callback succeeded for {pending.id}")
            except Exception as e:
                logger.error(f"[ApprovalManager] Notify callback failed: {e}", exc_info=True)

        # Wait for decision with timeout
        timeout = max(0, pending.expires_at - time.time())
        try:
            decision = await asyncio.wait_for(future, timeout=timeout)
            logger.info(f"[ApprovalManager] {pending.id} resolved: {decision}")
            return decision
        except asyncio.TimeoutError:
            logger.info(f"[ApprovalManager] {pending.id} timed out")
            return None
        finally:
            self._futures.pop(pending.id, None)
            self._pending.pop(pending.id, None)

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
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def get_pending(self, approval_id: str) -> PendingApproval | None:
        return self._pending.get(approval_id)

    def list_pending(self) -> list[PendingApproval]:
        now = time.time()
        return [p for p in self._pending.values() if p.expires_at > now]


# Module-level singleton — shared across agent loop, channels, and gateway
_manager: ApprovalManager | None = None


def get_approval_manager() -> ApprovalManager:
    global _manager
    if _manager is None:
        _manager = ApprovalManager()
    return _manager
