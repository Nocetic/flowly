"""register_machine() must send appVersion so the admin console can build a
client-version distribution. This locks the payload contract without hitting
the network — httpx is stubbed to capture the outgoing JSON body.
"""

from __future__ import annotations

from typing import Any

import flowly
from flowly.account import server as server_mod


class _FakeResponse:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {
            "server": {"id": "srv_test", "name": "test", "status": "active"},
            "gatewayAuthToken": "tok",
            "jwtSecret": "secret",
            "existing": False,
        }


class _FakeAsyncClient:
    """Captures the last POST payload into the class attribute `captured`."""

    captured: dict[str, Any] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> _FakeResponse:
        _FakeAsyncClient.captured = json
        return _FakeResponse()


async def test_register_machine_sends_app_version(monkeypatch):
    monkeypatch.setattr(server_mod.httpx, "AsyncClient", _FakeAsyncClient)

    await server_mod.register_machine("fake-id-token")

    payload = _FakeAsyncClient.captured
    assert payload is not None
    assert payload["appVersion"] == flowly.__version__
    assert payload["botType"] == "flowly"
    assert payload["provider"] == "desktop"
    assert "machineId" in payload
