"""Anonymous relay-push registry behaviour."""

from __future__ import annotations

import pytest

from flowly.push import relay_push


@pytest.mark.asyncio
async def test_notify_devices_maps_gateway_and_relay_ids(monkeypatch, tmp_path) -> None:
    reg = relay_push.PushRegistry(tmp_path / "push_subs.json")
    reg.register(push_id="p_gateway", push_secret="s1", gateway_id="gw-1", kind="gateway")
    reg.register(push_id="p_relay", push_secret="s2", gateway_id="srv-1", kind="relay")

    sent: list[dict] = []

    def fake_send_one(base: str, sub: dict, title: str, body: str, data: dict) -> int:
        sent.append({
            "base": base,
            "pushId": sub["pushId"],
            "title": title,
            "body": body,
            "data": data,
        })
        return 200

    monkeypatch.setattr(relay_push, "_registry", reg)
    monkeypatch.setattr(relay_push, "_relay_base", lambda: "https://relay.test")
    monkeypatch.setattr(relay_push, "_send_one", fake_send_one)

    await relay_push.notify_devices("Board · Task", "done", data={"type": "board"})

    by_push_id = {row["pushId"]: row for row in sent}
    assert by_push_id["p_gateway"]["data"] == {
        "type": "board",
        "gatewayId": "gw-1",
    }
    assert by_push_id["p_relay"]["data"] == {
        "type": "board",
        "serverId": "srv-1",
    }
