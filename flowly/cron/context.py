"""ContextVar-based flag for "this coroutine tree is a cron run".

Used so cross-cutting code (approval gate, toolset filter) can detect it's
running inside a scheduled job without having to thread a parameter through
every call site. Set by `gateway_cmd.on_cron_job`, read by downstream code
(`flowly.exec.executor`).

Why ContextVar and not os.environ?
    The gateway handles many concurrent requests. os.environ is process-
    global and would leak cron state into in-flight user chats. ContextVar
    is per-task, so each async call tree has its own value.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_IN_CRON: ContextVar[bool] = ContextVar("flowly_in_cron", default=False)


def in_cron_context() -> bool:
    """Return True if the current task is executing under a cron run."""
    return _IN_CRON.get()


@contextmanager
def cron_context() -> Iterator[None]:
    """Mark the enclosed block as running inside a cron job.

    Use around `agent.process_direct(...)` in the cron callback so approval
    gates and tool policies can apply cron-specific rules.
    """
    token = _IN_CRON.set(True)
    try:
        yield
    finally:
        _IN_CRON.reset(token)
