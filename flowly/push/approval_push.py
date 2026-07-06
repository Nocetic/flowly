"""Exec-approval push notifications."""

from __future__ import annotations

from typing import Any

from loguru import logger


async def notify_approval_requested(pending: Any) -> None:
    """Send a best-effort push when an approval is requested.

    Mirrors ``notify_board_finished`` — same relay ``/api/push/send`` endpoint
    and the same device registry — so an approval request reaches the user's
    phone even when the app is closed. The push is informational: tapping it
    opens the app, where the live ``exec.approval.requested`` event drives the
    in-app approve/deny UI. Never blocks the approval wait.
    """
    try:
        from flowly.push import relay_push

        command = (
            getattr(getattr(pending, "request", None), "command", "") or ""
        ).strip()
        await relay_push.notify_devices(
            "Approval required",
            (command or "A command needs your approval")[:140],
            data={
                "type": "approval",
                "id": str(getattr(pending, "id", "") or ""),
            },
        )
    except Exception as exc:  # pragma: no cover - best-effort
        logger.debug(f"[approval] push notify skipped: {exc}")
