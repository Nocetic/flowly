"""Permission-aware ElevenLabs connection health checks."""

from __future__ import annotations

import asyncio

from flowly.integrations import probes


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return self.response


def _run(monkeypatch, response: _Response):
    monkeypatch.setattr(
        probes.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response),
    )
    return asyncio.run(probes.probe_elevenlabs({
        "enabled": True,
        "api_key": "restricted-key",
    }))


def test_restricted_key_without_user_read_is_valid(monkeypatch):
    result = _run(monkeypatch, _Response(401, {
        "detail": {
            "status": "missing_permissions",
            "message": "The API key is missing user_read.",
        },
    }))

    assert result.status == "ok"
    assert result.detail == "key valid · limited API permissions"


def test_invalid_key_is_still_rejected(monkeypatch):
    result = _run(monkeypatch, _Response(401, {
        "detail": {"status": "invalid_api_key"},
    }))

    assert result.status == "auth_failed"
    assert result.detail == "API key rejected"


def test_forbidden_key_reports_permissions_or_ip_allowlist(monkeypatch):
    result = _run(monkeypatch, _Response(403, {
        "detail": {"status": "forbidden"},
    }))

    assert result.status == "auth_failed"
    assert "permissions or IP allowlist" in result.detail


def test_full_account_access_reports_subscription(monkeypatch):
    result = _run(monkeypatch, _Response(200, {
        "subscription": {"tier": "creator"},
    }))

    assert result.status == "ok"
    assert result.detail == "connected · creator"
