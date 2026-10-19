"""When the agent asked to be woken, as an absolute time.

The agent writes times the way people do ("tomorrow 09:00", "+2h", "20:00",
an ISO date). They are read in the owner's timezone (the agenda setting, or
the agent computer's zone), never as naive host-local time, so a follow-up
for "09:00" lands at nine where the owner is.
"""

from __future__ import annotations

import datetime as dt
import re

#: Earliest a wake-up may be: an instant one is a reply, not a follow-up.
MIN_LEAD_MS = 60_000
#: Latest a wake-up may be planned; further out belongs to a goal's plan.
MAX_LEAD_MS = 90 * 24 * 60 * 60 * 1000

_RELATIVE = re.compile(r"^\+\s*(\d{1,4})\s*([mhdw])$")
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


class WhenError(ValueError):
    pass


def _clock(text: str) -> tuple[int, int]:
    match = _CLOCK.match(text.strip())
    if not match:
        raise WhenError(f"Unreadable time of day: {text!r} (use HH:MM)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise WhenError(f"Unreadable time of day: {text!r} (use HH:MM)")
    return hour, minute


def _at(day: dt.date, hour: int, minute: int, zone: dt.tzinfo) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)


def parse_when(value: object, *, now: dt.datetime) -> int:
    """Epoch milliseconds for ``value``, read in ``now``'s timezone.

    Accepts ``+30m`` / ``+2h`` / ``+1d`` / ``+1w``, ``HH:MM`` (the next one),
    ``today HH:MM``, ``tomorrow [HH:MM]`` (09:00 when no time is given), an
    ISO date or datetime (with or without an offset), or epoch milliseconds.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = now.tzinfo
    now_ms = int(now.timestamp() * 1000)

    if isinstance(value, bool):
        raise WhenError("A time is required")
    if isinstance(value, (int, float)):
        target = int(value)
    elif isinstance(value, str) and value.strip():
        text = value.strip().lower()
        relative = _RELATIVE.match(text)
        if relative:
            target = now_ms + int(relative.group(1)) * _UNIT_MS[relative.group(2)]
        elif text.startswith("tomorrow"):
            rest = text[len("tomorrow"):].strip()
            hour, minute = _clock(rest) if rest else (9, 0)
            target = int(_at(now.date() + dt.timedelta(days=1), hour, minute, zone).timestamp() * 1000)
        elif text.startswith("today"):
            hour, minute = _clock(text[len("today"):].strip())
            target = int(_at(now.date(), hour, minute, zone).timestamp() * 1000)
        elif _CLOCK.match(text):
            hour, minute = _clock(text)
            candidate = _at(now.date(), hour, minute, zone)
            if candidate <= now:
                candidate = _at(now.date() + dt.timedelta(days=1), hour, minute, zone)
            target = int(candidate.timestamp() * 1000)
        else:
            target = _iso(value.strip(), zone)
    else:
        raise WhenError("A time is required")

    if target < now_ms + MIN_LEAD_MS - 1_000:
        raise WhenError("That time has already passed; pick a later one")
    if target > now_ms + MAX_LEAD_MS:
        raise WhenError("That is more than 90 days away; pick a nearer time")
    return target


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso(text: str, zone: dt.tzinfo) -> int:
    try:
        if _DATE_ONLY.match(text):
            # A day without a time means its morning, not its midnight.
            day = dt.date.fromisoformat(text)
            parsed = dt.datetime(day.year, day.month, day.day, 9, 0)
        else:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise WhenError(
            f"Unreadable time: {text!r}. Use '+2h', 'tomorrow 09:00', '20:00' or an ISO date."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return int(parsed.timestamp() * 1000)


def owner_zone(name: str | None) -> dt.tzinfo:
    """The owner's timezone: the setting when valid, else the agent computer's."""
    from flowly.cron.timezone import schedule_timezone

    if name:
        try:
            return schedule_timezone(name)
        except ValueError:
            pass
    try:
        return schedule_timezone()
    except ValueError:
        return dt.timezone.utc


def describe(ms: int, zone: dt.tzinfo) -> str:
    """``Tue 2026-10-13 20:00`` in the owner's zone, for prompts and tool replies."""
    return dt.datetime.fromtimestamp(ms / 1000, zone).strftime("%a %Y-%m-%d %H:%M")
