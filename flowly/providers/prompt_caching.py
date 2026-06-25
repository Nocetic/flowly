"""Anthropic prompt caching — inject cache_control breakpoints.

Places up to 4 ``cache_control`` markers on messages so the Anthropic API
(via OpenRouter or direct) caches the stable prefix across turns:

  1. System prompt  (stable across all turns → highest cache hit rate)
  2–4. Last 3 non-system messages  (rolling window)

Cached tokens cost 0.1x on reads (90% saving) and 1.25x on writes.
After the first turn the system prompt is almost always a cache hit.

TTL — 1h vs 5m
~~~~~~~~~~~~~~

Anthropic supports two cache lifetimes: the default 5-minute ephemeral
cache, and a 1-hour ephemeral cache (opted into per breakpoint via
``ttl: "1h"``). The 1-hour cache survives across multi-turn sessions
that span coffee breaks, IDE switches, or background tool runs — a
5-minute cache misses every time the user steps away for more than a
song.

Flowly's default is **1h**, set here. Empirical cost shape:

  * 5m TTL — write 1.25×, read 0.1×, expires fast → frequent rewrites
    when the user pauses, effective average ≈ 0.5-0.7× of uncached.
  * 1h TTL — write 2× (one-time premium), read 0.1× → on a long
    session, effective average ≈ 0.15-0.25× of uncached.

The 2× write premium is paid ONCE per session prefix. Read is the
hot path; every subsequent turn within an hour is at 0.1×. The
break-even is ~3 turns within the hour; almost every real Flowly
session beats that easily.

Override via ``FLOWLY_CLAUDE_CACHE_TTL`` (``"5m"`` to revert) if a
specific deployment sees odd caching behaviour with the upstream
proxy.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Literal

# Anthropic-supported TTL values. ``"5m"`` was the original default;
# ``"1h"`` is the Flowly default now (see module docstring for the
# cost / hit-rate rationale). Anything else passed via env var
# falls back to ``"1h"`` with a debug log — no silent miscaching.
CacheTTL = Literal["5m", "1h"]
_VALID_TTLS: frozenset[str] = frozenset({"5m", "1h"})

# Module-level constant so the marker dict is built once at import
# time (the structure does not change across calls). Override via
# ``FLOWLY_CLAUDE_CACHE_TTL`` env var at process start; mid-process
# overrides require ``set_default_cache_ttl()``.
def _resolve_default_ttl() -> CacheTTL:
    raw = (os.environ.get("FLOWLY_CLAUDE_CACHE_TTL") or "").strip().lower()
    if raw in _VALID_TTLS:
        return raw  # type: ignore[return-value]
    return "1h"


_DEFAULT_TTL: CacheTTL = _resolve_default_ttl()


def _build_marker(ttl: CacheTTL) -> dict[str, str]:
    """Build a ``cache_control`` marker dict for *ttl*.

    Anthropic accepts ``{"type": "ephemeral"}`` (5m default) or
    ``{"type": "ephemeral", "ttl": "1h"}``. The shape is the same
    one OpenRouter forwards to the Anthropic upstream; the Flowly
    backend proxy (``useflowlyapp.com``) passes ``cache_control``
    through verbatim.
    """
    marker: dict[str, str] = {"type": "ephemeral"}
    if ttl == "1h":
        marker["ttl"] = "1h"
    # "5m" is the API default — omitting ``ttl`` keeps the request
    # body slightly smaller and avoids redundancy.
    return marker


def set_default_cache_ttl(ttl: CacheTTL) -> None:
    """Override the default TTL used by ``apply_cache_control``.

    Mostly for tests; production callers configure via the env var.
    Invalid values are clamped to ``"1h"`` to avoid silent misconfig.
    """
    global _DEFAULT_TTL
    _DEFAULT_TTL = ttl if ttl in _VALID_TTLS else "1h"


def _apply_marker(msg: dict[str, Any], marker: dict[str, str]) -> None:
    """Add cache_control to the last content block of a message.

    Tool messages (``role: "tool"``) are intentionally skipped:
    putting a top-level ``cache_control`` on a tool message is
    either silently rejected or induces a silent hang on OpenRouter's
    Claude pipeline. The previous branch in this function tried to
    mark tool messages with a top-level field anyway; that behaviour
    has been removed. Tool messages still get cached implicitly as
    part of the rolling window of non-system messages — the marker
    lives on whichever non-tool message ends the breakpoint window.
    """
    content = msg.get("content")

    # Tool messages: skip — top-level cache_control on role:"tool" is
    # unsafe on OpenRouter (silent hang observed on the Claude pipeline).
    if msg.get("role") == "tool":
        return

    # None/empty content
    if not content:
        msg["cache_control"] = marker
        return

    # String content → convert to content block list
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": marker}
        ]
        return

    # List content → mark last block
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = marker


def apply_cache_control(
    messages: list[dict[str, Any]],
    max_breakpoints: int = 4,
    ttl: CacheTTL | None = None,
) -> list[dict[str, Any]]:
    """Return a deep copy of *messages* with cache_control breakpoints.

    Strategy — "system + last 3":
      - 1 breakpoint on the system prompt (slot 0)
      - Up to 3 on the last non-system messages (rolling window)

    Args:
        messages: Conversation messages (not modified in place).
        max_breakpoints: Maximum cache breakpoints (Anthropic limit is 4).
        ttl: Override the module default (``_DEFAULT_TTL``, normally
            ``"1h"``). Useful for the rare caller that wants 5m
            behaviour for a specific request without touching the
            module global. ``None`` → use the default.

    Returns:
        New message list with cache markers injected.
    """
    if not messages:
        return messages

    effective_ttl: CacheTTL = ttl if ttl in _VALID_TTLS else _DEFAULT_TTL
    marker = _build_marker(effective_ttl)

    msgs = copy.deepcopy(messages)
    used = 0

    # 1. System prompt (always first if present)
    if msgs[0].get("role") == "system":
        _apply_marker(msgs[0], marker)
        used += 1

    # 2. Last N non-system, non-tool messages.
    #
    # Tool messages are excluded because ``_apply_marker`` no-ops on
    # them (see its docstring — OpenRouter silent-hang risk). If we
    # picked a tool message here we'd consume a breakpoint slot and
    # emit nothing, leaving a turn effectively uncached. Excluding
    # them from the selection spends every slot on a message that
    # actually receives the marker.
    remaining = max_breakpoints - used
    candidate_indices = [
        i for i in range(len(msgs))
        if msgs[i].get("role") not in ("system", "tool")
    ]
    for idx in candidate_indices[-remaining:]:
        _apply_marker(msgs[idx], marker)

    return msgs


def is_cacheable_model(model: str) -> bool:
    """Return True if the model supports Anthropic prompt caching."""
    if not model:
        return False
    lower = model.lower()
    return "claude" in lower
