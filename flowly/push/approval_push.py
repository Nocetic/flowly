"""Exec-approval push notifications."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

# Strong references: a fire-and-forget task with none can be collected mid-send.
_inflight: set[asyncio.Task[None]] = set()


async def notify_approval_requested(pending: Any) -> None:
    """Send a best-effort push when an approval is requested.

    Mirrors ``notify_board_finished`` — same relay ``/api/push/send`` endpoint
    and the same device registry — so an approval request reaches the user's
    phone even when the app is closed. The push is informational: tapping it
    opens the app, where the live ``exec.approval.requested`` event drives the
    in-app approve/deny UI. Callers on the approval path use
    :func:`schedule_approval_push`, which never blocks the approval wait.
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


def schedule_approval_push(pending: Any) -> None:
    """Send the approval push in the background.

    The push reaches phones through one relay call per registered device, which
    can take seconds; awaited on the notify path it delayed every approval
    decision by that long. Best-effort: no running loop means no push.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(notify_approval_requested(pending), name="approval-push")
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
