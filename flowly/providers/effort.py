"""Reasoning effort: how hard a model thinks on an agent's turns.

One agent setting (``agents.defaults.reasoningEffort``, empty = the model's
default) reaches every provider that can take it:

* ChatGPT subscription (``openai_codex``): ``reasoning.effort``; the levels
  each model takes come from the live catalogue.
* Flowly credit and OpenRouter: ``reasoning: {"effort": …}``; ``none`` is
  ``reasoning: {"enabled": false}``. The levels each model takes come from
  the catalogue's ``reasoning`` object (``openrouter_efforts``).
* xAI Grok: ``reasoning_effort`` (API key) or ``reasoning.effort`` (sign-in);
  the levels follow each model's documented ones (``xai_efforts``); off is
  ``none`` where the model takes it, else its non-reasoning twin
  (``xai_request``).

A level a model does not take is lowered to the nearest one it does
(``clamp_effort``), never raised and never switched off: an agent set to
``ultra`` runs a model that stops at ``max`` at ``max``, and ``ultra`` again on
one that has it. The setting itself is kept.
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

#: What an OpenRouter row that lists the ``reasoning`` parameter but carries no
#: ``reasoning`` object (an older catalogue, or a proxy that drops it) is
#: offered. ``none`` switches thinking off (``reasoning: {"enabled": false}``);
#: a model that always thinks refuses that and is asked again without it.
OPENROUTER_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high")
OPENROUTER_DEFAULT_EFFORT = "medium"

#: Every level OpenRouter's gateway takes (its reasoning docs, 2026-11): what a
#: row whose ``reasoning.supported_efforts`` is null accepts.
OPENROUTER_GATEWAY_EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


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


def clamp_effort(effort: str, levels: list[str] | tuple[str, ...] | None) -> str:
    """``effort`` on a model that takes ``levels``: itself when it is one of
    them (or nothing is known); else the nearest weaker level, so a clamp never
    spends more; with nothing weaker, the model's weakest. An enabled ask never
    lands on ``none`` (that would switch thinking off unasked); ``none`` on a
    model that cannot switch thinking off becomes its weakest level."""
    if not effort or not levels or effort in levels:
        return effort
    rank = effort_rank(effort)
    if rank < 0:
        return effort
    candidates = [level for level in levels if level != "none" and effort_rank(level) >= 0]
    if not candidates:
        return effort
    below = [level for level in candidates if effort_rank(level) < rank]
    return max(below, key=effort_rank) if below else min(candidates, key=effort_rank)


def openrouter_efforts(row: dict) -> tuple[list[str], str | None, bool]:
    """The levels an OpenRouter (or Flowly proxy) catalogue row takes, its
    default level, and whether it thinks when no level is sent.

    The row's ``reasoning`` object is the source (OpenRouter's models API):
    ``supported_efforts`` lists the levels (null: every gateway level; absent:
    the model exposes none); ``mandatory`` means thinking cannot be switched
    off, so ``none`` is not offered; ``default_effort`` is the level it thinks
    at; ``default_enabled: false`` means it does not think until asked. A row
    without the object falls back to ``reasoning`` in ``supported_parameters``."""
    reasoning = row.get("reasoning")
    if isinstance(reasoning, dict):
        if "supported_efforts" not in reasoning:
            return [], None, True
        raw = reasoning.get("supported_efforts")
        levels = list(OPENROUTER_GATEWAY_EFFORTS) if raw is None else ordered_efforts(raw)
        if reasoning.get("mandatory") is True:
            levels = [level for level in levels if level != "none"]
        thinking = [level for level in levels if level != "none"]
        if not thinking:
            return [], None, True
        default = normalize_effort(reasoning.get("default_effort"))
        if default not in thinking:
            default = "medium" if "medium" in thinking else clamp_effort("medium", thinking)
        return levels, default, reasoning.get("default_enabled") is not False
    params = row.get("supported_parameters")
    if isinstance(params, list) and "reasoning" in params:
        return list(OPENROUTER_EFFORTS), OPENROUTER_DEFAULT_EFFORT, True
    return [], None, True


#: xAI Grok, from each model's page on docs.x.ai (2026-11-06):
#:
#: - Grok 4.6 and 4.7 take ``low`` to ``xhigh`` and think at ``high`` by default;
#: - Grok 4.5 takes ``low`` to ``high`` (``xhigh`` is read as ``high``);
#: - Grok 4.3 also takes ``none`` (thinking off); its default is ``low``;
#: - Grok 4.20 reasoning takes no level and cannot stop thinking, but has a
#:   non-reasoning twin (``…-non-reasoning``): off is that twin, when the
#:   account's catalogue lists it (model_catalog pairs them);
#: - Grok 4.20 multi-agent's ``reasoning.effort`` sets how many agents
#:   collaborate, not how hard it thinks: no levels offered;
#: - Grok Build takes none.
#:
#: A model that refuses a level anyway is asked again without it (the
#: providers remember it).
_XAI_FAMILIES: tuple[tuple[re.Pattern[str], tuple[str, ...], str], ...] = (
    (re.compile(r"(^|/)grok-4[.-]3(\b|-)"), ("none", "low", "medium", "high", "xhigh"), "low"),
    (re.compile(r"(^|/)grok-4[.-]5(\b|-)"), ("low", "medium", "high"), "high"),
    (re.compile(r"(^|/)grok-4[.-][67](\b|-)"), ("low", "medium", "high", "xhigh"), "high"),
)
#: What a reasoning model whose only control is its non-reasoning twin offers:
#: the switch (``none``) and thinking on, which sends no level.
XAI_SWITCH_LEVELS: tuple[str, ...] = ("none", "high")
_XAI_REASONING_SUFFIX = re.compile(r"-reasoning(?=$|-)")


def _xai_model(model: str) -> str:
    low = (model or "").strip().lower()
    return low if low.startswith(("grok", "x-ai/grok", "xai/grok")) else ""


def xai_efforts(model: str) -> tuple[list[str], str | None]:
    """The levels an xAI model takes and its default; ``([], None)`` for one
    that takes none (a non-reasoning or multi-agent model, Grok 4.20
    reasoning, Grok Build). A Grok 4.20 reasoning model can still be
    switched off through its twin: see :func:`xai_twin`."""
    low = _xai_model(model)
    if not low or "non-reasoning" in low or "multi-agent" in low:
        return [], None
    for pattern, levels, default in _XAI_FAMILIES:
        if pattern.search(low):
            return list(levels), default
    return [], None


def xai_twin(model: str) -> str:
    """The non-reasoning twin of a reasoning model that takes no level
    (``grok-4.20-0309-reasoning`` → ``grok-4.20-0309-non-reasoning``); ``""``
    when it has none by name. Whether it exists is the catalogue's to say."""
    low = _xai_model(model)
    if not low or "non-reasoning" in low or "multi-agent" in low or xai_efforts(model)[0]:
        return ""
    if not _XAI_REASONING_SUFFIX.search(model):
        return ""
    head, _, tail = model.rpartition("-reasoning")
    return f"{head}-non-reasoning{tail}"


def xai_request(model: str, effort: str, *, listed: frozenset[str] | set[str] = frozenset()) -> tuple[str, str]:
    """``(model to send, level to send or "")`` for an agent at ``effort``.

    ``none`` turns thinking off: on Grok 4.3 it is sent as is; on a model
    whose non-reasoning twin is in ``listed`` (the catalogue's ids) the twin
    is sent instead; on a model that cannot stop thinking it becomes the
    weakest level it takes. A model that takes no level gets none.
    """
    levels, _default = xai_efforts(model)
    if effort == "none":
        twin = xai_twin(model)
        if twin and twin in listed:
            return twin, ""
    if not effort or not levels:
        return model, ""
    level = clamp_effort(effort, levels)
    return model, level if level in levels else ""
