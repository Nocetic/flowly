"""Real kernel locks coordinate separate processes using the same environment."""

import os
import subprocess
import sys

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.installation_gate import environment_activity

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX screen environment locks')


def test_multiple_screens_share_activity_but_exclude_installation():
    with environment_activity():
        with environment_activity():
            with pytest.raises(ScreenError, match='INSTALL_BUSY'):
                with environment_activity(exclusive=True):
                    pytest.fail('maintenance entered a live environment')
    with environment_activity(exclusive=True):
        with pytest.raises(ScreenError, match='INSTALL_BUSY'):
            with environment_activity():
                pytest.fail('screen entered maintenance')


def test_process_exit_releases_only_the_live_lock():
    source = '''
from flowly.desktop_runtime.installation_gate import environment_activity
with environment_activity(exclusive=True):
    print('ready', flush=True)
    input()
'''
    child = subprocess.Popen([sys.executable, '-c', source], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(ScreenError, match='INSTALL_BUSY'):
            with environment_activity():
                pytest.fail('cross-process lease was ignored')
    finally:
        child.kill()
        child.wait(timeout=5)
    with environment_activity():
        pass  # Durable job recovery is deliberately a separate invariant.
