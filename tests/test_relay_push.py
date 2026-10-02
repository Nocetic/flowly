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


def _registry(monkeypatch, tmp_path, count: int) -> relay_push.PushRegistry:
    reg = relay_push.PushRegistry(tmp_path / "push_subs.json")
    for index in range(count):
        reg.register(push_id=f"push-{index}", push_secret=f"secret-{index}", gateway_id="srv-1", kind="relay")
    monkeypatch.setattr(relay_push, "get_push_registry", lambda: reg)
    monkeypatch.setattr(relay_push, "_relay_base", lambda: "https://relay.test")
    monkeypatch.setattr(relay_push, "RETRY_DELAYS", (0.01, 0.01))
    return reg


@pytest.mark.asyncio
async def test_a_push_the_proxy_could_not_forward_is_tried_again(monkeypatch, tmp_path) -> None:
    # 2026-10-02: nginx answered 502 to 14 of 19 pushes while the relay was
    # healthy (a dead IPv6 upstream); each was tried once and lost.
    reg = _registry(monkeypatch, tmp_path, 3)
    answers = {"push-0": [502, 200], "push-1": [0, 503, 200], "push-2": [504, 504, 504]}
    calls: dict[str, int] = {}

    def send(base, sub, title, body, data):
        calls[sub["pushId"]] = calls.get(sub["pushId"], 0) + 1
        return answers[sub["pushId"]].pop(0)

    monkeypatch.setattr(relay_push, "_send_one", send)
    summary = await relay_push.notify_devices("Approval needed", "Open Flowly")
    assert calls == {"push-0": 2, "push-1": 3, "push-2": 3}
    assert summary == {"sent": 2, "dropped": 0, "failed": 1}
    assert len(reg.list()) == 3


@pytest.mark.asyncio
async def test_an_answer_from_the_relay_is_never_retried(monkeypatch, tmp_path) -> None:
    reg = _registry(monkeypatch, tmp_path, 3)
    answers = {"push-0": 200, "push-1": 410, "push-2": 400}
    calls: list[str] = []

    def send(base, sub, title, body, data):
        calls.append(sub["pushId"])
        return answers[sub["pushId"]]

    monkeypatch.setattr(relay_push, "_send_one", send)
    summary = await relay_push.notify_devices("Board · Task", "done")
    assert sorted(calls) == ["push-0", "push-1", "push-2"]
    assert summary == {"sent": 1, "dropped": 1, "failed": 1}
    assert [sub["pushId"] for sub in reg.list()] == ["push-0", "push-2"]


@pytest.mark.asyncio
async def test_the_summary_line_holds_counts_only(monkeypatch, tmp_path) -> None:
    from loguru import logger

    _registry(monkeypatch, tmp_path, 2)
    monkeypatch.setattr(relay_push, "_send_one", lambda base, sub, title, body, data: 200 if sub["pushId"] == "push-0" else 502)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO", format="{message}")
    try:
        await relay_push.notify_devices("Secret title", "secret body", conversation_id="conv-9", data={"eventKey": "approval:a1"})
    finally:
        logger.remove(sink)
    summary = [line for line in lines if line.startswith("[push] ") and "sent" in line]
    assert summary == ["[push] 1/2 sent, 0 dropped, 1 failed (HTTP 502)\n"]
    assert not any(token in "".join(lines) for token in ("push-", "secret", "Secret", "conv-9", "approval:a1"))
