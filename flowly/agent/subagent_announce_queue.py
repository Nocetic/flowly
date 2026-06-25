"""Announce queue — buffers subagent results when parent is busy."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from loguru import logger

_DEBOUNCE_SECONDS = 1.0
_DEFAULT_CAP = 20


@dataclass
class AnnounceItem:
    prompt: str
    summary: str     # one-liner for batch display
    enqueued_at: float = field(default_factory=time.time)


class AnnounceQueue:
    """Per-session announce queue with debounce and batching."""

    def __init__(
        self,
        session_key: str,
        send_fn: Callable[[str], Awaitable[None]],
        cap: int = _DEFAULT_CAP,
    ) -> None:
        self.session_key = session_key
        self._send_fn = send_fn
        self._cap = cap
        self.items: list[AnnounceItem] = []
        self._draining = False
        self._last_enqueued = 0.0

    async def enqueue(self, item: AnnounceItem) -> None:
        # Drop oldest if over cap
        if len(self.items) >= self._cap:
            dropped = self.items.pop(0)
            logger.debug(f"[AnnounceQueue] Dropped oldest item: {dropped.summary}")
        self.items.append(item)
        self._last_enqueued = time.time()
        if not self._draining:
            asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        self._draining = True
        try:
            # Deliver each completion as its OWN message so the user gets one
            # reply per finished subagent (incremental), the way the parent
            # would answer if it were idle when each child completed. We do NOT
            # merge them into a single "N tasks completed" summary — that
            # collapsed staggered completions into one delayed burst, which is
            # exactly the behaviour we're fixing. The parent loop processes each
            # published InboundMessage as a separate turn → a separate reply.
            #
            # This queue only exists to hold announces while the parent is busy
            # (see SubagentManager._announce); ordering is preserved (FIFO) and
            # the cap in enqueue() bounds growth.
            while self.items:
                item = self.items.pop(0)
                try:
                    await self._send_fn(item.prompt)
                except Exception as e:
                    logger.error(
                        f"[AnnounceQueue] send failed for {self.session_key} "
                        f"({item.summary}): {e}"
                    )
        finally:
            self._draining = False
            if self.items:
                # Items added while draining — restart
                asyncio.create_task(self._drain())
            else:
                # Queue empty — remove from global store to prevent leak
                remove_queue(self.session_key)


# Module-level queue store
_queues: dict[str, AnnounceQueue] = {}


def get_or_create_queue(
    session_key: str,
    send_fn: Callable[[str], Awaitable[None]],
) -> AnnounceQueue:
    if session_key not in _queues:
        _queues[session_key] = AnnounceQueue(session_key, send_fn)
    return _queues[session_key]


def remove_queue(session_key: str) -> None:
    _queues.pop(session_key, None)
