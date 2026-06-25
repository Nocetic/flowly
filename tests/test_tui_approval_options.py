"""The TUI approval prompt must hide "Always allow" when the backend says the
decision can't be remembered (supports_always=False)."""

from __future__ import annotations

import asyncio

import pytest

from flowly.tui.client import ApprovalRequest, GatewayClient
from flowly.tui.panes.composer import APPROVAL_OPTIONS, visible_approval_options


def test_visible_options_include_always_when_supported():
    opts = visible_approval_options(True)
    assert opts == APPROVAL_OPTIONS
    assert any(o.decision == "allow-always" for o in opts)


def test_visible_options_drop_always_when_unsupported():
    opts = visible_approval_options(False)
    decisions = [o.decision for o in opts]
    assert decisions == ["allow-once", "deny"]
    assert all(o.decision != "allow-always" for o in opts)


@pytest.mark.asyncio
async def test_client_parses_supports_always_false():
    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()
    await client._dispatch(
        {
            "type": "event",
            "event": "exec.approval.requested",
            "data": {
                "id": "a1",
                "command": "📧 Send email to a@b.com",
                "sessionKey": "tui:default",
                "supportsAlways": False,
            },
        }
    )
    ev = await client._inbox.get()
    assert isinstance(ev, ApprovalRequest)
    assert ev.supports_always is False


@pytest.mark.asyncio
async def test_client_defaults_supports_always_true_when_absent():
    # Older gateways don't send the flag — must default to True (back-compat).
    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()
    await client._dispatch(
        {
            "type": "event",
            "event": "exec.approval.requested",
            "data": {"id": "a2", "command": "git push", "sessionKey": "tui:default"},
        }
    )
    ev = await client._inbox.get()
    assert ev.supports_always is True
