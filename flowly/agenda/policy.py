"""The limits on an agent writing first. Code decides these, not the model.

A wake-up is the agent's own decision to look at something again. Whether it
may then reach the owner is decided here, before any model turn runs, from
the owner's settings and from what happened to the agent's earlier messages:

- **Proactivity level** (per agent): ``off`` never writes first; ``low``,
  ``normal`` and ``high`` allow 1, 2 and 4 messages a day.
- **Quiet hours** (owner's timezone): nothing is sent inside them; the
  wake-up waits for the window to end. Unless the owner wrote within
  :data:`OWNER_AWAKE_MS`: someone who asked at 00:54 for a check-in at 00:57
  is up, and holding it until morning breaks the promise for nothing.
- **One budget for every agent**: :data:`GLOBAL_DAILY_CAP` messages a day
  across all of the owner's agents together, so five agents do not mean five
  times the interruptions (:mod:`flowly.agenda.budget`).
- **Back-off**: each message the owner did not answer doubles the wait before
  the next one in that conversation (12 h, then 24 h); after
  :data:`MAX_UNANSWERED` unanswered messages the agent stops writing first
  there until the owner writes again.
- **Busy conversation**: while a turn is running there, the wake-up waits.

The function is pure: the service gathers the facts and acts on the result.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

LEVELS = ("off", "low", "normal", "high")
DEFAULT_LEVEL = "normal"
DAILY_CAP_BY_LEVEL = {"off": 0, "low": 1, "normal": 2, "high": 4}
#: Messages a day all of the owner's agents may send first, together.
GLOBAL_DAILY_CAP = 6
#: Unanswered messages after which an agent stops writing first in a conversation.
MAX_UNANSWERED = 3
#: Wait after one unanswered message; it doubles with each further one.
BACKOFF_BASE_MS = 12 * 60 * 60 * 1000
#: How long a wake-up waits for a running turn in its conversation.
BUSY_RETRY_MS = 3 * 60 * 1000
#: The owner wrote this recently: they are up, and quiet hours do not hold a
#: wake-up back.
OWNER_AWAKE_MS = 30 * 60 * 1000

DEFAULT_QUIET_START = "22:00"
DEFAULT_QUIET_END = "09:00"


@dataclass(frozen=True)
class Decision:
    action: str  # "run" | "defer" | "drop"
    reason: str = ""
    until_ms: int | None = None


@dataclass(frozen=True)
class Facts:
    now_ms: int
    zone: dt.tzinfo
    level: str
    quiet_start: str
    quiet_end: str
    #: Delivered messages in this conversation the owner has not answered.
    unanswered: int
    #: When the agent last wrote first in this conversation (ms), if ever.
    last_proactive_ms: int | None
    sent_today_by_agent: int
    sent_today_by_all: int
    session_busy: bool
    #: For a goal's check-in: the goal's status; None when not about a goal.
    objective_status: str | None = None
    is_objective: bool = False
    #: When the owner last wrote to this agent, in any conversation (ms).
    owner_last_active_ms: int | None = None


def owner_awake(facts: Facts) -> bool:
    last = facts.owner_last_active_ms
    return last is not None and 0 <= facts.now_ms - last <= OWNER_AWAKE_MS


def parse_clock(text: str, fallback: str) -> tuple[int, int]:
    for candidate in (text, fallback):
        try:
            hour, minute = (int(part) for part in str(candidate).split(":", 1))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (TypeError, ValueError):
            continue
    return 0, 0


def quiet_until(now_ms: int, zone: dt.tzinfo, quiet_start: str, quiet_end: str) -> int | None:
    """When the quiet window ends, if ``now`` is inside it; else None."""
    now = dt.datetime.fromtimestamp(now_ms / 1000, zone)
    start_h, start_m = parse_clock(quiet_start, DEFAULT_QUIET_START)
    end_h, end_m = parse_clock(quiet_end, DEFAULT_QUIET_END)
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    minute = now.hour * 60 + now.minute
    if start == end:
        return None
    if start < end:
        inside = start <= minute < end
    else:  # overnight, e.g. 22:00 to 09:00
        inside = minute >= start or minute < end
    if not inside:
        return None
    end_day = now.date()
    if start > end and minute >= start:
        end_day = end_day + dt.timedelta(days=1)
    ends = dt.datetime(end_day.year, end_day.month, end_day.day, end_h, end_m, tzinfo=zone)
    return int(ends.timestamp() * 1000)


def next_day_start(now_ms: int, zone: dt.tzinfo, quiet_end: str) -> int:
    """Tomorrow, at the end of the owner's quiet hours."""
    now = dt.datetime.fromtimestamp(now_ms / 1000, zone)
    end_h, end_m = parse_clock(quiet_end, DEFAULT_QUIET_END)
    day = now.date() + dt.timedelta(days=1)
    return int(dt.datetime(day.year, day.month, day.day, end_h, end_m, tzinfo=zone).timestamp() * 1000)


def backoff_ms(unanswered: int) -> int:
    if unanswered <= 0:
        return 0
    return BACKOFF_BASE_MS * (2 ** (unanswered - 1))


def decide(facts: Facts) -> Decision:
    level = facts.level if facts.level in LEVELS else DEFAULT_LEVEL
    if level == "off":
        return Decision("drop", "proactivity_off")
    if facts.is_objective and facts.objective_status != "active":
        return Decision("drop", "goal_not_active")
    if facts.unanswered >= MAX_UNANSWERED:
        return Decision("drop", "owner_not_answering")

    quiet = quiet_until(facts.now_ms, facts.zone, facts.quiet_start, facts.quiet_end)
    if quiet is not None and not owner_awake(facts):
        return Decision("defer", "quiet_hours", quiet)

    if facts.unanswered > 0 and facts.last_proactive_ms is not None:
        allowed_at = facts.last_proactive_ms + backoff_ms(facts.unanswered)
        if facts.now_ms < allowed_at:
            return Decision("defer", "waiting_for_reply", allowed_at)

    if facts.sent_today_by_agent >= DAILY_CAP_BY_LEVEL[level]:
        return Decision("defer", "agent_daily_limit", next_day_start(facts.now_ms, facts.zone, facts.quiet_end))
    if facts.sent_today_by_all >= GLOBAL_DAILY_CAP:
        return Decision("defer", "daily_limit", next_day_start(facts.now_ms, facts.zone, facts.quiet_end))

    if facts.session_busy:
        return Decision("defer", "conversation_busy", facts.now_ms + BUSY_RETRY_MS)
    return Decision("run")
