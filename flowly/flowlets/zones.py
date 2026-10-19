"""Which time zone a flowlet's "today" and reminder times mean.

A flowlet belongs to a person, not to the machine its core runs on: a core on
a VPS in Frankfurt must not end the user's day at a Frankfurt midnight. Clients
send their IANA zone with each flowlet call; the store remembers the latest per
flowlet, and every server-side computation — values, photo dates, reminders —
uses it. Until a device has opened the screen, the host's zone applies.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_zone(name: Any) -> ZoneInfo | None:
    """An IANA zone name from a client, or None when absent or unknown."""
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def zone_for(flowlet: dict | None, fallback: tzinfo | None = None) -> tzinfo | None:
    """The remembered device zone of ``flowlet``, else ``fallback`` (None means
    the host's local zone)."""
    zone = parse_zone((flowlet or {}).get("tz"))
    return zone if zone is not None else fallback


def adopt_client_zone(store: Any, flowlet: dict | None, params: dict) -> tzinfo | None:
    """Remember the zone a client sent (``params["tz"]``) and return the zone to
    compute with for this request."""
    zone = parse_zone(params.get("tz"))
    if zone is not None and flowlet is not None:
        store.set_tz(flowlet["id"], zone.key)
        return zone
    return zone_for(flowlet)
