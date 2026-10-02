"""Exec-approval push notifications."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from flowly.push.approval_push import notify_approval_requested


@dataclass
class _Request:
    command: str = "rm -rf ./build"


@dataclass
class _Pending:
    id: str = "a_1"
    request: _Request = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.request is None:
            self.request = _Request()


@pytest.mark.asyncio
async def test_approval_push_payload(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    await notify_approval_requested(_Pending())

    # Says that a decision is needed, never what: no command text leaves the agent.
    assert calls == [{
        "title": "Approval needed",
        "body": "Flowly wants to take an action. Open Flowly to review it.",
        "conversation_id": "",
        "data": {
            "type": "approval",
            "id": "a_1",
            "eventKey": "approval:a_1",
        },
    }]
    assert "rm -rf" not in str(calls)


@pytest.mark.asyncio
async def test_approval_push_names_a_command_without_showing_it(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    pending = _Pending(id="a_2", request=_Request(command="curl -H 'Authorization: Bearer s3cret' x"))
    pending.kind = "exec"  # type: ignore[attr-defined]
    await notify_approval_requested(pending)

    assert calls[0]["body"] == "Flowly wants to run a command. Open Flowly to review it."
    assert calls[0]["data"]["id"] == "a_2"
    assert "s3cret" not in str(calls)


@pytest.mark.asyncio
async def test_approval_push_never_raises(monkeypatch) -> None:
    async def boom(*args, **kwargs) -> None:
        raise RuntimeError("relay down")

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", boom)
    # Best-effort: a relay failure must not propagate into the approval wait.
    await notify_approval_requested(_Pending())
