"""Trusted, task-local execution ownership, kept outside model tool arguments."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ToolCallOrigin:
    session_key: str
    loop: asyncio.AbstractEventLoop
    context: contextvars.Context


_origin: contextvars.ContextVar[ToolCallOrigin | None] = contextvars.ContextVar(
    "flowly_tool_call_origin", default=None,
)


def current_tool_origin() -> ToolCallOrigin | None:
    return _origin.get()


@contextmanager
def tool_execution_scope(session_key: str | None) -> Iterator[None]:
    """Bind an owner supplied by the runtime, never by a remote tool's schema."""
    if session_key is None:
        yield  # Nested dispatch inherits its caller; standalone calls have no owner.
        return
    origin = ToolCallOrigin(session_key, asyncio.get_running_loop(), contextvars.copy_context())
    token = _origin.set(origin)
    try:
        yield
    finally:
        _origin.reset(token)
