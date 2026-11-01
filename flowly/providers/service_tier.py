"""Service tier: how fast a model answers an agent (model-settings.md).

One agent setting (``agents.defaults.serviceTier``, empty = the provider's
standard speed). Today only the ChatGPT subscription (``openai_codex``) takes
one: its live catalogue lists, per model, the tiers it offers
(``service_tiers: [{"id": "priority", "name": "Fast", …}]``) and the request
carries the id as ``service_tier``. Faster tiers spend the subscription's
limits faster; the apps say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A tier id as a catalogue names it ("priority", "ultrafast").
_TIER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class ServiceTier:
    """One tier a model offers, as its catalogue describes it."""

    id: str
    name: str
    description: str = ""


def normalize_service_tier(value: object) -> str:
    """A tier id in lower case, or ``""`` for anything else (standard)."""
    if not isinstance(value, str):
        return ""
    tier = value.strip().lower()
    return tier if _TIER_ID.match(tier) else ""


def parse_service_tiers(raw: object) -> list[ServiceTier]:
    """The tiers in a catalogue row's ``service_tiers``, in its order, once
    each. Rows that are not ``{"id": …}`` objects with a valid id are dropped."""
    if not isinstance(raw, list):
        return []
    tiers: list[ServiceTier] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        tier = normalize_service_tier(item.get("id"))
        if not tier or tier in seen:
            continue
        seen.add(tier)
        name = item.get("name")
        description = item.get("description")
        tiers.append(ServiceTier(
            id=tier,
            name=name.strip()[:40] if isinstance(name, str) and name.strip() else tier,
            description=description.strip()[:140] if isinstance(description, str) else "",
        ))
    return tiers
