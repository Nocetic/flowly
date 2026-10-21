"""Sleeping until a wall-clock time on a machine that may be suspended.

A suspended machine stops CLOCK_MONOTONIC, and asyncio sleeps on it: on Fly,
a 60 s ``asyncio.sleep`` fired 191 s late after a 196 s suspend. A job due
while the machine slept must run right after it resumes, so long waits are
cut into short sleeps that each look at the wall clock again.
"""

import asyncio
import time

# How late a timer may fire after a resume, at most.
WALL_CLOCK_CHUNK_S = 30.0


async def sleep_until_wall(deadline: float) -> None:
    """Return once ``time.time()`` reaches ``deadline`` (epoch seconds)."""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, WALL_CLOCK_CHUNK_S))
