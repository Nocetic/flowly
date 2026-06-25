"""M3 — under a service manager the gateway must not duplicate its full log into
the never-rotated flowly-gateway.err.log.

loguru's default stderr sink writes every INFO line to stderr, which launchd /
systemd redirect to flowly-gateway.err.log and append to forever (no rotation).
The rotated gateway.log already captures everything, so the gateway drops the
default stderr sink when stderr is redirected. This tests the decision predicate
(without mutating loguru's global handler state).
"""

from __future__ import annotations

import io

from flowly.cli.gateway_cmd import _should_drop_stderr_sink


class _Tty:
    def isatty(self):
        return True


class _NotTty:
    def isatty(self):
        return False


def test_drops_sink_when_stderr_is_a_redirected_file():
    # Service mode: stderr is a file → not a TTY → drop the duplicate sink.
    assert _should_drop_stderr_sink(_NotTty()) is True
    assert _should_drop_stderr_sink(io.StringIO()) is True


def test_keeps_sink_on_an_interactive_terminal():
    # Foreground `flowly gateway`: keep console output.
    assert _should_drop_stderr_sink(_Tty()) is False


def test_drops_sink_when_stderr_is_none():
    # pythonw / console-less hosts can leave sys.stderr as None.
    assert _should_drop_stderr_sink(None) is True
