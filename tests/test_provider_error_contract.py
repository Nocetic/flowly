"""User-safe, structured provider error handling across agent and gateway."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from flowly.gateway.auth import loopback_ws_allowed

from flowly.agent.error_classifier import (
    ErrorCategory,
    backoff_for,
    classify_response,
    is_image_input_unsupported,
    present_provider_error,
)
from flowly.agent.loop import (
    _browser_tool_result_failed,
    _messages_contain_image_input,
)
from flowly.gateway.server import GatewayServer
from flowly.integrations import model_catalog
from flowly.integrations.model_catalog import Model
from flowly.providers.base import LLMResponse


def _error(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="error")


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 404 - {'error': {'message': 'No endpoints found that support image input'}}",
        "This model does not support image input.",
        "image input is not supported for this deployment",
        "Unsupported content type: image",
    ],
)
def test_image_input_errors_are_terminal_and_provider_independent(message: str) -> None:
    response = _error(message)
    assert is_image_input_unsupported(message)
    assert classify_response(response) is ErrorCategory.IMAGE_INPUT_UNSUPPORTED
    assert backoff_for(ErrorCategory.IMAGE_INPUT_UNSUPPORTED, 1) is None


@pytest.mark.parametrize(
    "result",
    [
        '{"error": "Browser provider does not support action: evaluate", "error_code": "UNSUPPORTED_ACTION"}',
        '{"error_code": "TYPE_NOT_PERSISTED", "observed": ""}',
        '{"error": "Element not found or no longer visible"}',
        '{"error_code": "UPLOAD_PATH_NOT_ALLOWED"}',
    ],
)
def test_browser_tab_error_envelope_counts_as_failure(result: str) -> None:
    # The generic loop check would call these successes (they don't start with
    # "Error"); the browser-scoped detector must flag them as failures.
    assert not result.startswith("Error")
    assert _browser_tool_result_failed("browser_tab", result) is True


@pytest.mark.parametrize(
    "result",
    [
        '{"success": true, "ref": "ref_8", "textLength": 7, "verified": true}',
        '{"success": true, "tabId": 4, "url": "https://x.com/"}',
        # A success envelope that happens to carry an inner "error" field must
        # still be a success.
        '{"success": true, "error": "harmless subfield"}',
    ],
)
def test_browser_tab_success_stays_success(result: str) -> None:
    assert _browser_tool_result_failed("browser_tab", result) is False


def test_browser_failure_detector_is_scoped_to_browser_tab() -> None:
    # Another tool returning JSON with an "error" field must be untouched.
    payload = '{"error": "some other tool payload"}'
    assert _browser_tool_result_failed("exec", payload) is False
    # Non-JSON browser results are left to the generic check.
    assert _browser_tool_result_failed("browser_tab", "plain text result") is False


def _req(origin: str | None, host: str, url_host: str | None) -> Any:
    headers: dict[str, str] = {}
    if origin is not None:
        headers["Origin"] = origin
    return SimpleNamespace(headers=headers, host=host, url=SimpleNamespace(host=url_host))


@pytest.mark.parametrize(
    "req",
    [
        # Native local clients: desktop main-process ws / TUI (no Origin), the
        # extension (chrome-extension://), a file:// renderer, and a same-host
        # localhost web origin (dev renderer).
        _req(None, "localhost:18790", "localhost"),
        _req(None, "127.0.0.1:18790", "127.0.0.1"),
        _req("chrome-extension://abcdefg", "localhost:18790", "localhost"),
        _req("file://", "localhost:18790", "localhost"),
        _req("http://localhost:5173", "localhost:18790", "localhost"),
    ],
)
def test_loopback_ws_allows_legit_local_clients(req: Any) -> None:
    assert loopback_ws_allowed(req) is True


@pytest.mark.parametrize(
    "req",
    [
        # A web page the embedded browser visits scripting a cross-origin WS.
        _req("https://evil.com", "localhost:18790", "localhost"),
        # DNS rebinding: Origin and Host match (passes the origin check) but the
        # Host is not loopback → rejected.
        _req("http://attacker.com", "attacker.com", "attacker.com"),
        _req("https://attacker.com", "attacker.com:18790", "attacker.com"),
    ],
)
def test_loopback_ws_blocks_web_origins_and_rebinding(req: Any) -> None:
    assert loopback_ws_allowed(req) is False


@pytest.mark.parametrize(
    "message",
    [
        "The image URL timed out",
        "Could not decode the attached image",
        "Vision service is temporarily overloaded",
        "No endpoints found for this model",
    ],
)
def test_image_classifier_avoids_broad_false_positives(message: str) -> None:
    assert not is_image_input_unsupported(message)


def test_image_error_presentation_never_leaks_raw_provider_payload() -> None:
    raw = (
        "Error calling LLM: Error code: 404 - {'error': {'message': "
        "'No endpoints found that support image input', 'debug': 'secret-route'}}"
    )
    presentation = present_provider_error(_error(raw))
    payload = presentation.as_dict()

    assert payload == {
        "code": "MODEL_IMAGE_INPUT_UNSUPPORTED",
        "title": "This model can't read images",
        "message": (
            "Choose a vision-capable model or remove the image, then try again. "
            "No action was taken."
        ),
        "retryable": False,
        "category": "image_input_unsupported",
    }
    assert "secret-route" not in str(payload)
    assert "404" not in str(payload)


def test_image_block_detection_handles_user_and_tool_shapes() -> None:
    assert _messages_contain_image_input([
        {"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]},
    ])
    assert _messages_contain_image_input([
        {"role": "tool", "content": [{"type": "input_image", "image_url": "https://x/img"}]},
    ])
    assert not _messages_contain_image_input([
        {"role": "user", "content": "the word image is only text"},
    ])


def test_cached_vision_support_is_explicit_and_true_wins(monkeypatch) -> None:
    monkeypatch.setattr(model_catalog, "_CACHE", {
        "flowly": [
            Model("vision/model", "Vision", supports_vision=True),
            Model("text/model", "Text", supports_vision=False),
            Model("unknown/model", "Unknown"),
        ],
        "openrouter": [Model("text/model", "Alias", supports_vision=True)],
    })

    assert model_catalog.get_vision_support("vision/model") is True
    assert model_catalog.get_vision_support("text/model") is True
    assert model_catalog.get_vision_support("unknown/model") is None
    assert model_catalog.get_vision_support("missing/model") is None


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_gateway_emits_native_error_event_without_raw_response() -> None:
    server = object.__new__(GatewayServer)
    server._session_ws = {}

    async def on_chat_message(*args: Any) -> tuple[str, dict[str, Any]]:
        return "raw response must not be sent", {
            "model": "text/model",
            "error": {
                "code": "MODEL_IMAGE_INPUT_UNSUPPORTED",
                "title": "This model can't read images",
                "message": "Choose a vision-capable model or remove the image, then try again.",
                "retryable": False,
            },
        }

    server.on_chat_message = on_chat_message
    server._schedule_offline_chat_push = lambda *args: None
    ws = _FakeWS()

    await server._run_chat(
        ws, "client", "desktop:chat", "look at this", "run-1",
        stream_callback=lambda delta: None,  # callback is not used by this fake
    )

    assert len(ws.sent) == 1
    data = ws.sent[0]["data"]
    assert data == {
        "state": "error",
        "runId": "run-1",
        "sessionKey": "desktop:chat",
        "model": "text/model",
        "errorCode": "MODEL_IMAGE_INPUT_UNSUPPORTED",
        "errorTitle": "This model can't read images",
        "errorMessage": "Choose a vision-capable model or remove the image, then try again.",
        "retryable": False,
    }
    assert "raw response" not in str(ws.sent)


@pytest.mark.asyncio
async def test_gateway_sanitizes_unexpected_exception_details() -> None:
    server = object.__new__(GatewayServer)
    server._session_ws = {}

    async def on_chat_message(*args: Any) -> tuple[str, dict[str, Any]]:
        raise RuntimeError("provider-token-and-private-path-must-not-leak")

    server.on_chat_message = on_chat_message
    server._schedule_offline_chat_push = lambda *args: None
    ws = _FakeWS()

    await server._run_chat(
        ws, "client", "desktop:chat", "hello", "run-2",
        stream_callback=lambda delta: None,
    )

    assert len(ws.sent) == 1
    data = ws.sent[0]["data"]
    assert data["state"] == "error"
    assert data["errorCode"] == "AGENT_INTERNAL_ERROR"
    assert data["retryable"] is True
    assert "provider-token" not in str(ws.sent)
