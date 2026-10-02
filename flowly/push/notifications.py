"""The one way Core pushes to phones.

Every phone notification is a :class:`Notice` with a stable event key. This
module sends each event at most once, adds the key to the payload so the relay
can collapse repeats on the device (``eventKey``), and lets a request that can
be answered elsewhere wait and be cancelled (:func:`schedule`, :func:`cancel`).
See ``docs/engineering/notification-policy.md``.

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


def _claim(key: str) -> bool:
    now = time.monotonic()
    for old in [k for k, at in _sent.items() if now - at > DEDUPE_SECONDS]:
        _sent.pop(old, None)
    if key in _sent:
        return False
    _sent[key] = now
    return True


async def deliver(notice: Notice) -> bool:
    """Push ``notice`` now unless its event was already pushed. Never raises."""
    if not notice.key or not _claim(notice.key):
        return False
    try:
        from flowly.push import relay_push

        await relay_push.notify_devices(
            notice.title.strip()[:80],
            notice.body.strip()[:140],
            conversation_id=notice.conversation_id,
            data={"type": notice.kind, **notice.data, "eventKey": notice.key},
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
