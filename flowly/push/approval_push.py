"""Approval push notifications.

An approval is pushed to the phone only if it is still waiting
:data:`~flowly.push.notifications.APPROVAL_PUSH_DELAY_SECONDS` after it was
asked: at the computer, in a voice call or on the strip it is answered first
and the phone stays silent. The notification says that something needs a
decision, never what: a command can hold a secret, a path or a recipient, and
the text passes through Apple's and Google's push services and the lock
screen. Tapping it opens the app, where the live ``exec.approval.requested``
event drives the decision. See ``docs/engineering/notification-policy.md``.
"""

from __future__ import annotations

from typing import Any

from flowly.push import notifications

TITLE = "Approval needed"
COMMAND_BODY = "Flowly wants to run a command. Open Flowly to review it."
ACTION_BODY = "Flowly wants to take an action. Open Flowly to review it."


def approval_notice(pending: Any) -> notifications.Notice:
    approval_id = str(getattr(pending, "id", "") or "")
    kind = str(getattr(pending, "kind", "") or "action")
    return notifications.Notice(
        kind="approval",
        key=notifications.event_key("approval", approval_id),
        title=TITLE,
        body=COMMAND_BODY if kind in ("exec", "codex") else ACTION_BODY,
        data={"id": approval_id},
    )


async def notify_approval_requested(pending: Any) -> None:
    """Push the approval now (for a caller that has already waited)."""
    await notifications.deliver(approval_notice(pending))


def schedule_approval_push(pending: Any) -> None:
    """Push the approval later unless it is settled first. Never blocks."""
    notifications.schedule(approval_notice(pending), notifications.APPROVAL_PUSH_DELAY_SECONDS)


def cancel_approval_push(approval_id: str) -> None:
    """The approval was settled: a push that has not gone out never will."""
    notifications.cancel(notifications.event_key("approval", approval_id))
