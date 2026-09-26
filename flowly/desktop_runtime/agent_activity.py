"""Maintenance leases for optional background work and real worker lifetimes."""

from contextlib import ExitStack
from functools import wraps

from loguru import logger

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.installation_gate import agent_environment_activity


def background_agent_activity(function):
    """Defer best-effort background passes when environment maintenance owns it.

    Only admission refusal is deferred. Exceptions from the admitted operation
    retain its existing handling, and cancellation releases that task's lease.
    """
    @wraps(function)
    async def guarded(*args, **kwargs):
        with ExitStack() as activity:
            try:
                activity.enter_context(agent_environment_activity())
            except ScreenError as exc:
                logger.debug("Background agent work deferred for environment maintenance: {}", exc.code)
                return None
            return await function(*args, **kwargs)
    return guarded


def run_agent_worker(function, *args, **kwargs):
    """Acquire inside the worker, so cancelling its async waiter cannot unlock it."""
    with agent_environment_activity():
        return function(*args, **kwargs)
