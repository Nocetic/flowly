"""What the agent reads when it wakes up for its agenda.

Two prompts:

- the **wake-up** prompt runs inside the conversation the follow-up is about,
  as a hidden system turn: the agent sees the whole conversation, the note it
  left itself and, for a goal, the goal. It may work with its tools first and
  then either writes the message or answers ``[SILENT]``.
- the **daily review** prompt runs in an isolated session once a day: the agent
  looks over its goals and the owner's recent conversations and plans the
  day's follow-ups. It cannot write to the owner from there.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from flowly.agenda.timeparse import describe
from flowly.cron.service import SILENT_MARKER

_LOG_LINES = 5


def _ago(ms: int | None, now_ms: int) -> str:
    if not ms:
        return "never"
    minutes = max(0, (now_ms - int(ms)) // 60_000)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} days ago"


def goal_block(goal: dict[str, Any], zone: dt.tzinfo, *, next_check: int | None = None) -> str:
    lines = [f"Goal {goal['id']}: {goal['title']} ({goal.get('category', 'other')}, {goal.get('status')})"]
    if goal.get("why"):
        lines.append(f"Why: {goal['why']}")
    if goal.get("stage"):
        lines.append(f"Stage: {goal['stage']}")
    progress = goal.get("progress") or {}
    if progress.get("target") is not None or progress.get("current") is not None:
        unit = f" {progress['unit']}" if progress.get("unit") else ""
        lines.append(f"Progress: {progress.get('current') if progress.get('current') is not None else '?'}"
                     f" / {progress.get('target') if progress.get('target') is not None else '?'}{unit}")
    if goal.get("cadence"):
        lines.append(f"Check-in rhythm: {goal['cadence']}")
    if goal.get("endsAt"):
        lines.append(f"Ends: {describe(int(goal['endsAt']), zone)}")
    if next_check:
        lines.append(f"Next check-in: {describe(int(next_check), zone)}")
    plan = goal.get("plan") or []
    if plan:
        lines.append("Plan:")
        lines.extend(f"  {n}. [{'x' if step.get('done') else ' '}] {step.get('text')}" for n, step in enumerate(plan, 1))
    log = (goal.get("log") or [])[-_LOG_LINES:]
    if log:
        lines.append("Recent log:")
        lines.extend(
            f"  - {describe(int(entry['at']), zone)} ({entry.get('kind')}"
            f"{', ' + entry['event'] if entry.get('event') else ''}): {entry.get('text')}"
            for entry in log
        )
    return "\n".join(lines)


def wake_prompt(
    item: dict[str, Any],
    *,
    goal: dict[str, Any] | None,
    conversation: dict[str, Any],
    now_ms: int,
    zone: dt.tzinfo,
    agent_name: str,
) -> str:
    due = int(item.get("dueAt") or now_ms)
    late_minutes = max(0, (now_ms - due) // 60_000)
    parts = [
        "[Agenda] You planned to look at this again now. The owner did not just write; "
        "this is your own follow-up, and nobody is waiting for a reply.",
        f"Planned for: {describe(due, zone)}"
        + (f" (you are {late_minutes // 60} h {late_minutes % 60} min late; the computer may have been asleep)"
           if late_minutes >= 30 else ""),
        f"Your note: {item.get('note')}",
    ]
    if goal is not None:
        parts.append(goal_block(goal, zone))
    unanswered = int(conversation.get("unanswered") or 0)
    parts.append(
        f"Owner last wrote in this conversation: {_ago(conversation.get('lastOwnerAt'), now_ms)}. "
        + (f"{unanswered} message(s) you wrote first here are still unanswered — be lighter, not louder."
           if unanswered else "")
    )
    parts.append(
        "First, if it makes the message worth reading, do the work with your tools "
        "(look something up, check the calendar, compare options).\n"
        "Then decide:\n"
        f"- If there is something genuinely useful to tell the owner now, as {agent_name}, "
        "reply with the message itself: one short message in the owner's language, written the way you "
        "write in this conversation. It is sent as your message, with a notification on their phone.\n"
        f"- If not (nothing new, they already handled it, it would only nag), reply with exactly {SILENT_MARKER}. "
        "Nothing is sent.\n"
        "Bring something concrete; never ask where they have been or make them feel behind. Do not ask a "
        "clarifying question through a tool — just write."
    )
    if goal is not None:
        parts.append(
            "For the goal: record what you found with objective(action=\"log\"), adjust the plan or stage "
            "if it should change, and plan the next check-in with objective(action=\"update\", next_check=...) "
            "following its rhythm — or close it with objective(action=\"close\") when it is done or over."
        )
    return "\n\n".join(parts)


def review_prompt(
    *,
    now_ms: int,
    zone: dt.tzinfo,
    settings: dict[str, Any],
    goals: list[dict[str, Any]],
    next_checks: dict[str, int],
    pending: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    stats: dict[str, dict[str, Any]],
    last_owner_at: int | None,
    remaining_today: int,
) -> str:
    parts = [
        f"[Daily review] {describe(now_ms, zone)}. Plan today's follow-ups for your owner.",
        f"Proactivity: {settings.get('level')}. Quiet hours {settings.get('quietStart')}–{settings.get('quietEnd')}. "
        f"You may still write first {remaining_today} time(s) today.",
        f"The owner last wrote to you: {_ago(last_owner_at, now_ms)}.",
    ]
    if goals:
        parts.append("Goals you track:\n\n" + "\n\n".join(
            goal_block(goal, zone, next_check=next_checks.get(goal["id"])) for goal in goals
        ))
    else:
        parts.append("You track no goals right now.")
    if pending:
        parts.append("Already planned:\n" + "\n".join(
            f"- {item['id']} {describe(int(item['dueAt']), zone)} in {item['sessionKey']}: {item['note']}"
            for item in pending
        ))
    if conversations:
        rows = []
        for convo in conversations:
            key = convo["key"]
            info = stats.get(key) or {}
            rows.append(
                f"- {key} \"{convo.get('title') or 'untitled'}\" — active {convo.get('updated_at') or '?'}; "
                f"owner last wrote {_ago(info.get('lastOwnerAt'), now_ms)}; "
                f"unanswered first messages: {int(info.get('unanswered') or 0)}"
            )
        parts.append("The owner's recent conversations (read one with session_search when needed):\n" + "\n".join(rows))
    parts.append(
        "Decide what is worth following up today:\n"
        "- each active goal without a next check-in: plan one with objective(action=\"update\", next_check=...);\n"
        "- a thread the owner left unfinished (a decision, a purchase, a plan) where you can bring something "
        "useful: follow_up(action=\"schedule\", conversation=<key>, when=..., note=...);\n"
        "- if the owner has not written for a day or more and you have something concrete to offer, one light "
        "check-in in the conversation it belongs to.\n"
        f"Plan at most {remaining_today} message(s); choose times outside quiet hours that suit the owner. "
        "Plan nothing when nothing is worth it. You cannot write to the owner from here, and they never see "
        "this review. Finish with one line saying what you planned."
    )
    return "\n\n".join(parts)
