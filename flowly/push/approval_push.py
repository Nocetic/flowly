"""Push notifications for everything the agent waits on the user for.

Approvals, questions (clarify, MCP prompts) and plan reviews all pause the
agent until the user answers. One is pushed at once, unless the owner can
answer it on a computer right now (its conversation is on a Flowly Desktop's
screen, or a voice call is on there; see :mod:`flowly.push.presence`). Then it
is pushed only if it is still waiting
:data:`~flowly.push.notifications.APPROVAL_PUSH_DELAY_SECONDS` after it was
asked, and an answer on any surface cancels it.

The notification reads as a message from the agent: its name as the title,
and what it is waiting for as the body, the question, the command, the
action or the plan, with credentials redacted (:mod:`flowly.push.display`).
A command holding characters a reader cannot see is never shown. The owner
can switch the content off (``notifications.preview: minimal``). Tapping it
opens the conversation that asked, when the phone has it (see
:func:`phone_conversation`), where the waiting request is restored. See
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


#: Phone apps open a relay chat by its bare id and an agent-stored chat by its
#: own key; these prefixes are the agent-stored chats the phone apps start.
_PHONE_SESSION_PREFIXES = ("ios", "android")


def phone_conversation(session_key: object) -> str:
    """The conversation a phone opens for this session, or "" if it has none.

    Relay chats ("web:<id>") open by the bare id, as the relay's own chat
    pushes carry it; chats the phone apps keep in the agent's own store (a
    bare id, "ios:<id>", "android:<id>") open by their key, as the gateway's
    chat pushes carry it. A computer's session or a messaging channel
    (Telegram, a schedule) is not a conversation on the phone: the tap then
    just opens the app.
    """
    key = str(session_key or "").strip()
    if not key:
        return ""
    if key.startswith("web:"):
        return key[len("web:"):]
    prefix, separator, _rest = key.partition(":")
    if not separator:
        return key
    return key if prefix in _PHONE_SESSION_PREFIXES else ""


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
        conversation_id=phone_conversation(getattr(pending, "session_key", "")),
    )


async def notify_approval_requested(pending: Any) -> None:
    """Push the approval now (for a caller that has already waited)."""
    await notifications.deliver(approval_notice(pending))


def waiting_push_delay(session_key: object) -> float:
    """How long a waiting request holds back its push: a minute while the
    owner can answer it on a computer, no time at all otherwise."""
    from flowly.push import presence

    try:
        watching = presence.owner_watching(session_key)
    except Exception:
        watching = False
    return notifications.APPROVAL_PUSH_DELAY_SECONDS if watching else 0.0


def schedule_approval_push(pending: Any) -> None:
    """Push the approval later unless it is settled first. Never blocks."""
    notifications.schedule(approval_notice(pending), waiting_push_delay(getattr(pending, "session_key", "")))


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
        conversation_id=phone_conversation(getattr(pending, "session_key", "")),
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
        conversation_id=phone_conversation(getattr(plan, "sessionKey", "")),
    )


def wire_waiting_pushes(questions: Any, plans: Any) -> None:
    """Push questions and plan reviews the way approvals are pushed.

    Registered as callbacks of their own, so a surface that fails to draw the
    prompt never takes the phone notification down with it.
    """
    async def push_question(pending: Any) -> None:
        notifications.schedule(question_notice(pending), waiting_push_delay(getattr(pending, "session_key", "")))

    async def retire_question(question_id: str, reason: str, session_key: str) -> None:
        notifications.cancel(notifications.event_key("clarify", question_id))

    async def push_plan(approval: Any, plan_id: str) -> None:
        try:
            session_key = getattr(_stored_plan(str(plan_id or "")), "sessionKey", "")
        except Exception:
            session_key = ""
        notifications.schedule(plan_notice(approval, plan_id), waiting_push_delay(session_key))

    def retire_plan(approval_id: str, reason: str) -> None:
        notifications.cancel(notifications.event_key("plan", approval_id))

    questions.add_notify_callback(push_question)
    questions.add_close_callback(retire_question)
    plans.add_notify_callback(push_plan)
    plans.add_close_callback(retire_plan)
