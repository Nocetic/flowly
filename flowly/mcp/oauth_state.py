"""Private, atomic OAuth state and cancellable cross-process recovery leases."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


def _open_lock(path: Path) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("OAuth lock must be a regular file")
        from flowly.utils.file_security import secure_file

        secure_file(path)
        # Windows byte-range locking requires at least one byte in the file.
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _try_lock(descriptor: int) -> bool:
    try:
        if os.name == "nt":  # pragma: no cover - Windows host
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise


@contextmanager
def state_lock(path: Path, timeout: float = 10.0):
    """Short read/modify/write lease; never held across a network operation."""
    descriptor = _open_lock(path)
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(descriptor):
            if time.monotonic() >= deadline:
                raise TimeoutError("OAuth state is busy")
            time.sleep(0.01)
        yield
    finally:
        os.close(descriptor)


@asynccontextmanager
async def recovery_lease(path: Path, timeout: float = 330.0):
    """One authorization/refresh per credential file, including other processes.

    Nonblocking acquisition keeps the event loop responsive. Cancellation or
    process death releases the OS lock; there is no stale PID/TTL lock to reap.
    """
    descriptor = _open_lock(path)
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(descriptor):
            if time.monotonic() >= deadline:
                raise TimeoutError("OAuth recovery is busy; retry after the current login")
            await asyncio.sleep(0.025)
        yield
    finally:
        os.close(descriptor)


def read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise OSError("Invalid OAuth state file")
        value = handle.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            raise OSError("OAuth state exceeds the size limit")
        return value


def atomic_private_write(path: Path, data: bytes) -> None:
    """Create owner-only *before* writing credentials, then durably replace."""
    if len(data) > 1024 * 1024:
        raise ValueError("OAuth state exceeds the size limit")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        from flowly.utils.file_security import secure_file

        secure_file(temporary_path)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        secure_file(path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
