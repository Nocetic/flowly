"""`flowly update` source-checkout path — git pull --ff-only + reinstall."""

from __future__ import annotations

import subprocess

import pytest

from flowly.cli import update_cmd


def _cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class _FakeGit:
    """Fake for ``update_cmd._git`` keyed on the git subcommand."""

    def __init__(self, *, branch="main", behind=0, dirty=False, unmerged=False,
                 fetch_rc=0, pull_rc=0, stash_pop_rc=0):
        self.branch, self.behind, self.dirty, self.unmerged = branch, behind, dirty, unmerged
        self.fetch_rc, self.pull_rc, self.stash_pop_rc = fetch_rc, pull_rc, stash_pop_rc
        self.calls: list[tuple] = []

    def __call__(self, repo, *args, capture=True):
        self.calls.append(args)
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return _cp(0, self.branch + "\n")
        if args[0] == "fetch":
            return _cp(self.fetch_rc)
        if args[:2] == ("rev-list", "--count"):
            return _cp(0, f"{self.behind}\n")
        if args[:2] == ("ls-files", "--unmerged"):
            return _cp(0, "x\n" if self.unmerged else "")
        if args[:2] == ("status", "--porcelain"):
            return _cp(0, " M f\n" if self.dirty else "")
        if args[:2] == ("stash", "push"):
            return _cp(0)
        if args[:2] == ("stash", "pop"):
            return _cp(self.stash_pop_rc)
        if args[0] == "pull":
            return _cp(self.pull_rc)
        return _cp(0)

    def ran(self, *prefix) -> bool:
        return any(c[: len(prefix)] == prefix for c in self.calls)


@pytest.fixture
def patched(monkeypatch, tmp_path):
    monkeypatch.setattr(update_cmd, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_reinstall_editable", lambda repo: 0)
    monkeypatch.setattr(update_cmd, "clear_pycache", lambda: None)
    monkeypatch.setattr(update_cmd, "_restart_gateway", lambda: None)
    yield


def _run(check_only=False, force=False, restart=False):
    return update_cmd._update_source(check_only=check_only, force=force, restart=restart)


def test_up_to_date_short_circuits(monkeypatch, patched):
    fake = _FakeGit(behind=0)
    monkeypatch.setattr(update_cmd, "_git", fake)
    assert _run() == 0
    assert not fake.ran("pull")


def test_behind_pulls_and_reinstalls(monkeypatch, patched):
    fake = _FakeGit(behind=3)
    calls = {"reinstall": 0, "restart": 0}
    monkeypatch.setattr(update_cmd, "_git", fake)
    monkeypatch.setattr(update_cmd, "_reinstall_editable", lambda r: calls.__setitem__("reinstall", calls["reinstall"] + 1) or 0)
    monkeypatch.setattr(update_cmd, "_restart_gateway", lambda: calls.__setitem__("restart", calls["restart"] + 1))
    assert _run(restart=True) == 0
    assert fake.ran("pull", "--ff-only")
    assert calls == {"reinstall": 1, "restart": 1}


def test_check_only_does_not_pull(monkeypatch, patched):
    fake = _FakeGit(behind=2)
    monkeypatch.setattr(update_cmd, "_git", fake)
    assert _run(check_only=True) == 0
    assert not fake.ran("pull")


def test_not_a_git_checkout(monkeypatch):
    monkeypatch.setattr(update_cmd, "_repo_root", lambda: None)
    assert _run() == 1


def test_detached_head_refuses(monkeypatch, patched):
    monkeypatch.setattr(update_cmd, "_git", _FakeGit(branch="HEAD"))
    assert _run() == 1


def test_dirty_tree_autostashes(monkeypatch, patched):
    fake = _FakeGit(behind=1, dirty=True)
    monkeypatch.setattr(update_cmd, "_git", fake)
    assert _run() == 0
    assert fake.ran("stash", "push")
    assert fake.ran("stash", "pop")


def test_force_reinstalls_even_when_current(monkeypatch, patched):
    fake = _FakeGit(behind=0)
    seen = {"reinstall": 0}
    monkeypatch.setattr(update_cmd, "_git", fake)
    monkeypatch.setattr(update_cmd, "_reinstall_editable", lambda r: seen.__setitem__("reinstall", 1) or 0)
    assert _run(force=True) == 0
    assert seen["reinstall"] == 1


def test_pull_failure_returns_nonzero(monkeypatch, patched):
    fake = _FakeGit(behind=1, pull_rc=1)
    monkeypatch.setattr(update_cmd, "_git", fake)
    assert _run() == 1


def test_fetch_failure_returns_nonzero(monkeypatch, patched):
    fake = _FakeGit(behind=1, fetch_rc=1)
    monkeypatch.setattr(update_cmd, "_git", fake)
    assert _run() == 1


def test_source_mode_routes_to_update_source(monkeypatch):
    monkeypatch.setattr(update_cmd, "detect_install_mode", lambda: "source")
    seen = {}
    monkeypatch.setattr(
        update_cmd, "_update_source",
        lambda **kw: seen.update(kw) or 0,
    )
    assert update_cmd.run_update(check_only=True, restart=False) == 0
    assert seen == {"check_only": True, "force": False, "restart": False}
