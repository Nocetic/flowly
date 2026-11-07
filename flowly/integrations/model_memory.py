"""The model an agent last chose on each provider.

Before (2026-11-07): switching provider replaced the agent's model with the
new provider's curated default (``DEFAULT_MODELS``) whenever the old model did
not fit it, so coming back to a provider never brought back the model chosen
there. Each agent (the main one's ``config.json``, a named one's own) now
keeps ``agents.defaults.modelsByProvider``: a model is remembered under the
provider it was chosen on, and a switch brings back the target provider's
remembered model when that provider still serves it.

Functions take the raw config dict (camelCase keys, as on disk) so the main
agent's ``set_active_provider`` and a named agent's profile settings share
them.
"""

from __future__ import annotations

from typing import Any

#: Kept entries; the oldest go first (a provider rarely used again).
MAX_REMEMBERED = 64
_MAX_MODEL_CHARS = 300


def _same_provider(left: str, right: str) -> bool:
    """Provider keys compared as written either way: the config writer turns
    dict keys to camelCase on disk (``openaiCodex``), the loader back
    (``openai_codex``)."""
    return left.replace("_", "").lower() == right.replace("_", "").lower()


def _defaults(raw: dict[str, Any]) -> dict[str, Any]:
    agents = raw.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = raw["agents"] = {}
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = agents["defaults"] = {}
    return defaults


def remembered(raw: dict[str, Any], provider: str) -> str:
    """The model remembered for ``provider`` (``""`` when none)."""
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    memory = defaults.get("modelsByProvider")
    if not isinstance(memory, dict):
        return ""
    for key, value in memory.items():
        if isinstance(key, str) and isinstance(value, str) and _same_provider(key, provider):
            return value.strip()
    return ""


def remember(raw: dict[str, Any], provider: str, model: str) -> None:
    """Remember ``model`` as the one chosen on ``provider`` (most recent last)."""
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider or not model or len(model) > _MAX_MODEL_CHARS:
        return
    defaults = _defaults(raw)
    memory = defaults.get("modelsByProvider")
    if not isinstance(memory, dict):
        memory = {}
    memory = {
        k: v for k, v in memory.items()
        if isinstance(k, str) and isinstance(v, str) and not _same_provider(k, provider)
    }
    memory[provider] = model
    while len(memory) > MAX_REMEMBERED:
        memory.pop(next(iter(memory)))
    defaults["modelsByProvider"] = memory


def model_on_switch(raw: dict[str, Any], old_provider: str, new_provider: str, current_model: str) -> str | None:
    """The model an agent takes when it moves from ``old_provider`` to
    ``new_provider``: its remembered one there, when that provider serves it
    (``None`` when nothing is remembered or it no longer fits, so the caller
    applies its own rule). The current model is remembered under the old
    provider first, so coming back finds it."""
    if old_provider and old_provider != new_provider and current_model:
        remember(raw, old_provider, current_model)
    return recall(raw, new_provider)


def recall(raw: dict[str, Any], provider: str) -> str | None:
    """The model remembered for ``provider`` when that provider still serves
    it; ``None`` otherwise. Reads only."""
    from flowly.integrations.active_provider import model_fits_provider

    model = remembered(raw, provider)
    new_provider = provider
    if not model or not model_fits_provider(model, new_provider):
        return None
    # A local server's list says what it serves now (a model deleted there
    # must not come back); a provider never read leaves the fit check alone.
    from flowly.integrations.model_catalog import cached_ids

    served = cached_ids(new_provider)
    if served and model not in served:
        return None
    return model
