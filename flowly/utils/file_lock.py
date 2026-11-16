"""A lock every Flowly process honours, and a file swap that survives Windows' sharing rules.

macOS and Linux lock a whole file with ``flock``. Windows has no ``fcntl``:
a lock written only for ``flock`` locked nothing there, so a conversation was
read by one process while another swapped it, and on Windows a file another
process has open cannot be replaced. A named agent's first words were lost
that way (2026-11-16): the app read its conversation while the agent saved
its reply, the save failed with "Access is denied", and the chat stayed empty.

- ``lock_exclusive`` / ``unlock`` hold an open handle's file against every other
  handle, in this process or another: ``flock`` on POSIX, a one-byte
  ``msvcrt.locking`` region on Windows (the kernel drops both when the
  process dies).
- ``replace_file`` is ``os.replace``, waiting a moment on Windows while
  something outside Flowly (an antivirus scan, the search indexer, a backup
  tool) has the file open, as those let go within milliseconds.
"""

from __future__ import annotations

import os
import time
from typing import IO, Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # macOS, Linux
    msvcrt = None  # type: ignore[assignment]

_POLL_SECONDS = 0.01
_POLL_MAX_SECONDS = 0.05
#: Long enough for a scanner that opened the file to let it go, short enough that a
#: file someone really keeps open fails the save instead of hanging the agent.
REPLACE_WAIT_SECONDS = 2.0
# ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
_BUSY_WINERRORS = frozenset({5, 32, 33})


def lock_exclusive(handle: IO[Any]) -> None:
    """Block until this handle holds its file alone."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is None:  # pragma: no cover - neither POSIX nor Windows
        return
    delay = _POLL_SECONDS
    while True:
        # Windows locks a byte range from the current position: always byte 0,
        # whatever the handle's mode (an "a+" handle sits at the end).
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SECONDS)


def try_lock_exclusive(handle: IO[Any]) -> bool:
    """Take the file alone if no other handle holds it; never wait."""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if msvcrt is None:  # pragma: no cover
        return True
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def unlock(handle: IO[Any]) -> None:
    """Let the file go (closing the handle does too)."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is None:  # pragma: no cover
        return
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _busy(error: OSError) -> bool:
    return getattr(error, "winerror", None) in _BUSY_WINERRORS


def replace_file(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    """``os.replace``; on Windows, retried briefly while another program has ``target`` open."""
    if os.name != "nt":
        os.replace(source, target)
        return
    deadline = time.monotonic() + REPLACE_WAIT_SECONDS
    delay = _POLL_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if not _busy(error) or time.monotonic() >= deadline:
                raise
        time.sleep(delay)
        delay = min(delay * 2, _POLL_MAX_SECONDS * 2)
