"""Execution-host timezone shared by cron calculations and RPC metadata."""

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.tz import gettz


def schedule_timezone(name: str | None = None) -> dt.tzinfo:
    """Explicit IANA zone wins; otherwise use the execution host (including TZ).

    gettz() reads the OS zone rules, including DST and Windows registry zones.
    datetime.now().astimezone().tzinfo alone would freeze today's UTC offset.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid cron timezone: {name}") from exc
    zone = gettz()
    if zone is None:
        raise ValueError("Unable to determine the execution host timezone")
    return zone


def host_timezone_metadata() -> dict:
    """Expose the host clock without guessing an IANA ID from a UTC offset."""
    now = dt.datetime.now(schedule_timezone())
    candidates = []
    if 'TZ' in os.environ:
        candidates.append(os.environ['TZ'].lstrip(':'))
    else:
        try:
            path = str(Path('/etc/localtime').resolve())
            if '/zoneinfo/' in path:
                candidates.append(path.split('/zoneinfo/', 1)[1])
            candidates.append(Path('/etc/timezone').read_text().strip())
        except OSError:
            pass
    zone_id = None
    for candidate in candidates:
        try:
            ZoneInfo(candidate)
            zone_id = candidate
            break
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return {
        'id': zone_id,
        'name': now.tzname(),
        'utcOffsetSeconds': int(now.utcoffset().total_seconds()),
    }
