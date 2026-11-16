"""Whether a process is running, asked without touching it.

On POSIX ``os.kill(pid, 0)`` is the usual probe. On Windows it is not one:
signal 0 is ``CTRL_C_EVENT``, so ``os.kill(pid, 0)`` sends Ctrl+C to every
process on the console, the asker included, and any other number terminates
the process. A named agent asking whether its main agent was alive this way
stopped both, every two seconds, under the Windows service (2026-11-16). On
Windows the process is opened for a query instead.

Every "is it alive" check in Core goes through ``pid_alive``
(tests/test_pid_alive.py keeps ``os.kill(..., 0)`` out of the rest of Core).
"""

from __future__ import annotations

import os

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259


def _windows_pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # It exists but belongs to someone this process may not query.
        return kernel32.GetLastError() == _ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == _STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a running process (one that exists but is not ours counts)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
