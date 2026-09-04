"""What crosses the wire when an RPC fails unexpectedly.

Sending a message to a bot once answered with

    [Errno 1] Operation not permitted: '/Users/hakanoren/.flowly/config.json'

which is an errno and somebody's home directory, in place of anything they
could act on. Worse, the log recorded the same sentence and no traceback — so
the failure named a path and never said which line reached for it, and there
was nothing to diagnose from.

Every deliberate failure in this handler reports itself through
`_ws_rpc_error` with its own code, so the catch-all only ever sees the
unexpected: there is no curated message for it to swallow.
"""
from __future__ import annotations

import pytest

from flowly.gateway.server import GatewayServer


@pytest.mark.asyncio
async def test_an_unexpected_failure_reaches_the_client_without_the_exception(monkeypatch):
    server = GatewayServer(host="127.0.0.1", port=0)
    sent: list[tuple[str, str, str]] = []

    async def capture(_ws, rpc_id, code, message):
        sent.append((rpc_id, code, message))

    monkeypatch.setattr(server, "_ws_rpc_error", capture)

    async def explode(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted", "/Users/someone/.flowly/config.json")

    monkeypatch.setattr(server, "_ws_rpc_reply", explode)

    await server._handle_ws_rpc(object(), "client-1", {"method": "health", "id": "r1"})

    assert len(sent) == 1
    rpc_id, code, message = sent[0]
    assert rpc_id == "r1"
    assert code == "UNAVAILABLE"
    # Neither the errno nor the path may survive the trip.
    assert "Errno" not in message
    assert ".flowly" not in message
    assert "/Users/" not in message
    assert message


@pytest.mark.asyncio
async def test_a_deliberate_failure_keeps_its_own_words(monkeypatch):
    # An unknown method reports itself above the catch-all, so its message and
    # code must be exactly what that branch chose.
    server = GatewayServer(host="127.0.0.1", port=0)
    sent: list[tuple[str, str, str]] = []

    async def capture(_ws, rpc_id, code, message):
        sent.append((rpc_id, code, message))

    monkeypatch.setattr(server, "_ws_rpc_error", capture)
    await server._handle_ws_rpc(object(), "client-1", {"method": "nope", "id": "r2"})

    assert sent == [("r2", "INVALID_REQUEST", "Unknown method: nope")]
