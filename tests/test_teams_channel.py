"""Tests for the Faz 1 Microsoft Teams channel (incoming webhook).

HTTP is faked with ``httpx.MockTransport`` so no network is touched.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.teams import TeamsChannel
from flowly.config.schema import TeamsConfig


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_channel(
    webhook_url: str = "https://outlook.office.com/webhook/abc123",
    transport: httpx.MockTransport | None = None,
) -> tuple[TeamsChannel, list[httpx.Request]]:
    """Construct a TeamsChannel wired against a MockTransport."""
    captured: list[httpx.Request] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="1")

    config = TeamsConfig(enabled=True, webhook_url=webhook_url)
    bus = MessageBus()
    channel = TeamsChannel(config, bus)

    async def _start_with_mock():
        await channel.start()
        # Swap the real httpx client for one bound to the mock transport.
        if channel._client is not None:
            await channel._client.aclose()
        channel._client = httpx.AsyncClient(
            transport=transport or httpx.MockTransport(default_handler),
            timeout=20.0,
        )

    _run(_start_with_mock())
    return channel, captured


def _outbound(text: str = "hello", media: list[str] | None = None, metadata: dict | None = None) -> OutboundMessage:
    return OutboundMessage(
        channel="teams",
        chat_id="default",
        content=text,
        media=media or [],
        metadata=metadata or {},
    )


# ---- start() guards -----------------------------------------------------


def test_disabled_config_does_not_start():
    config = TeamsConfig(enabled=True, webhook_url="")  # empty url
    channel = TeamsChannel(config, MessageBus())
    _run(channel.start())
    assert channel.is_running is False
    _run(channel.stop())


def test_non_https_webhook_refused():
    config = TeamsConfig(enabled=True, webhook_url="http://insecure.example/hook")
    channel = TeamsChannel(config, MessageBus())
    _run(channel.start())
    assert channel.is_running is False
    _run(channel.stop())


def test_valid_https_webhook_starts():
    channel, _ = _make_channel()
    try:
        assert channel.is_running is True
    finally:
        _run(channel.stop())


# ---- send() payload shape ----------------------------------------------


def test_send_plain_text_posts_to_webhook():
    channel, captured = _make_channel()
    try:
        _run(channel.send(_outbound("Hello Teams")))
    finally:
        _run(channel.stop())

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url == httpx.URL("https://outlook.office.com/webhook/abc123")
    body = json.loads(req.content.decode())
    assert body == {"text": "Hello Teams"}


def test_send_includes_media_urls_as_markdown_list():
    channel, captured = _make_channel()
    try:
        _run(channel.send(_outbound(
            "Report ready",
            media=[
                "https://cdn.example.com/users/u1/uploads/abc.png",
                "https://cdn.example.com/users/u1/uploads/def.mp4",
            ],
        )))
    finally:
        _run(channel.stop())

    body = json.loads(captured[0].content.decode())
    text = body["text"]
    assert "Report ready" in text
    assert "**Attachments**" in text
    assert "- https://cdn.example.com/users/u1/uploads/abc.png" in text
    assert "- https://cdn.example.com/users/u1/uploads/def.mp4" in text


def test_send_drops_local_file_paths_from_media():
    """Local disk paths are unreachable from Teams; only http(s) URLs go."""
    channel, captured = _make_channel()
    try:
        _run(channel.send(_outbound(
            "Mixed media",
            media=[
                "/Users/foo/local/file.png",      # dropped
                "https://cdn.example.com/x.png",  # kept
            ],
        )))
    finally:
        _run(channel.stop())

    text = json.loads(captured[0].content.decode())["text"]
    assert "/Users/foo/local/file.png" not in text
    assert "https://cdn.example.com/x.png" in text


def test_send_metadata_attachments_merge_unique():
    channel, captured = _make_channel()
    try:
        _run(channel.send(_outbound(
            "via metadata",
            media=["https://cdn.example.com/a.png"],
            metadata={"teams": {"attachments": [
                "https://cdn.example.com/a.png",  # duplicate — should dedupe
                "https://cdn.example.com/b.png",  # new
            ]}},
        )))
    finally:
        _run(channel.stop())

    text = json.loads(captured[0].content.decode())["text"]
    # Each URL appears exactly once.
    assert text.count("a.png") == 1
    assert text.count("b.png") == 1


def test_send_empty_message_skips_request():
    channel, captured = _make_channel()
    try:
        _run(channel.send(_outbound(text="", media=[])))
    finally:
        _run(channel.stop())
    assert captured == []


def test_send_without_start_logs_but_does_not_raise():
    """send() before start() must fail soft so the dispatcher keeps moving."""
    config = TeamsConfig(enabled=True, webhook_url="https://outlook.office.com/webhook/x")
    channel = TeamsChannel(config, MessageBus())
    # No start() call
    _run(channel.send(_outbound("hello")))  # should not raise


# ---- retry / error handling --------------------------------------------


def test_5xx_triggers_retry_then_gives_up():
    """Two 5xx responses → caller doesn't see an exception, logs it."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(503, text="busy")

    transport = httpx.MockTransport(handler)
    channel, _ = _make_channel(transport=transport)
    try:
        # Force the mock to take over the freshly-replaced client.
        _run(channel.send(_outbound("retry me")))
    finally:
        _run(channel.stop())

    assert call_count[0] == 2  # 1 try + 1 retry


def test_5xx_then_200_succeeds_on_retry():
    responses = iter([
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, text="1"),
    ])
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return next(responses)

    transport = httpx.MockTransport(handler)
    channel, _ = _make_channel(transport=transport)
    try:
        _run(channel.send(_outbound("recover me")))
    finally:
        _run(channel.stop())

    assert call_count[0] == 2


def test_4xx_does_not_retry():
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(400, text="bad payload")

    transport = httpx.MockTransport(handler)
    channel, _ = _make_channel(transport=transport)
    try:
        _run(channel.send(_outbound("dont retry")))
    finally:
        _run(channel.stop())

    assert call_count[0] == 1
