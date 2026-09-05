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
    allowed_tools: frozenset[str] | None = None


_origin: contextvars.ContextVar[ToolCallOrigin | None] = contextvars.ContextVar(
    "flowly_tool_call_origin", default=None,
)


def current_tool_origin() -> ToolCallOrigin | None:
    return _origin.get()


@contextmanager
def tool_execution_scope(
    session_key: str | None, *, allowed_tools: frozenset[str] | None = None,
) -> Iterator[None]:
    """Bind an owner supplied by the runtime, never by a remote tool's schema."""
    parent = current_tool_origin()
    owner = session_key if session_key is not None else (parent.session_key if parent else None)
    if owner is None:
        yield  # Standalone calls have no owner to delegate.
        return
    if parent and parent.session_key == owner and parent.allowed_tools is not None:
        allowed_tools = parent.allowed_tools if allowed_tools is None else allowed_tools & parent.allowed_tools
    captured = contextvars.copy_context()
    origin = ToolCallOrigin(owner, asyncio.get_running_loop(), captured, allowed_tools)
    captured.run(_origin.set, origin)
    token = _origin.set(origin)
    try:
        yield
    finally:
        _origin.reset(token)
