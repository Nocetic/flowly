"""Global test isolation.

Unit tests must never download model assets, and must never read or write the
real Flowly home. Before anything imports ``flowly`` (pytest loads this file
first), the session gets a home of its own: ``HOME`` and ``FLOWLY_HOME`` point
into a temporary directory, so ``Path.home() / ".flowly"`` (the fallback every
path helper ends at) is that directory too. A test that forgets to isolate
itself then writes there, not into the owner's agents. ``test_test_isolation``
checks this holds.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

os.environ.setdefault("FLOWLY_SEMANTIC_MODEL_DOWNLOAD", "0")

_SESSION_HOME = tempfile.mkdtemp(prefix="flowly-tests-home-")
os.environ["HOME"] = _SESSION_HOME
os.environ["FLOWLY_HOME"] = os.path.join(_SESSION_HOME, ".flowly")
atexit.register(shutil.rmtree, _SESSION_HOME, True)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_push_notifications():
    """Each test starts with no pushed or scheduled notification events."""
    from flowly.push import notifications

    notifications._reset_for_tests()
    yield
    notifications._reset_for_tests()
