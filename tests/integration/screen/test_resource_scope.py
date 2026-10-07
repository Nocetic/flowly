"""Opt-in real systemd/cgroup v2 tests, only inside a disposable host."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flowly.desktop_runtime.resource_scope import MEMORY_MAX, scoped_command, stop_scope

pytestmark = pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_SCOPE_TEST") != "1",
                                reason="requires disposable systemd host")


def wait_for(path, process):
    deadline = time.monotonic() + 15
    while not path.exists():
        assert process.poll() is None, "scope exited before reporting kernel limits"
        assert time.monotonic() < deadline
        time.sleep(0.05)


def launch(directory, generation, script):
    command, enabled = scoped_command([sys.executable, "-c", script, str(directory), generation], directory, generation)
    assert enabled
    return subprocess.Popen(command, env={**os.environ, "PYTHONPATH": "/workspace"})


def test_oom_in_one_screen_does_not_kill_agent_or_sibling(tmp_path):
    membership = Path("/proc/self/cgroup").read_text()
    sibling_dir, hungry_dir = tmp_path / "sibling", tmp_path / "hungry"
    sibling_dir.mkdir()
    hungry_dir.mkdir()
    prelude = '''
import json, sys, time
from pathlib import Path
from flowly.desktop_runtime.resource_scope import verify_scope
d=Path(sys.argv[1])
limits=verify_scope(d, sys.argv[2])
(d/'ready').write_text(json.dumps(limits))
'''
    sibling = launch(sibling_dir, "b" * 32, prelude + "time.sleep(90)")
    hungry = None
    try:
        wait_for(sibling_dir / "ready", sibling)
        # Touch every allocation. This child must hit its own kernel limit,
        # not a Python address-space limit or the parent's container budget.
        # First verify the shipped high watermark, then disable only reclaim
        # throttling in this disposable root child to reach the hard ceiling
        # quickly. Otherwise memory.high intentionally stalls this allocator.
        hungry = launch(hungry_dir, "a" * 32, prelude + '''
group=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().strip().split('::')[1].lstrip('/')
(group/'memory.high').write_text('max')
chunks=[]
while True: chunks.append(bytearray(32*1024*1024))
''')
        wait_for(hungry_dir / "ready", hungry)
        assert json.loads((hungry_dir / "ready").read_text())["memoryMaxBytes"] == MEMORY_MAX
        assert hungry.wait(timeout=60) == -9, "kernel OOM must kill the allocator"
        assert sibling.poll() is None
        assert Path("/proc/self/cgroup").read_text() == membership
    finally:
        stop_scope(sibling_dir, "b" * 32)
        stop_scope(hungry_dir, "a" * 32)
        sibling.wait(timeout=10)
        if hungry is not None:
            hungry.wait(timeout=10)


def test_stop_reaps_double_forked_descendants_and_is_idempotent(tmp_path):
    # A Terminal job may close FDs, fork twice and start its own session. It
    # still belongs to the scope; numeric PID ownership is not used to kill it.
    script = '''
import os, sys, time
from pathlib import Path
d=Path(sys.argv[1])
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0: os._exit(0)
    for fd in (0,1,2):
        try: os.close(fd)
        except OSError: pass
    (d/'daemon').write_text(str(os.getpid()))
    time.sleep(90)
else:
    time.sleep(90)
'''
    process = launch(tmp_path, "c" * 32, script)
    try:
        wait_for(tmp_path / "daemon", process)
        pid = int((tmp_path / "daemon").read_text())
        stop_scope(tmp_path, "c" * 32)
        process.wait(timeout=10)
        deadline = time.monotonic() + 5
        while Path(f"/proc/{pid}").exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        stop_scope(tmp_path, "c" * 32)
    finally:
        stop_scope(tmp_path, "c" * 32)
        process.wait(timeout=10)


@pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_SCOPE_RUNTIME_TEST") != "1",
                    reason="requires real desktop packages and an unprivileged user manager")
def test_unprivileged_runtime_has_a_private_memory_scope(tmp_path):
    from flowly.desktop_runtime.launcher import ScreenRuntime

    assert os.geteuid() != 0
    runtime = ScreenRuntime(tmp_path / "desktop")
    state = runtime.start()
    try:
        assert state["resourceLimits"]["enforced"]
        assert state["resourceLimits"]["memoryMaxBytes"] == MEMORY_MAX
        membership = Path(f"/proc/{state['supervisor']['pid']}/cgroup").read_text()
        assert membership != Path("/proc/self/cgroup").read_text()
        for child in state["children"]:
            assert Path(f"/proc/{child['pid']}/cgroup").read_text() == membership
    finally:
        runtime.stop(state["generation"])
