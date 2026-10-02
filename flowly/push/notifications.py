"""The one way Core pushes to phones.

Every phone notification is a :class:`Notice` with a stable event key. This
module sends each event at most once, adds the key to the payload so the relay
can collapse repeats on the device (``eventKey``), and lets a request that can
be answered elsewhere wait and be cancelled (:func:`schedule`, :func:`cancel`).
See ``docs/engineering/notification-policy.md``.

Every title and body is cleaned and redacted here (:mod:`flowly.push.display`)
whatever the sender did, and the owner's ``notifications.preview`` setting is
applied here: ``minimal`` replaces the content with the agent's name and what
kind of thing happened.

Nothing here may sit on a decision path: callers either await :func:`deliver`
from a background task or use :func:`schedule`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from loguru import logger

#: An event key seen within this window is not pushed again.
DEDUPE_SECONDS = 600
#: How long an approval waits on screens before it goes to the phone.
APPROVAL_PUSH_DELAY_SECONDS = 60

_MAX_KEY = 200
TITLE_LIMIT = 80
#: APNs and FCM bodies are cut at 200 by the relay; a lock screen shows ~4 lines.
BODY_LIMIT = 180

#: What a ``minimal`` preview says for each kind, under the agent's name.
MINIMAL_BODY = {
    "approval": "Needs your OK to continue. Open Flowly to review it.",
    "clarify": "Has a question for you. Open Flowly to reply.",
    "plan": "Has a plan ready for your OK. Open Flowly to review it.",
    "cron": "A scheduled task finished. Open Flowly to see it.",
    "board": "A Board task finished. Open Flowly to see it.",
    "flowlet": "You have a reminder. Open Flowly to see it.",
    "chat": "Sent you a message.",
}
_MINIMAL_FALLBACK = "Open Flowly to see it."
#: Payload fields that repeat content rather than route the tap. The payload
#: travels with the notification, so a ``minimal`` preview leaves them out.
_CONTENT_DATA_KEYS = ("jobName",)
_sent: dict[str, float] = {}
_scheduled: dict[str, asyncio.Task[None]] = {}


@dataclass(frozen=True)
class Notice:
    kind: str
    key: str
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)
    conversation_id: str = ""


def event_key(kind: str, *parts: object) -> str:
    """A stable key for one event, e.g. ``approval:<id>``, ``cron:<job>:<run>``."""
    return ":".join([kind, *(str(part) for part in parts if part not in (None, ""))])[:_MAX_KEY]


def unique_key(kind: str, *parts: object) -> str:
    """A key for something that may legitimately repeat (a recurring reminder)."""
    return event_key(kind, *parts, uuid.uuid4().hex)


def agent_name() -> str:
    """The name the owner reads for this bot: the title of a notification
    that reads as a message from it."""
    try:
        from flowly.profile import current_profile_display_name

        return current_profile_display_name() or "Flowly"
    except Exception:
        return "Flowly"


def preview_mode() -> str:
    """``full`` or ``minimal``, from the owner's config; ``full`` when unsure."""
    try:
        from flowly.config.loader import load_config

        return "minimal" if load_config().notifications.preview == "minimal" else "full"
    except Exception:
        return "full"


def rendered(notice: Notice) -> tuple[str, str]:
    """The title and body that actually leave the machine."""
    from flowly.push.display import safe_text

    if preview_mode() == "minimal":
        title, body = agent_name(), MINIMAL_BODY.get(notice.kind, _MINIMAL_FALLBACK)
    else:
        title, body = notice.title, notice.body
    return (
        safe_text(title, TITLE_LIMIT) or "Flowly",
        safe_text(body, BODY_LIMIT) or MINIMAL_BODY.get(notice.kind, _MINIMAL_FALLBACK),
    )


def _claim(key: str) -> bool:
    now = time.monotonic()
    for old in [k for k, at in _sent.items() if now - at > DEDUPE_SECONDS]:
        _sent.pop(old, None)
    if key in _sent:
        return False
    _sent[key] = now
    return True


async def deliver(notice: Notice) -> bool:
    """Push ``notice`` now unless its event was already pushed, or the owner
    is at a computer that shows it (:mod:`flowly.push.presence`). Never raises."""
    if not notice.key or not _claim(notice.key):
        return False
    try:
        from flowly.push import presence

        if presence.shown_at_computer(notice.kind):
            # Claimed all the same: this event was delivered, on the computer.
            logger.info(f"[push] {notice.kind} shown on the computer; phone not rung")
            return False
    except Exception as exc:  # pragma: no cover - never lose a notification to this check
        logger.debug(f"[push] presence check skipped: {exc}")
    try:
        from flowly.push import relay_push

        title, body = rendered(notice)
        data = dict(notice.data)
        if preview_mode() == "minimal":
            for key in _CONTENT_DATA_KEYS:
                data.pop(key, None)
        await relay_push.notify_devices(
            title,
            body,
            conversation_id=notice.conversation_id,
            data={"type": notice.kind, **data, "eventKey": notice.key},
        )
    except Exception as exc:  # pragma: no cover - best-effort
        logger.debug(f"[push] {notice.kind} notify skipped: {exc}")
    return True


def schedule(notice: Notice, delay: float) -> bool:
    """Push ``notice`` after ``delay`` seconds unless :func:`cancel` comes first."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if notice.key in _scheduled or notice.key in _sent:
        return False

    async def later() -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
        finally:
            if _scheduled.get(notice.key) is task:
                _scheduled.pop(notice.key, None)
        await deliver(notice)

    task = loop.create_task(later(), name=f"push:{notice.key}")
    _scheduled[notice.key] = task
    return True


def cancel(key: str) -> bool:
    """Drop a scheduled push that has not gone out. True if one was waiting."""
    task = _scheduled.pop(key, None)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def _reset_for_tests() -> None:
    for task in _scheduled.values():
        task.cancel()
    _scheduled.clear()
    _sent.clear()
