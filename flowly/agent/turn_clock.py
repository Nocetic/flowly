"""When a turn happens, told to the model with the turn.

The system prompt carries no clock: a timestamp there changed every turn
and kept the provider's prompt cache from reusing it. The model was told to
run ``date`` instead, and when it did not, it guessed, and wrote a wrong
date into memory ("Düzeltme (2026-10-03)" on 4 October). The clock now rides
on the turn's own message, which is never cached, and is not stored with it.
"""
from __future__ import annotations

import datetime as dt


def turn_clock(now: dt.datetime | None = None) -> str:
    """One line with the agent computer's local date, weekday, time and zone."""
    from flowly.cron.timezone import host_timezone_metadata, schedule_timezone

    try:
        zone = schedule_timezone()
        label = host_timezone_metadata().get("id")
    except ValueError:
        zone, label = dt.timezone.utc, "UTC"
    now = (now or dt.datetime.now(zone)).astimezone(zone)
    offset = now.strftime("%z")
    utc = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    where = f"{label}, {utc}" if label else utc
    return (f"<turn_time>{now:%A} {now:%Y-%m-%d} {now:%H:%M} ({where}) on the agent's computer, "
            "when this message arrived. Use this date for anything you date or record.</turn_time>")
