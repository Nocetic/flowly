"""Conversations are locked on Windows too, and a save waits out a scanner (2026-11-16).

On a Windows test machine a named agent's first words were lost: the app read
its conversation while the agent saved its reply. The session lock used only
``fcntl``, so on Windows it locked nothing, and Windows refuses to replace a
file another process has open ("[WinError 5] Access is denied"). The turn
failed and the chat stayed empty.
"""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from flowly.session import manager as session_manager
from flowly.session.manager import SessionManager, session_file_lock
from flowly.utils import file_lock


class _FakeMsvcrt:
    """Windows' byte-range locks, one region per file, refused while another handle holds it."""

    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self) -> None:
        self.held: dict[str, int] = {}
        self.calls: list[tuple[str, int]] = []
        self.paths: dict[int, str] = {}

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        import os

        assert nbytes == 1
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0, "Windows locks from the current position: it must be byte 0"
        path = self.paths.setdefault(fd, str(fd))
        self.calls.append(("lock" if mode == self.LK_NBLCK else "unlock" if mode == self.LK_UNLCK else "other", fd))
        if mode == self.LK_NBLCK:
            owner = self.held.get(path)
            if owner is not None and owner != fd:
                raise OSError(36, "Resource deadlock avoided")
            self.held[path] = fd
        elif mode == self.LK_UNLCK:
            self.held.pop(path, None)


@pytest.fixture
def windows(monkeypatch):
    fake = _FakeMsvcrt()
    monkeypatch.setattr(file_lock, "fcntl", None)
    monkeypatch.setattr(file_lock, "msvcrt", fake)
    monkeypatch.setattr(file_lock.time, "sleep", lambda _seconds: None)
    return fake


def test_a_conversation_is_locked_on_windows(tmp_path: Path, windows: _FakeMsvcrt) -> None:
    path = tmp_path / "sessions" / "desktop_profile-home.jsonl"
    path.parent.mkdir()
    with session_file_lock(path):
        assert windows.held, "nothing was locked"
    assert [kind for kind, _ in windows.calls] == ["lock", "unlock"]
    assert not windows.held


def test_a_second_holder_waits_for_the_first_on_windows(tmp_path: Path, windows: _FakeMsvcrt, monkeypatch) -> None:
    lock = tmp_path / "x.lock"
    first = lock.open("a+b")
    second = lock.open("a+b")
    windows.paths[first.fileno()] = windows.paths[second.fileno()] = str(lock)
    try:
        file_lock.lock_exclusive(first)
        refused = 0

        def sleep(_seconds: float) -> None:
            nonlocal refused
            refused += 1
            if refused == 3:
                file_lock.unlock(first)

        monkeypatch.setattr(file_lock.time, "sleep", sleep)
        file_lock.lock_exclusive(second)
        assert refused == 3
        assert windows.held[str(lock)] == second.fileno()
    finally:
        first.close()
        second.close()


def test_a_save_waits_while_another_program_has_the_file_open_on_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(file_lock.os, "name", "nt")
    monkeypatch.setattr(file_lock.time, "sleep", lambda _seconds: None)
    real_replace = file_lock.os.replace
    attempts = 0

    def replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError(13, "Access is denied")
            error.winerror = 5
            raise error
        real_replace(source, target)

    monkeypatch.setattr(file_lock.os, "replace", replace)
    source, target = tmp_path / "a.tmp", tmp_path / "a.jsonl"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    file_lock.replace_file(source, target)
    assert attempts == 3
    assert target.read_text(encoding="utf-8") == "new"


def test_a_file_kept_open_still_fails_the_save_in_the_end(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(file_lock.os, "name", "nt")
    clock = iter(range(0, 100))
    monkeypatch.setattr(file_lock.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(file_lock.time, "sleep", lambda _seconds: None)

    def replace(_source, _target):
        error = PermissionError(13, "Access is denied")
        error.winerror = 5
        raise error

    monkeypatch.setattr(file_lock.os, "replace", replace)
    with pytest.raises(PermissionError):
        file_lock.replace_file(tmp_path / "a.tmp", tmp_path / "a.jsonl")


def test_the_session_save_and_the_cron_store_use_the_waiting_swap() -> None:
    import inspect

    from flowly.cron import service as cron_service

    assert "replace_file(" in inspect.getsource(session_manager.SessionManager._save_unlocked)
    assert "replace_file(" in inspect.getsource(cron_service._write_durably)


def _hold(path: str, started, release) -> None:
    with session_file_lock(Path(path)):
        started.set()
        release.wait(10)


def test_the_lock_holds_across_processes(tmp_path: Path) -> None:
    """The real lock of this platform, between two processes (flock here, msvcrt on Windows)."""
    path = tmp_path / "sessions" / "desktop_profile-home.jsonl"
    path.parent.mkdir()
    manager = SessionManager(tmp_path, sessions_dir=path.parent)
    session = manager.get_or_create("desktop:profile-home")
    session.add_message("assistant", "first words")
    context = multiprocessing.get_context("spawn")
    started, release = context.Event(), context.Event()
    holder = context.Process(target=_hold, args=(str(path), started, release))
    holder.start()
    try:
        assert started.wait(30)

        def let_go() -> None:
            time.sleep(0.4)
            release.set()

        import threading

        threading.Thread(target=let_go, daemon=True).start()
        began = time.monotonic()
        manager.save(session)
        assert time.monotonic() - began >= 0.3, "the save did not wait for the other process"
    finally:
        release.set()
        holder.join(10)
