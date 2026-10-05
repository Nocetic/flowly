"""Root-only path checks accept a root-group-writable ancestor ('/' is 775 on
some Fedora images, seen live) and nothing a non-root user could change."""

import os
import stat
import types
from pathlib import Path

import pytest

from flowly.desktop_runtime import runtime_account
from flowly.desktop_runtime.install_recipe import BROWSER_SOURCE, RECIPE_SOURCE
from flowly.desktop_runtime.runtime_account_payload import PROVISION_SOURCE


def fake(mode, uid=0, gid=0):
    return os.stat_result((stat.S_IFDIR | mode, 0, 0, 2, uid, gid, 0, 0, 0, 0))


@pytest.mark.parametrize("root,accepted", [
    (fake(0o555), True), (fake(0o755), True),
    (fake(0o775), True),              # group write, group root: only root is in it
    (fake(0o775, gid=1000), False),   # group write for an ordinary group
    (fake(0o757), False),             # anyone may write
    (fake(0o1777), False),            # /tmp-like
    (fake(0o755, uid=1000), False),   # not root's
])
def test_every_ancestor_must_be_changeable_by_root_alone(monkeypatch, root, accepted):
    def lstat(path):
        return root if str(path) == "/" else fake(0o755)
    monkeypatch.setattr(Path, "lstat", lstat)
    worker = types.ModuleType("checks")
    exec(compile(PROVISION_SOURCE + RECIPE_SOURCE + BROWSER_SOURCE, "<checks>", "exec"), worker.__dict__)
    checks = [lambda: runtime_account._trusted_root(Path("/var/lib/flowly-screen-runtime")),
              lambda: worker.account_directory(Path("/var/lib/flowly-screen-runtime")),
              lambda: worker._root_directory(Path("/opt/flowly-screen/chromium"))]
    for check in checks:
        if accepted:
            check()
        else:
            with pytest.raises(Exception):
                check()
