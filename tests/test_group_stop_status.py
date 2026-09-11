from __future__ import annotations

from flowly.profile_rooms import ProfileRoomService


def test_stopped_turn_ignores_even_an_earlier_completed_member_mention():
    room = {"messages": [
        {"role": "user", "content": "review", "createdAt": "2026-09-11T10:00:00Z"},
        {"role": "assistant", "content": "@user decide", "createdAt": "2026-09-11T10:00:01Z"},
        {"role": "assistant", "content": "partial", "aborted": True, "createdAt": "2026-09-11T10:00:02Z"},
    ]}
    assert not ProfileRoomService._needs_user(room)
    room["messages"].extend([
        {"role": "user", "content": "continue", "createdAt": "2026-09-11T10:01:00Z"},
        {"role": "assistant", "content": "@user which one?", "createdAt": "2026-09-11T10:01:01Z"},
    ])
    assert ProfileRoomService._needs_user(room)


def test_stop_before_checkpoint_suppresses_only_that_run():
    room = {"run": {"state": "aborted", "finishedAt": "2026-09-11T10:00:02Z"}, "messages": [
        {"role": "user", "content": "review", "createdAt": "2026-09-11T10:00:00Z"},
        {"role": "assistant", "content": "@user decide", "createdAt": "2026-09-11T10:00:01Z"},
    ]}
    assert not ProfileRoomService._needs_user(room)
    # A message addressed to nobody preserves the old run on the host.
    room["messages"].extend([
        {"role": "user", "content": "again", "createdAt": "2026-09-11T10:01:00Z"},
        {"role": "assistant", "content": "@user choose", "createdAt": "2026-09-11T10:01:01Z"},
    ])
    assert ProfileRoomService._needs_user(room)


def test_normal_pending_decision_still_works():
    assert ProfileRoomService._needs_user({"messages": [
        {"role": "assistant", "content": "@user choose"},
    ]})
    assert not ProfileRoomService._needs_user({"messages": []})


async def test_stop_snapshot_live_patch_reload_and_real_requests(tmp_path):
    import asyncio

    async def rpc(profile, method, params, timeout):
        return {"runId": f"status-{profile}"} if method == "chat.send" else {"ok": True}

    events = []
    service = ProfileRoomService(
        target_rpc=rpc, profile_directory=lambda: ["default", "writer"],
        on_event=events.append, store_path=tmp_path / "rooms.json",
    )
    room_id = (await service.create("Review", ["default", "writer"]))["id"]
    await service.send(room_id, "@writer review")
    for _ in range(100):
        if service._waiters:
            break
        await asyncio.sleep(0.01)
    assert service._waiters
    envelope = {"sessionKey": f"desktop:profile-room:{room_id}", "runId": "status-writer"}
    await service.handle_profile_event("writer", "agent", {
        **envelope, "stream": "assistant", "data": {"text": "@user choose a path"},
    })
    await service.stop(room_id)
    for _ in range(100):
        if not service._tasks:
            break
        await asyncio.sleep(0.01)
    assert not service._tasks
    room = service._rooms[room_id]
    for snapshot in (service._public(room), service._public(room, include_messages=False), service._live_patch(room_id)):
        assert snapshot["needsUser"] is False
        assert snapshot["running"] is False
        assert snapshot["attentions"] == []
    assert any(event.get("type") == "run-state" for event in events)
    reloaded = ProfileRoomService(
        target_rpc=rpc, profile_directory=lambda: ["default", "writer"],
        on_event=None, store_path=tmp_path / "rooms.json",
    )
    assert (await reloaded.list())[0]["needsUser"] is False
    # An actual actionable request outranks the generic mention heuristic.
    service._attentions[room_id] = {"request-1": {
        "id": "request-1", "profile": "writer", "kind": "clarify",
        "request": {"question": "Which file?"},
    }}
    for snapshot in (service._public(room), service._live_patch(room_id)):
        assert snapshot["needsUser"] is True
        assert snapshot["attentions"][0]["profile"] == "writer"
