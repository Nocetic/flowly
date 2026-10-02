"""Push notifications for everything the agent waits on the user for.

Approvals, questions (clarify, MCP prompts) and plan reviews all pause the
agent until the user answers. Each is pushed to the phone only if it is still
waiting :data:`~flowly.push.notifications.APPROVAL_PUSH_DELAY_SECONDS` after
it was asked: at the computer, in a voice call or on the strip it is answered
first and the phone stays silent.

The notification reads as a message from the agent: its name as the title,
and what it is waiting for as the body, the question, the command, the
action or the plan, with credentials redacted (:mod:`flowly.push.display`).
A command holding characters a reader cannot see is never shown. The owner
can switch the content off (``notifications.preview: minimal``). Tapping it
opens the app, where the live event drives the answer. See
``docs/engineering/notification-policy.md``.
"""

from __future__ import annotations

from typing import Any, Callable

from flowly.push import notifications
from flowly.push.display import has_hidden_characters, safe_text

COMMAND_BODY = "Needs your OK to run: {command}"
HIDDEN_COMMAND_BODY = (
    "Needs your OK to run a command with hidden characters. Open Flowly to review it."
)
ACTION_BODY = "Needs your OK: {action}"
EMPTY_BODY = "Needs your OK to continue. Open Flowly to review it."
QUESTION_FALLBACK = "Has a question for you. Open Flowly to reply."
PLAN_BODY = "Plan ready for your OK: {title}"
PLAN_FALLBACK = "Has a plan ready for your OK. Open Flowly to review it."
#: Choices are listed after a question only while they stay short.
MAX_LISTED_CHOICES = 4
MAX_CHOICE_LENGTH = 24


def approval_body(pending: Any) -> str:
    request = getattr(pending, "request", None)
    text = str(getattr(request, "command", "") or "")
    kind = str(getattr(pending, "kind", "") or "action")
    if not text.strip():
        return EMPTY_BODY
    if kind in ("exec", "codex"):
        if has_hidden_characters(text):
            return HIDDEN_COMMAND_BODY
        return COMMAND_BODY.format(command=safe_text(text, notifications.BODY_LIMIT, command=True))
    # A tool action is a human sentence ("Send email to a@b.com") whose
    # first line says what; the rest (an email's preview) stays in the app.
    first = next((line for line in text.splitlines() if line.strip()), "")
    return ACTION_BODY.format(action=safe_text(first, notifications.BODY_LIMIT))


def approval_notice(pending: Any) -> notifications.Notice:
    approval_id = str(getattr(pending, "id", "") or "")
    return notifications.Notice(
        kind="approval",
        key=notifications.event_key("approval", approval_id),
        title=notifications.agent_name(),
        body=approval_body(pending),
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


def question_body(pending: Any) -> str:
    question = safe_text(getattr(pending, "question", ""), notifications.BODY_LIMIT)
    if not question:
        return QUESTION_FALLBACK
    choices = [str(choice).strip() for choice in (getattr(pending, "choices", None) or []) if str(choice).strip()]
    if 0 < len(choices) <= MAX_LISTED_CHOICES and all(len(choice) <= MAX_CHOICE_LENGTH for choice in choices):
        return f"{question} ({' / '.join(safe_text(choice, MAX_CHOICE_LENGTH) for choice in choices)})"
    return question


def question_notice(pending: Any) -> notifications.Notice:
    question_id = str(getattr(pending, "id", "") or "")
    return notifications.Notice(
        kind="clarify",
        key=notifications.event_key("clarify", question_id),
        title=notifications.agent_name(),
        body=question_body(pending),
        data={"id": question_id},
    )


def _stored_plan(plan_id: str) -> Any:
    from flowly.plans.store import get_plan_store

    return get_plan_store().get(plan_id)


def plan_body(plan: Any) -> str:
    title = safe_text(getattr(plan, "title", "") or getattr(plan, "goal", ""), notifications.BODY_LIMIT)
    if not title:
        return PLAN_FALLBACK
    steps = len(getattr(plan, "steps", None) or [])
    return PLAN_BODY.format(title=title) + (f" ({steps} step{'s' if steps != 1 else ''})" if steps else "")


def plan_notice(approval: Any, plan_id: str, lookup: Callable[[str], Any] = _stored_plan) -> notifications.Notice:
    approval_id = str(getattr(approval, "id", "") or "")
    try:
        plan = lookup(str(plan_id or ""))
    except Exception:
        plan = None
    return notifications.Notice(
        kind="plan",
        key=notifications.event_key("plan", approval_id),
        title=notifications.agent_name(),
        body=plan_body(plan),
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
