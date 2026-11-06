"""Keep a local model server's real context current between turns.

The agent sizes every request from the model's context window
(``CompactionService`` reads ``model_catalog.get_context_window``). For a model
on Ollama or LM Studio that window is whatever the server runs the model with
now, and it changes behind the agent's back: the owner reloads the model with
another context, or LM Studio unloads an idle model and, on the next request,
loads it again with its own small default. Read once when the provider was
switched, the window went stale: seen on 2026-11-06, a model reloaded at 128K
kept every message refused as too large until the gateway restarted.

So before a main-agent request on a local slot, at most every
:data:`CHECK_EVERY_SECONDS` (or at once after a refusal for a window too
small), the server is asked (flowly/integrations/local_models.py
``runtime_window``): LM Studio is made to hold the model with the context the
agent needs, Ollama is read. The answer is recorded where compaction looks
(``model_catalog.set_runtime_context_window``). A check that fails changes
nothing and never fails the turn: the request guard still refuses honestly.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

#: How often a running agent asks its local server; a local GET, cheap.
CHECK_EVERY_SECONDS = 30.0

_LOCAL_SLOTS = ("ollama", "vllm")


class LocalWindowSync:
    """One agent's view of its local server's context (one per agent loop)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._checked: dict[tuple[str, str, str], float] = {}

    async def before_request(self, provider: object, model: str, *, reserve: int, force: bool = False) -> int | None:
        """Ask the local server behind ``provider`` about ``model``. Returns
        the context it runs it with when it was asked and said; None when it
        was not asked (not a local slot, asked recently) or did not say."""
        slot = getattr(provider, "provider_name", "")
        base = getattr(provider, "api_base", None)
        if slot not in _LOCAL_SLOTS or not isinstance(base, str) or not base or not model:
            return None
        from flowly.integrations.active_provider import is_local_endpoint

        if not is_local_endpoint(base):
            return None
        key = (slot, base, model)
        now = self._clock()
        last = self._checked.get(key)
        if not force and last is not None and now - last < CHECK_EVERY_SECONDS:
            return None
        self._checked[key] = now

        from flowly.compaction.window_need import required_context
        from flowly.integrations import local_models, model_catalog

        required, _measured = required_context(reserve)
        try:
            context = await local_models.runtime_window(slot, base, model, required=required)
        except local_models.LocalServerError as exc:
            logger.warning(f"[local-model] {model}: {exc.message}")
            return None
        except Exception as exc:  # noqa: BLE001 - a check must never fail the turn
            logger.debug(f"[local-model] {model}: context check skipped ({type(exc).__name__})")
            return None
        if context:
            model_catalog.set_runtime_context_window(model, context)
            logger.info(f"[local-model] {model}: the server runs it with {context} tokens of context")
        return context
