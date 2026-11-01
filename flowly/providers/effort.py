"""Reasoning effort: how hard a model thinks on an agent's turns.

One agent setting (``agents.defaults.reasoningEffort``, empty = the model's
default) reaches every provider that can take it:

* ChatGPT subscription (``openai_codex``): ``reasoning.effort``; the levels
  each model takes come from the live catalogue.
* Flowly credit and OpenRouter: ``reasoning: {"effort": …}``.
* Anthropic: ``output_config: {"effort": …}``; the levels follow the model
  family (``anthropic_efforts``).

The free Flowly plan goes up to ``medium`` (``FREE_PLAN_EFFORT_CAP``); the
proxy enforces the same cap, Core only shows and applies it. A user's own
subscription or key is never capped.
"""

from __future__ import annotations

import re

#: Every level any provider takes, lowest first.
EFFORT_ORDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

#: The highest level the free Flowly plan's credit runs at.
FREE_PLAN_EFFORT_CAP = "medium"

#: Levels the apps warn about: they spend credit or a subscription's limits fastest.
COSTLY_EFFORTS: frozenset[str] = frozenset({"xhigh", "max", "ultra"})

#: OpenRouter normalises effort across its models; it takes these three on any
#: model whose catalogue row lists the ``reasoning`` parameter.
OPENROUTER_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
OPENROUTER_DEFAULT_EFFORT = "medium"


def normalize_effort(value: object) -> str:
    """A known level in lower case, or ``""`` for anything else (unset)."""
    if not isinstance(value, str):
        return ""
    effort = value.strip().lower()
    return effort if effort in EFFORT_ORDER else ""


def ordered_efforts(levels: object) -> list[str]:
    """The known levels in ``levels`` (strings or ``{"effort": …}`` rows),
    lowest first, without repeats. Unknown levels are dropped."""
    if not isinstance(levels, (list, tuple)):
        return []
    found: set[str] = set()
    for level in levels:
        raw = level.get("effort") if isinstance(level, dict) else level
        effort = normalize_effort(raw)
        if effort:
            found.add(effort)
    return [effort for effort in EFFORT_ORDER if effort in found]


def effort_rank(effort: str) -> int:
    """Position in ``EFFORT_ORDER``; -1 for an unknown level."""
    try:
        return EFFORT_ORDER.index(effort)
    except ValueError:
        return -1


def above_cap(effort: str, cap: str | None) -> bool:
    """True when ``effort`` is higher than ``cap`` (no cap: never)."""
    if not cap or not effort:
        return False
    return effort_rank(effort) > effort_rank(cap)


def capped(effort: str, cap: str | None) -> str:
    """``effort`` lowered to ``cap`` when it is higher."""
    return cap if cap and above_cap(effort, cap) else effort


# Anthropic's Messages API (``output_config.effort``), by model family, as
# documented in 2026-10. Families not listed take no effort: the API answers
# 400 (Sonnet 4.5, Haiku 4.5 and older).
_ANTHROPIC_FULL = ("low", "medium", "high", "xhigh", "max")
_ANTHROPIC_FAMILIES: tuple[tuple[re.Pattern[str], tuple[str, ...], str], ...] = (
    (re.compile(r"opus-5[.-]5\b"), _ANTHROPIC_FULL, "medium"),
    (re.compile(r"haiku-5[.-]5\b"), _ANTHROPIC_FULL, "medium"),
    (re.compile(r"(opus|sonnet|fable|mythos)-5\b"), _ANTHROPIC_FULL, "high"),
    (re.compile(r"(sonnet|fable|mythos)-5[.-]\d\b"), _ANTHROPIC_FULL, "high"),
    (re.compile(r"opus-4[.-][78]\b"), _ANTHROPIC_FULL, "high"),
    (re.compile(r"(opus|sonnet)-4[.-]6\b"), ("low", "medium", "high", "max"), "high"),
    (re.compile(r"opus-4[.-]5\b"), ("low", "medium", "high"), "high"),
)


def anthropic_efforts(model: str) -> tuple[list[str], str | None]:
    """The levels an Anthropic model takes and its default; ``([], None)``
    for a model that takes none. ``model`` may carry a provider prefix
    (``anthropic/claude-opus-4.7``) and a dated suffix."""
    low = (model or "").strip().lower()
    for pattern, levels, default in _ANTHROPIC_FAMILIES:
        if pattern.search(low):
            return list(levels), default
    return [], None
