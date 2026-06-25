"""Cross-platform tests for flowly.utils.subprocess_compat.

These run on any host: the Windows branches are exercised by monkeypatching
``os.name`` (and injecting the Win32 ``creationflags`` constants, which don't
exist on POSIX) so the *logic* is verified from macOS/Linux CI. The POSIX
assertions pin the pre-existing behavior so a future edit can't silently change
how macOS/Linux spawn processes.
"""

import os
import subprocess

from flowly.utils import subprocess_compat as sc

# Real Win32 values, hard-coded so the assertions are meaningful on POSIX hosts
# where these attributes are absent.
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


# --- POSIX: must stay byte-identical to the old inline kwargs --------------

def test_posix_hide_window_is_empty(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert sc.hide_window_kwargs() == {}


def test_posix_detach_is_start_new_session(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert sc.detach_kwargs() == {"start_new_session": True}


# --- Windows: translate intent to creationflags ----------------------------

def test_windows_hide_sets_only_create_no_window(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW, raising=False)
    kw = sc.hide_window_kwargs()
    assert kw == {"creationflags": _CREATE_NO_WINDOW}
    # hide must never detach / never request a process group
    assert "start_new_session" not in kw


def test_windows_detach_combines_no_window_and_detached(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS, raising=False)
    kw = sc.detach_kwargs()
    assert kw == {"creationflags": _CREATE_NO_WINDOW | _DETACHED_PROCESS}


def test_windows_never_sets_new_process_group(monkeypatch):
    """Regression guard: CREATE_NEW_PROCESS_GROUP cancels the ProactorEventLoop
    task on Win/Py3.11 (KeyboardInterrupt mid-turn). Neither helper may set it."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _CREATE_NEW_PROCESS_GROUP, raising=False)
    for kw in (sc.hide_window_kwargs(), sc.detach_kwargs()):
        flags = kw.get("creationflags", 0)
        assert not (flags & _CREATE_NEW_PROCESS_GROUP)


def test_windows_missing_constants_degrade_to_zero(monkeypatch):
    """getattr(..., 0) keeps things crash-free even if a constant is absent."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    monkeypatch.delattr(subprocess, "DETACHED_PROCESS", raising=False)
    assert sc.hide_window_kwargs() == {"creationflags": 0}
    assert sc.detach_kwargs() == {"creationflags": 0}
