"""What each conversation is waiting on from its owner.

A turn can stop and wait for the owner in four ways, each kept by its own
registry, each tied to a conversation (session key):

- ``approval``   a command or action waiting to be allowed (exec approvals)
- ``question``   the agent asked something (clarify)
- ``plan``       a proposed plan waiting to be approved
- ``connection`` a connection the agent asked for in the chat (MCP setup)

``pending_inputs`` reads the four registries at the moment it is called and
reports, per conversation, the most urgent kind, when the oldest wait began,
how many there are and, when one is safe to show in a list, a short subject:
the service a connection is for, or what a tool action wants to do. A shell
command is never a subject; it belongs on the approval card, not in a list. It is the one place clients learn "this conversation
needs you": ``sessions.list`` carries it per row and ``sessions.attention``
serves it alone. Nothing is stored; a wait that ends disappears from the next
read.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any, NotRequired, TypedDict

from loguru import logger

# Most urgent first: a blocked command or question stops the work outright;
# a plan and a connection wait for a decision the owner can take later.
KINDS: tuple[str, ...] = ("approval", "question", "plan", "connection")
# A subject is a label for a list row, not the request itself.
SUBJECT_MAX_CHARS = 80


class NeedsInput(TypedDict):
    kind: str
    since: int
    count: int
    subject: NotRequired[str]


def clean_subject(value: Any) -> str | None:
    """One line, at most ``SUBJECT_MAX_CHARS``; None when there is nothing to show."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    return text if len(text) <= SUBJECT_MAX_CHARS else text[: SUBJECT_MAX_CHARS - 1].rstrip() + "…"


def pending_inputs(connection_requests: Iterable[Mapping[str, Any]] = ()) -> dict[str, NeedsInput]:
    """``{sessionKey: {kind, since, count}}`` for every conversation waiting.

    ``connection_requests`` are the pending chat connection requests
    (``MCPChatSetup.pending()["requests"]``); the MCP runtime owns them and
    the caller passes them in, so this module does not reach into it.
    ``since`` is in milliseconds, like the rest of ``sessions.list``.
    """
    waits: dict[str, list[tuple[str, float, str | None]]] = {}

    def add(session_key: Any, kind: str, since: Any, subject: Any = None) -> None:
        if not isinstance(session_key, str) or not session_key:
            return
        at = float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else 0.0
        waits.setdefault(session_key, []).append((kind, at, clean_subject(subject)))

    try:
        from flowly.exec.approval_manager import get_approval_manager

        for approval in get_approval_manager().list_pending():
            # A tool action is already a sentence for people ("Send email to
            # …"); a shell or Codex command is not, and stays on its card.
            subject = approval.request.command if approval.kind == "action" else None
            add(approval.session_key, "approval", approval.created_at, subject)
    except Exception as exc:  # noqa: BLE001 — one registry must not hide the others
        logger.debug(f"[attention] approvals unavailable: {exc}")

    try:
        from flowly.clarify.manager import get_clarify_manager

        for question in get_clarify_manager().list_pending():
            add(question.session_key, "question", question.created_at)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[attention] questions unavailable: {exc}")

    try:
        from flowly.plans.store import get_plan_store

        store = get_plan_store()
        awaiting = {plan.sessionKey for plan in store.all_plans() if plan.status == "awaiting_approval"}
        for session_key in awaiting:
            # Only the plan a client would show: a newer draft or an executing
            # plan for the same conversation supersedes an older proposal.
            current = store.current_for_session(session_key)
            if current is not None and current.status == "awaiting_approval":
                add(session_key, "plan", current.updatedAt)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[attention] plans unavailable: {exc}")

    for request in connection_requests:
        add(request.get("sessionKey"), "connection", request.get("createdAt"), request.get("name"))

    result: dict[str, NeedsInput] = {}
    for session_key, entries in waits.items():
        # The row shows the most urgent wait; its subject is that of the
        # oldest wait of that kind.
        kind = min((kind for kind, _, _ in entries), key=KINDS.index)
        _, _, subject = min((entry for entry in entries if entry[0] == kind), key=lambda entry: entry[1])
        result[session_key] = {
            "kind": kind,
            "since": int(min(at for _, at, _ in entries) * 1000),
            "count": len(entries),
            **({"subject": subject} if subject else {}),
        }
    return result


def most_urgent(waits: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """The wait that needs the owner first, with its session key: the most
    urgent kind, then the one waiting longest. None when nothing waits."""
    candidates = [
        (KINDS.index(str(wait.get("kind"))), int(wait.get("since") or 0), key, wait)
        for key, wait in waits.items()
        if isinstance(wait, Mapping) and wait.get("kind") in KINDS
    ]
    if not candidates:
        return None
    _, _, key, wait = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    subject = clean_subject(wait.get("subject"))
    return {"sessionKey": key, "kind": wait["kind"], "since": int(wait.get("since") or 0),
            "count": sum(int(w.get("count") or 1) for _, _, _, w in candidates),
            **({"subject": subject} if subject else {})}
