"""Whether the owner is at a computer that already shows a notification.

A Flowly Desktop connected to this agent reports, every half minute, that its
user is active at that computer and which kinds of notification it shows
there itself (its own notification settings decide). While such a report is
fresh, :func:`flowly.push.notifications.deliver` does not also ring the phone
for those kinds: the owner sees the result where they are working.

Fail-safe in every direction: a report expires after its TTL, so a Desktop
that quits, sleeps or loses its connection stops holding the phone back on
its own; a Desktop that reports nothing (an older version, or no settings
known yet) holds nothing back; and only informational kinds are ever held.
Anything the agent waits on (an approval, a question, a plan) keeps its own
rule. See ``docs/engineering/notification-policy.md`` (P4).
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
_MAX_SOURCE_LENGTH = 128


@dataclass(frozen=True)
class _Report:
    expires_at: float
    kinds: frozenset[str]


_reports: dict[str, _Report] = {}


def report(source: str, present: bool, kinds: object, ttl_seconds: object) -> None:
    """Record what ``source`` (one Desktop) says about its user right now.

    ``present`` False, or no known kind, withdraws the source's report.
    """
    source = str(source or "").strip()[:_MAX_SOURCE_LENGTH]
    if not source:
        raise ValueError("source required")
    held = frozenset(
        kind for kind in (kinds if isinstance(kinds, (list, tuple, set, frozenset)) else [])
        if isinstance(kind, str) and kind in HOLDABLE_KINDS
    )
    if not present or not held:
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
    _reports[source] = _Report(time.monotonic() + ttl, held)
    while len(_reports) > MAX_SOURCES:
        _reports.pop(next(iter(_reports)))


def shown_at_computer(kind: str) -> bool:
    """True while a fresh report says the owner sees ``kind`` on a computer."""
    if kind not in HOLDABLE_KINDS:
        return False
    now = time.monotonic()
    for source in [s for s, r in _reports.items() if r.expires_at <= now]:
        _reports.pop(source, None)
    return any(kind in r.kinds for r in _reports.values())


def _reset_for_tests() -> None:
    _reports.clear()
