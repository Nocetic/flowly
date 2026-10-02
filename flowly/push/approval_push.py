"""Push notifications for everything the agent waits on the user for.

Approvals, questions (clarify, MCP prompts) and plan reviews all pause the
agent until the user answers. Each is pushed to the phone only if it is still
waiting :data:`~flowly.push.notifications.APPROVAL_PUSH_DELAY_SECONDS` after
it was asked: at the computer, in a voice call or on the strip it is answered
first and the phone stays silent. The notification says that something needs
the user, never what: a command, a question or a plan can hold a secret, a
path or a recipient, and the text passes through Apple's and Google's push
services and the lock screen. Tapping it opens the app, where the live event
drives the answer. See ``docs/engineering/notification-policy.md``.
"""

from __future__ import annotations

from typing import Any

from flowly.push import notifications

TITLE = "Approval needed"
COMMAND_BODY = "Flowly wants to run a command. Open Flowly to review it."
ACTION_BODY = "Flowly wants to take an action. Open Flowly to review it."
QUESTION_TITLE = "Question from Flowly"
QUESTION_BODY = "Flowly needs your answer to continue. Open Flowly to reply."
PLAN_TITLE = "Plan ready for review"
PLAN_BODY = "Flowly has a plan waiting for your approval. Open Flowly to review it."


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


def question_notice(pending: Any) -> notifications.Notice:
    question_id = str(getattr(pending, "id", "") or "")
    return notifications.Notice(
        kind="clarify",
        key=notifications.event_key("clarify", question_id),
        title=QUESTION_TITLE,
        body=QUESTION_BODY,
        data={"id": question_id},
    )


def plan_notice(approval: Any, plan_id: str) -> notifications.Notice:
    approval_id = str(getattr(approval, "id", "") or "")
    return notifications.Notice(
        kind="plan",
        key=notifications.event_key("plan", approval_id),
        title=PLAN_TITLE,
        body=PLAN_BODY,
        data={"id": approval_id, "planId": str(plan_id or "")},
    )


def wire_waiting_pushes(questions: Any, plans: Any) -> None:
    """Push questions and plan reviews the way approvals are pushed.

    Registered as callbacks of their own, so a surface that fails to draw the
    prompt never takes the phone notification down with it.
    """
    async def push_question(pending: Any) -> None:
        notifications.schedule(question_notice(pending), notifications.APPROVAL_PUSH_DELAY_SECONDS)

    async def retire_question(question_id: str, reason: str, session_key: str) -> None:
        notifications.cancel(notifications.event_key("clarify", question_id))

    async def push_plan(approval: Any, plan_id: str) -> None:
        notifications.schedule(plan_notice(approval, plan_id), notifications.APPROVAL_PUSH_DELAY_SECONDS)

    def retire_plan(approval_id: str, reason: str) -> None:
        notifications.cancel(notifications.event_key("plan", approval_id))

    questions.add_notify_callback(push_question)
    questions.add_close_callback(retire_question)
    plans.add_notify_callback(push_plan)
    plans.add_close_callback(retire_plan)
