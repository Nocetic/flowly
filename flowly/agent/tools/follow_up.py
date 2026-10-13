"""The agent's own agenda: look at something again later, in a conversation.

``follow_up`` is how an agent keeps a thread alive without being asked: the
owner left a decision half made, a purchase half researched, a plan to try
something tomorrow. The agent leaves itself a note for a time; when it comes,
it wakes inside that conversation (:mod:`flowly.agenda.service`), may do the
work first, and then writes to the owner or stays silent. Whether it may
write at all is decided in code (:mod:`flowly.agenda.policy`), not here.

A goal's check-ins are planned through the ``objective`` tool instead, so a
goal never has two.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.objectives.store import ObjectiveError


def calling_conversation() -> str:
    """The conversation this tool call runs in, as the runtime bound it (never
    a model-supplied argument)."""
    from flowly.agent.tool_context import current_tool_origin

    origin = current_tool_origin()
    return origin.session_key if origin else ""


def resolve_conversation(requested: Any) -> str:
    """The calling conversation, or ``requested`` where the caller has none of
    the owner's (the daily review). A call from an owner conversation always
    targets that conversation: a chat cannot plan messages into another one."""
    from flowly.agenda.store import is_owner_conversation

    current = calling_conversation()
    if is_owner_conversation(current):
        return current
    key = str(requested or "").strip()
    if not is_owner_conversation(key):
        raise ObjectiveError(
            "INVALID", "Name the owner's conversation this belongs to (conversation=<session key>)."
        )
    return key


def owner_now() -> tuple[dt.datetime, dt.tzinfo]:
    from flowly.agenda.store import get_agenda_store
    from flowly.agenda.timeparse import owner_zone

    zone = owner_zone(get_agenda_store().settings().get("timezone"))
    return dt.datetime.now(zone), zone


def timing_note(due_ms: int, zone: dt.tzinfo) -> str:
    """What the owner should hear about a time inside their quiet hours, so the
    agent never promises a minute the policy will not keep."""
    from flowly.agenda import policy
    from flowly.agenda.store import get_agenda_store

    settings = get_agenda_store().settings()
    start = settings.get("quietStart") or policy.DEFAULT_QUIET_START
    end = settings.get("quietEnd") or policy.DEFAULT_QUIET_END
    if policy.quiet_until(due_ms, zone, start, end) is None:
        return ""
    return (
        f" That is inside the owner's quiet hours ({start}-{end}): it happens on time only if they "
        f"are still writing to you then, otherwise at {end}. Say so when you tell them the time."
    )


class FollowUpTool(Tool):
    @property
    def name(self) -> str:
        return "follow_up"

    @property
    def description(self) -> str:
        return (
            "Plan to look at something again later in this conversation, on your own initiative. "
            "Use it when the owner leaves something unfinished you can move forward later — a "
            "decision, a purchase they were researching, something they said they would try — and "
            "when you promise to come back to something. At that time you wake up here, may do the "
            "work first (look things up, compare), and then write to them or stay silent if nothing "
            "is worth saying. The owner's limits (quiet hours, how often you may write first, "
            "unanswered messages) are enforced for you. Follow-ups are cancelled automatically when "
            "the owner writes in this conversation before they are due. For a tracked goal's "
            "check-ins use the objective tool instead. Not a reminder the owner asked for: use cron "
            "for those. Actions: schedule, list, cancel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["schedule", "list", "cancel"]},
                "when": {
                    "type": "string",
                    "description": "When to look again: '+2h', '+1d', '20:00', 'tomorrow 09:00', or an ISO date-time. Read in the owner's timezone.",
                },
                "note": {
                    "type": "string",
                    "description": "What to look at and why, for your future self: enough to act without rereading everything.",
                },
                "keep_if_owner_replies": {
                    "type": "boolean",
                    "description": "Keep it even if the owner writes here first (default false: their reply usually settles it).",
                },
                "conversation": {
                    "type": "string",
                    "description": "Only in the daily review: the owner's conversation (session key) this belongs to.",
                },
                "id": {"type": "string", "description": "For cancel: the follow-up's id."},
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "list", **kwargs: Any) -> str:
        from flowly.agenda.store import get_agenda_store
        from flowly.agenda.timeparse import WhenError, describe, parse_when

        store = get_agenda_store()
        try:
            if action == "schedule":
                key = resolve_conversation(kwargs.get("conversation"))
                now, zone = owner_now()
                due = parse_when(kwargs.get("when"), now=now)
                item = store.schedule(
                    kind="follow_up",
                    due_at=due,
                    note=kwargs.get("note"),
                    session_key=key,
                    cancel_on_reply=not bool(kwargs.get("keep_if_owner_replies")),
                )
                return (
                    f"Follow-up {item['id']} planned for {describe(due, zone)} in this conversation. "
                    "You will wake up here then; the owner is not told now unless you tell them."
                    + timing_note(due, zone)
                )
            if action == "list":
                _now, zone = owner_now()
                current = calling_conversation()
                items = store.pending()
                return json.dumps([
                    {
                        "id": i["id"],
                        "kind": i["kind"],
                        "when": describe(int(i["dueAt"]), zone),
                        "note": i["note"],
                        "conversation": i["sessionKey"],
                        "here": i["sessionKey"] == current,
                        **({"goal": i["objectiveId"]} if i.get("objectiveId") else {}),
                    }
                    for i in items
                ], ensure_ascii=False)
            if action == "cancel":
                item_id = str(kwargs.get("id") or "").strip()
                if not item_id:
                    return "Error: id is required to cancel a follow-up."
                return f"Cancelled {item_id}." if store.cancel(item_id, reason="agent") else f"No pending follow-up {item_id}."
        except (ObjectiveError, WhenError) as exc:
            return f"Error: {exc}"
        return f"Error: unknown action {action!r}."
