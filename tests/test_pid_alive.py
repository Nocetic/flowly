"""An "is it alive" check never signals the process it asks about.

2026-11-16, Windows: a named agent asked every two seconds whether its main
agent was alive with ``os.kill(pid, 0)``. On Windows signal 0 is Ctrl+C to the
whole console, so the main agent and the named agent both stopped, the service
started the main agent again, it started the named agent again, and so on.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from flowly import pid_alive as module
from flowly.agent.subagent_owner import _pid_alive
from flowly.exec.process_checkpoint import _is_pid_alive
from flowly.goals.manager import _pid_is_alive as goal_waits_on
from flowly.mcp.proc import _alive
from flowly.profile import _pid_is_alive

CORE = Path(module.__file__).resolve().parent


class _Kernel32:
    """The three calls ``pid_alive`` makes on Windows, for a process that runs (or not)."""

    def __init__(self, *, running: dict[int, int], denied: frozenset[int] = frozenset()) -> None:
        self.running = running  # pid -> exit code (259 while it runs)
        self.denied = denied
        self.error = 0
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802
        if pid in self.denied:
            self.error = 5
            return 0
        if pid not in self.running:
            self.error = 87  # ERROR_INVALID_PARAMETER: no such process
            return 0
        return 1000 + pid

    def GetLastError(self) -> int:  # noqa: N802
        return self.error

    def GetExitCodeProcess(self, handle: int, code) -> bool:  # noqa: N802
        code._obj.value = self.running[handle - 1000]
        return True

    def CloseHandle(self, handle: int) -> bool:  # noqa: N802
        self.closed.append(handle)
        return True


@pytest.fixture
def windows(monkeypatch):
    """Core as it runs on Windows: os.kill there would stop the process, so it fails the test."""
    kernel32 = _Kernel32(running={4242: 259, 4243: 0}, denied=frozenset({4}))
    monkeypatch.setattr(ctypes, "windll", type("windll", (), {"kernel32": kernel32})(), raising=False)
    monkeypatch.setattr(module.os, "name", "nt")

    def kill(pid, sig):
        raise AssertionError(f"os.kill({pid}, {sig}) on Windows stops the process instead of asking about it")

    monkeypatch.setattr(module.os, "kill", kill)
    return kernel32


def test_on_windows_the_main_agent_is_asked_about_never_signalled(windows):
    # The check a named agent makes on its main agent (profile._desktop_manager_context and the
    # gateway's manager watch both use it).
    assert _pid_is_alive(4242) is True
    assert _pid_is_alive(4243) is False  # exited
    assert _pid_is_alive(9999) is False  # gone
    assert _pid_is_alive(4) is True  # someone else's: it exists
    assert windows.closed == [5242, 5243]


def test_on_windows_every_alive_check_in_core_asks_without_signalling(windows):
    for check in (_pid_alive, _is_pid_alive, goal_waits_on, _alive):
        assert check(4242) is True, check
        assert check(9999) is False, check


def test_no_alive_check_in_core_uses_signal_zero():
    """Only flowly/pid_alive.py may probe with os.kill(pid, 0), and only off Windows."""
    probe = re.compile(r"os\.kill\(\s*[^,()]+(?:\([^()]*\))?\s*,\s*0\s*\)")
    found = []
    for path in sorted(CORE.rglob("*.py")):
        if path.name == "pid_alive.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if probe.search(code):
                found.append(f"{path.relative_to(CORE.parent)}:{number}: {line.strip()}")
    assert found == []


def test_off_windows_a_running_process_and_a_gone_one():
    assert module.pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert module.pid_alive(child.pid) is False
    assert module.pid_alive(0) is False
    assert module.pid_alive(-1) is False
