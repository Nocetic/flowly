"""Live-catalog behaviour for the ChatGPT subscription provider."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import flowly.integrations.model_catalog as catalog
from flowly import __version__
from flowly.auth import openai_codex


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://chatgpt.com/backend-api/codex/models")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("catalogue failed", request=request, response=response)


def _credentials(token: str = "token") -> SimpleNamespace:
    return SimpleNamespace(
        api_key=token,
        account_id="acct-1",
        base_url="https://chatgpt.com/backend-api/codex",
    )


def test_codex_payload_maps_visible_models_in_priority_order():
    models = catalog._codex_models_from_payload(
        {
            "models": [
                {
                    "slug": "gpt-5.6-terra",
                    "display_name": "GPT-5.6-Terra",
                    "description": "Balanced agentic coding model.",
                    "visibility": "list",
                    "priority": 2,
                    "context_window": 272_000,
                    "input_modalities": ["text"],
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "codex-auto-review",
                    "visibility": "hide",
                    "priority": 3,
                },
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "description": "Latest frontier agentic coding model.",
                    "visibility": "list",
                    "priority": 1,
                    "context_window": 272_000,
                    "input_modalities": ["text", "image"],
                    "supported_reasoning_levels": [{"effort": "low"}],
                },
            ],
        }
    )

    assert [model.id for model in models] == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert models[0].name == "GPT-5.6-Sol"
    assert models[0].context_window == 272_000
    assert models[0].supports_vision is True
    assert models[0].tags == ["reasoning", "vision"]
    assert models[1].supports_vision is False


@pytest.mark.asyncio
async def test_codex_fetch_uses_oauth_headers_and_required_client_version(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, params, headers):
            captured.update(url=url, params=params, headers=headers)
            return _FakeResponse(
                200,
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-sol",
                            "display_name": "GPT-5.6-Sol",
                            "visibility": "list",
                            "priority": 1,
                        }
                    ],
                },
            )

    monkeypatch.setattr(openai_codex, "resolve_runtime_credentials", _credentials)
    monkeypatch.setattr(catalog.httpx, "AsyncClient", FakeClient)

    models = await catalog._fetch_openai_codex()

    assert [model.id for model in models] == ["gpt-5.6-sol"]
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/models"
    assert captured["params"] == {"client_version": __version__}
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["headers"]["ChatGPT-Account-Id"] == "acct-1"


@pytest.mark.asyncio
async def test_codex_fetch_refreshes_once_after_401(monkeypatch):
    credential_calls: list[bool] = []
    auth_headers: list[str] = []
    responses = [
        _FakeResponse(401, {"error": {"message": "expired"}}),
        _FakeResponse(
            200,
            {
                "models": [
                    {
                        "slug": "gpt-5.6-terra",
                        "visibility": "list",
                        "priority": 1,
                    }
                ]
            },
        ),
    ]

    def resolve(*, force_refresh: bool = False):
        credential_calls.append(force_refresh)
        return _credentials("fresh" if force_refresh else "expired")

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, params, headers):
            auth_headers.append(headers["Authorization"])
            return responses.pop(0)

    monkeypatch.setattr(openai_codex, "resolve_runtime_credentials", resolve)
    monkeypatch.setattr(catalog.httpx, "AsyncClient", FakeClient)

    models = await catalog._fetch_openai_codex()

    assert [model.id for model in models] == ["gpt-5.6-terra"]
    assert credential_calls == [False, True]
    assert auth_headers == ["Bearer expired", "Bearer fresh"]


@pytest.mark.asyncio
async def test_codex_fetch_uses_static_fallback_on_network_failure(monkeypatch):
    class FailingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, params, headers):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(openai_codex, "resolve_runtime_credentials", _credentials)
    monkeypatch.setattr(catalog.httpx, "AsyncClient", FailingClient)

    models = await catalog._fetch_openai_codex()

    assert [model.id for model in models[:3]] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]


@pytest.mark.asyncio
async def test_codex_fetch_stays_empty_until_signed_in(monkeypatch):
    monkeypatch.setattr(openai_codex, "resolve_runtime_credentials", lambda: None)

    models = await catalog._fetch_openai_codex()

    assert models == []
