"""Cron push notification helpers."""

from __future__ import annotations

import asyncio

import pytest

from flowly.cli.gateway_cmd import _schedule_cron_push_notification


class _Job:
    id = "job-1"
    name = "Daily report"


@pytest.mark.asyncio
async def test_schedule_cron_push_without_chat_target(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    _schedule_cron_push_notification(_Job(), "first line\nsecond line")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [{
        "title": "Daily report",
        "body": "first line",
        "conversation_id": "",
        "data": {
            "type": "cron",
            "jobId": "job-1",
            "jobName": "Daily report",
        },
    }]


@pytest.mark.asyncio
async def test_chat_origin_cron_push_keeps_conversation_target(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    _schedule_cron_push_notification(
        _Job(),
        "chat result",
        conversation_id="ios:chat-1",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [{
        "title": "Daily report",
        "body": "chat result",
        "conversation_id": "ios:chat-1",
        "data": {
            "type": "cron",
            "jobId": "job-1",
            "jobName": "Daily report",
        },
    }]
