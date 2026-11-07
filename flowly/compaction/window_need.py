"""How large a context window this agent needs.

Every request carries a fixed overhead compaction cannot shrink: the system
instructions and the tool schemas. On a Desktop agent with its tools it was
55-68K tokens (measured 2026-11-06), so a model run with less context than
that, plus room for the reply and a little conversation, cannot answer at
all. The need is therefore measured, not guessed: the request coordinator
records the overhead of every request it budgets (request_guard), and the
largest recent one decides.

The largest figure is also kept on disk (``<home>/state/window-need.json``),
written only when it changes, so a restarted gateway still knows it before
its first request. An agent that never made one has nothing measured: the
need is then :data:`UNMEASURED_REQUIRED`, the same 64K floor other agents use
for tool use, and callers say it is an estimate.
"""

from __future__ import annotations

import json
import math
import os
import secrets
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

#: Room for the conversation itself beside the overhead and the reply.
MIN_CONVERSATION_ROOM = 8_192
#: The need before any request was measured.
UNMEASURED_REQUIRED = 65_536
#: Never ask for less than this, however lean the agent.
FLOOR = 32_768
#: Sizes servers are commonly loaded at; a load asks for the next one up.
COMMON_SIZES = (32_768, 65_536, 98_304, 131_072, 196_608, 262_144)

#: How many recent requests count. Tools come and go (an MCP server
#: connected, a toolset turned off); old requests fall out of the window.
_KEEP = 32
#: A larger figure is an estimator gone wrong, not a request.
_MAX_PLAUSIBLE = 2_000_000

_lock = Lock()
_observed: deque[int] = deque(maxlen=_KEEP)
#: The figure on disk was read into ``_observed`` (once per process).
_seeded = False
#: The largest figure last written to disk.
_persisted: int | None = None


def _plausible(tokens: object) -> bool:
    return isinstance(tokens, int) and not isinstance(tokens, bool) and 0 < tokens <= _MAX_PLAUSIBLE


def _state_path() -> Path:
    from flowly.profile import get_flowly_home

    return get_flowly_home() / "state" / "window-need.json"


def _seed_locked() -> None:
    """Read the figure a previous run left, once, before anything is measured."""
    global _seeded, _persisted
    if _seeded:
        return
    _seeded = True
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8")).get("fixedOverhead")
    except (OSError, ValueError, AttributeError):
        return
    if _plausible(value):
        _persisted = value
        if not _observed:
            _observed.append(value)


def _persist_locked(value: int) -> None:
    """Write the largest figure, atomically; a failed write only loses the hint."""
    global _persisted
    if value == _persisted:
        return
    path = _state_path()
    tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({
            "fixedOverhead": value,
            "measuredAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }), encoding="utf-8")
        os.replace(tmp, path)
        _persisted = value
    except OSError:
        tmp.unlink(missing_ok=True)


def record_fixed_overhead(tokens: object) -> None:
    """Remember one request's fixed overhead (system instructions + tool schemas)."""
    if not _plausible(tokens):
        return
    with _lock:
        _seed_locked()
        _observed.append(tokens)  # type: ignore[arg-type]
        _persist_locked(max(_observed))


def observed_fixed_overhead() -> int | None:
    """The largest fixed overhead among recent requests (or the last run's);
    None when this agent never made a request."""
    with _lock:
        _seed_locked()
        return max(_observed) if _observed else None


def required_window(fixed_overhead: int, reserve_tokens: int) -> int:
    """Context needed for a request with this overhead: the overhead, the
    reply reserve and room to talk, rounded up to a 1K step, at least :data:`FLOOR`."""
    need = max(0, fixed_overhead) + max(0, reserve_tokens) + MIN_CONVERSATION_ROOM
    return max(FLOOR, math.ceil(need / 1024) * 1024)


def required_context(reserve_tokens: int) -> tuple[int, bool]:
    """``(tokens, measured)``: what this agent needs now, and whether that
    comes from its own requests (False: none yet, the cautious default)."""
    overhead = observed_fixed_overhead()
    if overhead is None:
        return UNMEASURED_REQUIRED, False
    return required_window(overhead, reserve_tokens), True


def load_size(required: int, max_context: int | None) -> int:
    """The context to load a model with: the next common size at or above
    ``required``, within the model's reach. When its reach is below the
    need, its whole reach (the caller checks that it is enough)."""
    size = next((s for s in COMMON_SIZES if s >= required), math.ceil(required / 1024) * 1024)
    if max_context is not None and max_context > 0:
        return min(size, max_context)
    return size


def reset_for_tests() -> None:
    global _seeded, _persisted
    with _lock:
        _observed.clear()
        _seeded = False
        _persisted = None
        try:
            _state_path().unlink(missing_ok=True)
        except OSError:
            pass
