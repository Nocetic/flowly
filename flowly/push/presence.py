"""Where the owner is, so the phone rings only when it is the right place.

A Flowly Desktop connected to this agent reports, every half minute, while its
user is at that computer (or in a voice call there):

- which kinds of notification it shows there itself (its own notification
  settings decide). While such a report is fresh,
  :func:`flowly.push.notifications.deliver` does not also ring the phone for
  those kinds: the owner sees the result where they are working.
- which conversations are on its screen, and whether a voice call is on. A
  request the agent waits on (an approval, a question, a plan) in one of those
  conversations, or during the call, can be answered right there, so its push
  waits a minute (:mod:`flowly.push.approval_push`). Any other request is
  pushed at once: the owner is not looking at it on a computer, and may be on
  the phone that asked.

Fail-safe in every direction: a report expires after its TTL, so a Desktop
that quits, sleeps or loses its connection stops holding the phone back on its
own; a Desktop that reports nothing (an older version, or no settings known
yet) holds nothing back. See ``docs/engineering/notification-policy.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: The kinds a Desktop may say it shows itself. Nothing else is ever held.
HOLDABLE_KINDS = frozenset({"chat", "cron", "board", "flowlet"})
MIN_TTL_SECONDS = 15.0
MAX_TTL_SECONDS = 300.0
#: More reporting computers than this is not a person; the oldest is dropped.
MAX_SOURCES = 16
#: A screen shows a handful of conversations, not hundreds.
MAX_WATCHED_SESSIONS = 16
_MAX_SOURCE_LENGTH = 128
_MAX_SESSION_LENGTH = 512


@dataclass(frozen=True)
class _Report:
    expires_at: float
    kinds: frozenset[str]
    watching: frozenset[str]
    in_call: bool


_reports: dict[str, _Report] = {}


def _strings(value: object, limit: int, length: int) -> list[str]:
    items = value if isinstance(value, (list, tuple, set, frozenset)) else []
    return [item[:length] for item in items if isinstance(item, str) and item.strip()][:limit]


def report(
    source: str,
    present: bool,
    kinds: object,
    ttl_seconds: object,
    watching: object = None,
    in_call: object = False,
) -> None:
    """Record what ``source`` (one Desktop) says about its user right now.

    ``present`` False, or nothing to hold back (no known kind, no conversation
    on screen, no call), withdraws the source's report.
    """
    source = str(source or "").strip()[:_MAX_SOURCE_LENGTH]
    if not source:
        raise ValueError("source required")
    held = frozenset(kind for kind in _strings(kinds, len(HOLDABLE_KINDS), 32) if kind in HOLDABLE_KINDS)
    watched = frozenset(_strings(watching, MAX_WATCHED_SESSIONS, _MAX_SESSION_LENGTH))
    calling = in_call is True
    if not present or not (held or watched or calling):
        _reports.pop(source, None)
        return
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError):
        ttl = MIN_TTL_SECONDS
    if ttl != ttl:  # NaN
        ttl = MIN_TTL_SECONDS
    ttl = min(MAX_TTL_SECONDS, max(MIN_TTL_SECONDS, ttl))
    _reports.pop(source, None)
    _reports[source] = _Report(time.monotonic() + ttl, held, watched, calling)
    while len(_reports) > MAX_SOURCES:
        _reports.pop(next(iter(_reports)))


def _fresh() -> list[_Report]:
    now = time.monotonic()
    for source in [s for s, r in _reports.items() if r.expires_at <= now]:
        _reports.pop(source, None)
    return list(_reports.values())


def shown_at_computer(kind: str) -> bool:
    """True while a fresh report says the owner sees ``kind`` on a computer."""
    if kind not in HOLDABLE_KINDS:
        return False
    return any(kind in r.kinds for r in _fresh())


def owner_watching(session_key: object) -> bool:
    """True while the owner can answer this conversation's request on a
    computer: it is on a Desktop's screen, or a voice call is on there."""
    key = str(session_key or "").strip()
    return any(r.in_call or (key and key in r.watching) for r in _fresh())


def _reset_for_tests() -> None:
    _reports.clear()
