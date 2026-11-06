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

* Anthropic: ``output_config: {"effort": …}``; the levels follow the model
  family (``anthropic_efforts``).
* OpenAI (API key), Gemini, Groq, Z.AI GLM (API key and Coding Plan): the
  levels each model takes come from models.dev's ``reasoning_options``
  (``models_dev_efforts``); OpenAI, Gemini and Groq take ``reasoning_effort``,
  GLM takes ``thinking: {"type": "enabled" | "disabled"}`` beside it. A model
  that can only switch thinking on and off is offered the switch alone
  (``SWITCH_LEVELS``, ``switch_only``).

A level a model does not take is lowered to the nearest one it does
(``clamp_effort``), never raised and never switched off: an agent set to
``ultra`` runs a model that stops at ``max`` at ``max``, and ``ultra`` again on
one that has it. The setting itself is kept.

The free Flowly plan goes up to ``medium`` (``FREE_PLAN_EFFORT_CAP``); the
proxy enforces the same cap, Core only shows and applies it. A user's own
subscription or key is never capped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


# Anthropic's Messages API, by model family, as documented in 2026-11
# (platform docs, model migration guides). Each family says:
#
# - ``levels``: what ``output_config.effort`` takes;
# - ``off``: the ``thinking`` field that switches thinking off, or None when it
#   cannot be (Fable, Mythos and Opus 5.5 always think: ``disabled`` is a
#   400). Sonnet 5.5's off is ``between_tools`` (``disabled`` is a 400
#   there); Opus 5 and Haiku 5.5 take ``disabled`` only at effort high or
#   lower, so off sends no effort;
# - ``thinks``: whether it thinks when no ``thinking`` is sent. Opus 4.6 to
#   4.8 do not: thinking on is ``{"type": "adaptive"}``;
# - ``sampling``: whether it takes ``temperature`` (with thinking off). Opus
#   4.7 and later refuse it outright, Sonnet 5.5 and Haiku 5.5 refuse any
#   value but the default, so it is left out.
#
# Opus 4.5 takes an effort but thinks only with a token budget, which Flowly
# does not send: no switch. Families not listed (Sonnet 4.5, Haiku 4.5 and
# older) take no effort: the API answers 400.
_ANTHROPIC_FULL = ("low", "medium", "high", "xhigh", "max")
_DISABLED = {"type": "disabled"}


@dataclass(frozen=True)
class AnthropicThinking:
    levels: tuple[str, ...]
    default: str
    off: dict[str, str] | None
    thinks: bool
    sampling: bool


_ANTHROPIC_FAMILIES: tuple[tuple[re.Pattern[str], AnthropicThinking], ...] = (
    (re.compile(r"opus-5[.-]5\b"), AnthropicThinking(_ANTHROPIC_FULL, "medium", None, True, False)),
    (re.compile(r"(fable|mythos)-5\b"), AnthropicThinking(_ANTHROPIC_FULL, "high", None, True, False)),
    (re.compile(r"haiku-5[.-]5\b"), AnthropicThinking(_ANTHROPIC_FULL, "medium", _DISABLED, True, False)),
    (re.compile(r"sonnet-5[.-]\d\b"),
     AnthropicThinking(_ANTHROPIC_FULL, "high", {"type": "between_tools"}, True, False)),
    (re.compile(r"(opus|sonnet)-5\b"), AnthropicThinking(_ANTHROPIC_FULL, "high", _DISABLED, True, False)),
    (re.compile(r"opus-4[.-][78]\b"), AnthropicThinking(_ANTHROPIC_FULL, "high", _DISABLED, False, False)),
    (re.compile(r"(opus|sonnet)-4[.-]6\b"),
     AnthropicThinking(("low", "medium", "high", "max"), "high", _DISABLED, False, True)),
    (re.compile(r"opus-4[.-]5\b"), AnthropicThinking(("low", "medium", "high"), "high", None, False, True)),
)
_CLAUDE_VERSION = re.compile(r"(opus|sonnet|haiku|fable|mythos)-(\d+)(?:[.-](\d{1,2})(?=$|[.-]))?")


def anthropic_thinking(model: str) -> AnthropicThinking | None:
    """How an Anthropic model's thinking is set; None for a family that
    takes no effort. ``model`` may carry a provider prefix
    (``anthropic/claude-opus-4.7``) and a dated suffix."""
    low = (model or "").strip().lower()
    for pattern, rules in _ANTHROPIC_FAMILIES:
        if pattern.search(low):
            return rules
    return None


def anthropic_efforts(model: str) -> tuple[list[str], str | None]:
    """The levels an Anthropic model takes (``none`` first where thinking
    can be switched off) and its default; ``([], None)`` for one that takes
    none."""
    rules = anthropic_thinking(model)
    if rules is None:
        return [], None
    return (["none"] if rules.off is not None else []) + list(rules.levels), rules.default


def anthropic_takes_sampling(model: str) -> bool:
    """Whether ``temperature`` may be sent (with thinking off). A model no
    family lists keeps it only when it is older than Claude 4.7: newer ones
    refuse it."""
    rules = anthropic_thinking(model)
    if rules is not None:
        return rules.sampling
    match = _CLAUDE_VERSION.search((model or "").lower())
    if match is None:
        return True
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    if family in ("fable", "mythos"):
        return False
    return (major, minor) < (4, 7)


def anthropic_request(model: str, effort: str) -> tuple[dict[str, object], bool]:
    """``(fields, thinks)`` for an agent at ``effort``: the ``thinking`` and
    ``output_config`` fields a request carries, and whether the model will
    think on it (its thinking blocks are then passed back, and
    ``temperature`` is left out). No level set: nothing is sent, and the
    model does what it does by default."""
    rules = anthropic_thinking(model)
    if rules is None:
        return {}, False
    if not effort:
        return {}, rules.thinks
    levels, _default = anthropic_efforts(model)
    level = clamp_effort(effort, levels)
    if level not in levels:
        return {}, rules.thinks
    if level == "none":
        return {"thinking": dict(rules.off or {})}, False
    fields: dict[str, object] = {"output_config": {"effort": level}}
    if rules.thinks:
        return fields, True
    if rules.off is not None:
        fields["thinking"] = {"type": "adaptive"}
        return fields, True
    return fields, False


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


#: What a model that can only switch thinking on and off offers: the switch
#: (``none``) and thinking on. ``high`` there means "on", and carries no level.
SWITCH_LEVELS: tuple[str, ...] = ("none", "high")

#: The level a model thinks at when none is sent, by provider, as documented
#: (Gemini 3: ``high``; GLM-5.3: ``max``). Elsewhere ``medium``, the level the
#: other reasoning APIs default to, or the nearest one the model takes.
_MODELS_DEV_DEFAULTS: dict[str, str] = {"gemini": "high", "zhipu": "max", "zai_coding": "max"}


@dataclass(frozen=True)
class ThinkingControl:
    """What a model's thinking can be set to, read from a catalogue."""

    #: The levels offered, lowest first; ``none`` when thinking can be off.
    levels: list[str]
    default: str | None
    #: Only on and off: the ``high`` in ``levels`` means "on" and sends no level.
    switch_only: bool = False


NO_THINKING_CONTROL = ThinkingControl([], None)


def models_dev_efforts(options: object, provider_key: str) -> ThinkingControl:
    """The thinking control a models.dev ``reasoning_options`` list describes.

    ``{"type": "effort", "values": […]}`` lists the levels (``default``, a
    Groq value meaning "the model's own", is not a level); ``{"type":
    "toggle"}`` means thinking can be switched off; ``{"type":
    "budget_tokens", "min": 0}`` too. Gemini's OpenAI-compatible API takes a
    level for a model that only lists a budget, and maps it to one: those get
    ``low`` to ``high``. Nothing listed: no control."""
    if not isinstance(options, list):
        return NO_THINKING_CONTROL
    levels: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            continue
        kind = option.get("type")
        if kind == "effort":
            levels.update(ordered_efforts(option.get("values")))
        elif kind == "toggle":
            levels.add("none")
        elif kind == "budget_tokens":
            if option.get("min") == 0:
                levels.add("none")
            if provider_key == "gemini":
                levels.update(("low", "medium", "high"))
    ordered = [level for level in EFFORT_ORDER if level in levels]
    thinking = [level for level in ordered if level != "none"]
    if not thinking:
        if "none" in ordered:
            return ThinkingControl(list(SWITCH_LEVELS), "high", switch_only=True)
        return NO_THINKING_CONTROL
    default = _MODELS_DEV_DEFAULTS.get(provider_key, "medium")
    if default not in thinking:
        default = "medium" if "medium" in thinking else clamp_effort("medium", thinking)
    return ThinkingControl(ordered, default)


def thinking_body(provider_key: str, control: ThinkingControl, effort: str) -> dict[str, object]:
    """The body fields an agent at ``effort`` sends to a model with
    ``control`` on ``provider_key`` (openai, gemini, groq, zhipu,
    zai_coding). Nothing when no level is set
    or the model takes none: it then thinks as it does by default.

    - OpenAI, Gemini, Groq: ``reasoning_effort``; on a switch-only model,
      ``none`` for off and nothing for on.
    - Z.AI GLM: ``thinking: {"type": "disabled"}`` for off; ``{"type":
      "enabled"}`` for on, with ``reasoning_effort`` on a model that has levels.
      A model that cannot stop thinking gets the level alone.
    """
    if not effort or not control.levels:
        return {}
    level = clamp_effort(effort, control.levels)
    if level not in control.levels:
        return {}
    glm = provider_key in ("zhipu", "zai_coding")
    if level == "none":
        return {"thinking": {"type": "disabled"}} if glm else {"reasoning_effort": "none"}
    if control.switch_only:
        return {"thinking": {"type": "enabled"}} if glm else {}
    if glm and "none" in control.levels:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": level}
    return {"reasoning_effort": level}
