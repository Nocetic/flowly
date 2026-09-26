"""Process-shared exclusion between a live screen and environment maintenance.

Locks describe live activity only. A durable installation receipt must still
fence recovery after a worker/agent crash; losing a lock is not recovery proof.
"""

import hashlib
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenError


@contextmanager
def environment_activity(*, exclusive: bool = False):
    import fcntl

    from flowly.desktop_runtime.launcher import private_directory

    parent = private_directory(Path('/tmp') / f'flowly-screen-{os.getuid()}')
    key = hashlib.sha256(os.fsencode(Path(sys.prefix).resolve())).hexdigest()[:24]
    descriptor = os.open(parent / f'environment-{key}.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ScreenError('INSTALL_STATE_INVALID')
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScreenError('INSTALL_BUSY') from None
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def agent_environment_activity():
    """Keep a Linux agent turn out of optional dependency maintenance.

    Other platforms do not support this installation backend. Each task gets
    its own descriptor: a parent finishing cannot release a child's activity.
    """
    if sys.platform != "linux":
        yield
        return
    from flowly.desktop_runtime.install_jobs import assert_environment_installation_idle

    with environment_activity() as descriptor:
        assert_environment_installation_idle()
        yield descriptor
