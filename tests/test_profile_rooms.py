from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from filelock import FileLock

import flowly.profile_room_store as room_store_module
import flowly.profile_rooms as rooms_module
from flowly.gateway.server import GatewayServer
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.profile_room_store import (
    SQLITE_APPLICATION_ID,
    SQLITE_SCHEMA_VERSION,
    RoomStoreError,
    SQLiteRoomStore,
    assign_message_sequences,
)
from flowly.profile_rooms import ProfileRoomService


def _sqlite_path(legacy_path: Path) -> Path:
    return legacy_path.with_suffix(".sqlite3")


def _sqlite_message_payloads(legacy_path: Path) -> list[str]:
    with sqlite3.connect(_sqlite_path(legacy_path)) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM room_messages ORDER BY room_id, seq"
            )
        ]


def _legacy_room_payload(*, title: str = "Legacy council") -> dict[str, Any]:
    timestamp = "2026-08-27T00:00:00.000Z"
    return {
        "version": 1,
        "rooms": [{
            "id": "55fc1b75-0b89-47e6-8974-504eef89249c",
            "title": title,
            "members": ["default", "writer"],
            "mode": "panel",
            "messages": [{
                "id": "98654f75-2933-492a-82ee-f639337b4fc3",
                "role": "user",
                "content": "Please review this",
                "createdAt": timestamp,
            }],
            "watermarks": {"default": 0, "writer": 0},
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }],
    }


async def _eventually(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not settle")


def _room_settled(service: ProfileRoomService, room_id: str) -> bool:
    room = service._rooms.get(room_id, {})
    run = room.get("run") if isinstance(room, dict) else None
    return (
        not service._active.get(room_id)
        and isinstance(run, dict)
        and run.get("state") != "running"
    )


@pytest.mark.asyncio
async def test_room_store_is_owner_only_and_round_trips(tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        calls.append((profile, method, params))
        return {"ok": True}

    path = tmp_path / "rooms.json"
    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer", "reviewer"],
        on_event=None,
        store_path=path,
    )
    created = await service.create("Council", ["default", "writer"])

    assert created["title"] == "Council"
    assert created["members"] == ["default", "writer"]
    assert stat.S_IMODE(_sqlite_path(path).stat().st_mode) == 0o600

    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer", "reviewer"],
        on_event=None,
        store_path=path,
    )
    rooms = await reloaded.list()
    assert [room["id"] for room in rooms] == [created["id"]]
    assert rooms[0]["messages"] == []


@pytest.mark.asyncio
async def test_room_summaries_and_cursor_history_are_bounded_and_stable(
    tmp_path: Path,
) -> None:
    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]
    durable["messages"] = [
        {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": f"message-{index}",
            "createdAt": f"2026-08-27T00:{index // 60:02d}:{index % 60:02d}.000Z",
        }
        for index in range(125)
    ]
    durable["messages"][-1]["content"] = "x" * 700
    durable["messages"][-1]["attachments"] = [{
        "fileName": "brief.pdf",
        "mimeType": "application/pdf",
        "mediaId": "group-brief.pdf",
        "kind": "file",
        "size": 42,
        "status": "ready",
    }]

    compact = await service.dispatch("profiles.rooms.list", {"includeMessages": False})
    assert "messages" not in compact["rooms"][0]
    assert compact["rooms"][0]["messageCount"] == 125
    assert compact["rooms"][0]["latestMessage"]["content"] == "x" * 512
    assert "attachments" not in compact["rooms"][0]["latestMessage"]

    durable["messages"][-1]["content"] = "message-124"
    durable["messages"][-1].pop("attachments")

    # History is served by the durable store, not by slicing the resident
    # window, so a directly-seeded room has to be committed before it can be
    # paged. Production reaches this state through ``send``/``_run_room``,
    # which persist before any client can observe the new message.
    assign_message_sequences(durable)
    await service._persist()

    legacy = await service.dispatch("profiles.rooms.list", {})
    assert len(legacy["rooms"][0]["messages"]) == 125

    newest = await service.dispatch("profiles.rooms.history", {
        "roomId": room["id"], "limit": 50,
    })
    assert [message["content"] for message in newest["messages"]] == [
        f"message-{index}" for index in range(75, 125)
    ]
    assert newest["hasMore"] is True
    assert newest["totalCount"] == 125

    middle = await service.dispatch("profiles.rooms.history", {
        "roomId": room["id"], "cursor": newest["nextCursor"], "limit": 50,
    })
    assert [message["content"] for message in middle["messages"]] == [
        f"message-{index}" for index in range(25, 75)
    ]
    oldest = await service.dispatch("profiles.rooms.history", {
        "roomId": room["id"], "cursor": middle["nextCursor"], "limit": 50,
    })
    assert [message["content"] for message in oldest["messages"]] == [
        f"message-{index}" for index in range(25)
    ]
    assert oldest["nextCursor"] is None
    assert oldest["hasMore"] is False

    with pytest.raises(ProfileHostError) as wrong_room:
        await service.history(str(uuid.uuid4()), newest["nextCursor"], 50)
    assert wrong_room.value.code in {"ROOM_NOT_FOUND", "ROOM_CURSOR_INVALID"}

    # A cursor names a coordinate, not a message. Losing the message that
    # happened to sit on the boundary must not strand a reader mid-scroll:
    # the same cursor still answers "everything before this point".
    boundary_id = durable["messages"][75]["id"]
    durable["messages"] = [
        message for message in durable["messages"] if message["id"] != boundary_id
    ]
    await service._persist()
    survived = await service.history(room["id"], newest["nextCursor"], 50)
    assert [message["content"] for message in survived["messages"]] == [
        f"message-{index}" for index in range(25, 75)
    ]
    assert survived["hasMore"] is True


@pytest.mark.asyncio
async def test_room_prepare_warms_members_once_and_reports_partial_readiness(
    tmp_path: Path,
) -> None:
    events: list[dict[str, Any]] = []
    calls: list[str] = []
    gate = asyncio.Event()

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    async def prepare(profile: str):
        calls.append(profile)
        await gate.wait()
        if profile == "writer":
            raise ProfileHostError(
                "PROFILE_START_FAILED",
                "Writer needs provider authentication.",
                retryable=True,
            )
        return {"status": {"profile": profile, "state": "connected"}}

    service = ProfileRoomService(
        target_rpc=rpc,
        target_prepare=prepare,
        profile_directory=lambda: ["default", "writer"],
        on_event=events.append,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    first = asyncio.create_task(service.prepare(room["id"]))
    second = asyncio.create_task(service.prepare(room["id"]))
    await _eventually(lambda: len(calls) == 2)

    starting = (await service.list())[0]["readiness"]
    assert starting["default"]["state"] == "starting"
    assert starting["writer"]["state"] == "starting"
    gate.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert sorted(calls) == ["default", "writer"]
    assert first_result == second_result
    assert first_result["readiness"]["default"]["state"] == "ready"
    assert first_result["readiness"]["writer"] == {
        "state": "error",
        "updatedAt": first_result["readiness"]["writer"]["updatedAt"],
        "error": "Writer needs provider authentication.",
    }
    assert any(event.get("type") == "readiness" for event in events)


@pytest.mark.asyncio
async def test_legacy_json_migrates_by_verified_copy_and_remains_untouched(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "rooms.json"
    original = (json.dumps(_legacy_room_payload(), indent=2) + "\n").encode()
    legacy_path.write_bytes(original)

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=legacy_path,
    )
    rooms = await service.list()

    database_path = _sqlite_path(legacy_path)
    assert rooms[0]["title"] == "Legacy council"
    assert legacy_path.read_bytes() == original
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA application_id").fetchone()[0] == (
            SQLITE_APPLICATION_ID
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            SQLITE_SCHEMA_VERSION
        )
        assert connection.execute(
            "SELECT value FROM room_store_meta WHERE key = 'legacy_sha256'"
        ).fetchone()[0]

    # Once the verified database exists it remains authoritative. A stale or
    # manually edited source JSON can never roll the room history backward.
    legacy_path.write_text(
        json.dumps({"version": 1, "rooms": []}),
        encoding="utf-8",
    )
    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=legacy_path,
    )
    assert (await reloaded.list())[0]["title"] == "Legacy council"


@pytest.mark.asyncio
async def test_failed_migration_keeps_json_authoritative_and_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = tmp_path / "rooms.json"
    legacy_path.write_text(json.dumps(_legacy_room_payload()), encoding="utf-8")

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=legacy_path,
    )

    def fail_migration(*_args, **_kwargs):
        raise RoomStoreError("injected migration failure")

    monkeypatch.setattr(service._sqlite_store, "initialize_verified", fail_migration)
    assert (await service.list())[0]["title"] == "Legacy council"
    assert service._storage_mode == "legacy-json-fallback"
    assert not _sqlite_path(legacy_path).exists()

    await service.create("Fallback room", ["default", "writer"])
    durable = json.loads(legacy_path.read_text(encoding="utf-8"))
    assert {room["title"] for room in durable["rooms"]} == {
        "Legacy council", "Fallback room",
    }
    assert not _sqlite_path(legacy_path).exists()


def test_migration_lock_wait_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteRoomStore(tmp_path / "rooms.sqlite3")
    blocker = FileLock(str(store.lock_path))
    blocker.acquire()
    monkeypatch.setattr(room_store_module, "MIGRATION_LOCK_TIMEOUT_SECONDS", 0.01)
    try:
        with pytest.raises(RoomStoreError, match="Timed out"):
            store.initialize_verified({})
    finally:
        blocker.release()


@pytest.mark.asyncio
async def test_sqlite_revision_rejects_stale_process_without_losing_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rooms.json"

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    first = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    stale = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    assert await first.list() == []
    assert await stale.list() == []
    created = await first.create("Committed", ["default", "writer"])

    with pytest.raises(ProfileHostError) as error:
        await stale.create("Stale writer", ["default", "writer"])
    assert error.value.code == "ROOM_STORE_CONFLICT"
    assert error.value.retryable is True
    assert [room["id"] for room in await stale.list()] == [created["id"]]

    verified = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    rooms = await verified.list()
    assert [room["id"] for room in rooms] == [created["id"]]


@pytest.mark.asyncio
async def test_sqlite_updates_only_the_changed_room_rows(tmp_path: Path) -> None:
    path = tmp_path / "rooms.json"

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    first = await service.create("First", ["default", "writer"])
    second = await service.create("Second", ["default", "writer"])
    with sqlite3.connect(_sqlite_path(path)) as connection:
        before = dict(connection.execute("SELECT id, row_revision FROM rooms"))

    await service.update(
        first["id"], "First updated", ["default", "writer"], "panel"
    )
    with sqlite3.connect(_sqlite_path(path)) as connection:
        after = dict(connection.execute("SELECT id, row_revision FROM rooms"))
    assert after[first["id"]] > before[first["id"]]
    assert after[second["id"]] == before[second["id"]]


@pytest.mark.asyncio
async def test_existing_invalid_sqlite_never_falls_back_to_stale_json(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "rooms.json"
    legacy = json.dumps(_legacy_room_payload()).encode()
    legacy_path.write_bytes(legacy)
    _sqlite_path(legacy_path).write_bytes(b"not a sqlite database")

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=legacy_path,
    )
    with pytest.raises(ProfileHostError) as error:
        await service.list()
    assert error.value.code == "ROOM_STORE_INVALID"
    assert legacy_path.read_bytes() == legacy
    assert _sqlite_path(legacy_path).read_bytes() == b"not a sqlite database"


@pytest.mark.asyncio
async def test_room_store_preserves_malformed_legacy_history_for_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rooms.json"
    path.write_text(json.dumps({
        "version": 1,
        "rooms": [{
            "id": "55fc1b75-0b89-47e6-8974-504eef89249c",
            "title": "Council",
            "members": ["default", "writer"],
            "messages": [{
                "id": "98654f75-2933-492a-82ee-f639337b4fc3",
                "role": "assistant",
                "profile": "writer",
                "content": "Finished",
                "createdAt": "2026-08-27T00:00:00.000Z",
                "toolCalls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "argumentsJson": "not-json",
                }],
            }],
            "watermarks": {"default": 0, "writer": 1},
            "createdAt": "2026-08-27T00:00:00.000Z",
            "updatedAt": "2026-08-27T00:00:00.000Z",
        }],
    }))

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    original = path.read_bytes()
    with pytest.raises(ProfileHostError, match="left untouched"):
        await service.list()
    assert path.read_bytes() == original
    assert not _sqlite_path(path).exists()


@pytest.mark.asyncio
async def test_room_validates_definition_without_starting_profiles(tmp_path: Path) -> None:
    async def rpc(*_args, **_kwargs):
        raise AssertionError("definition validation must not start a runtime")

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    with pytest.raises(ProfileHostError, match="at least two"):
        await service.create("Solo", ["writer"])
    with pytest.raises(ProfileHostError, match="no longer exists"):
        await service.create("Missing", ["default", "unknown"])
    with pytest.raises(ProfileHostError, match="Group name"):
        await service.create("", ["default", "writer"])
    with pytest.raises(ProfileHostError, match="panel or council"):
        await service.create("Invalid mode", ["default", "writer"], "debate")


@pytest.mark.asyncio
async def test_room_mode_updates_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "rooms.json"

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    room = await service.create("Council", ["default", "writer"])
    assert room["mode"] == "panel"

    updated = await service.update(
        room["id"], "Council", ["default", "writer"], "council",
    )
    assert updated["mode"] == "council"
    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    assert (await reloaded.list())[0]["mode"] == "council"


@pytest.mark.asyncio
async def test_failed_persistence_rolls_back_room_mutations(tmp_path: Path) -> None:
    async def rpc(*_args, **_kwargs):
        raise AssertionError("a non-durable turn must never start a profile runtime")

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    original_persist = service._persist
    service._persist = AsyncMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        await service.send(room["id"], "This must not become visible")

    assert service._active.get(room["id"]) is None
    assert service._epochs.get(room["id"]) is None
    assert (await service.list())[0]["messages"] == []
    service._persist = original_persist

    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    assert (await reloaded.list())[0]["messages"] == []


@pytest.mark.asyncio
async def test_failed_member_does_not_advance_durable_watermark(tmp_path: Path) -> None:
    path = tmp_path / "rooms.json"
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method != "chat.send":
            return {"ok": True}
        run_id = "failed-run"

        async def fail() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "state": "error",
                "errorMessage": "provider unavailable",
            })

        asyncio.create_task(fail())
        return {"runId": run_id}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    room = await service.create("Council", ["default", "writer"])
    await service.send(room["id"], "@writer review this")
    await _eventually(lambda: _room_settled(service, room["id"]))

    assert service._rooms[room["id"]]["watermarks"]["writer"] == 0
    assert (await service.list())[0]["runState"]["state"] == "partial"

    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    await reloaded.list()
    assert reloaded._rooms[room["id"]]["watermarks"]["writer"] == 0


@pytest.mark.asyncio
async def test_stop_checkpoints_partial_member_reply_as_aborted_history(tmp_path: Path) -> None:
    path = tmp_path / "rooms.json"
    events: list[dict[str, Any]] = []

    async def rpc(profile: str, method: str, _params: dict[str, Any], _timeout: float):
        if method == "chat.send":
            return {"runId": f"partial-{profile}"}
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=events.append,
        store_path=path,
    )
    room = await service.create("Council", ["default", "writer"])
    await service.send(room["id"], "@writer explain this")
    await _eventually(lambda: bool(service._waiters))

    session_key = f"desktop:profile-room:{room['id']}"
    await service.handle_profile_event("writer", "agent", {
        "sessionKey": session_key,
        "runId": "partial-writer",
        "stream": "tool",
        "data": {
            "phase": "start",
            "toolCallId": "read-1",
            "name": "read_file",
            "argumentsJson": '{"path":"/workspace/report.md"}',
        },
    })
    await service.handle_profile_event("writer", "agent", {
        "sessionKey": session_key,
        "runId": "partial-writer",
        "stream": "assistant",
        "data": {"text": "Here is the part I finished"},
    })

    await service.stop(room["id"])
    settled = (await service.list())[0]
    reply = settled["messages"][-1]
    assert reply["role"] == "assistant"
    assert reply["profile"] == "writer"
    assert reply["content"] == "Here is the part I finished"
    assert reply["aborted"] is True
    assert isinstance(reply["durationMs"], int)
    assert reply["toolCalls"] == [{
        "id": "read-1",
        "name": "read_file",
        "argumentsJson": '{"path":"/workspace/report.md"}',
    }]
    assert settled["running"] is False
    assert settled["runState"]["state"] == "aborted"
    assert service._rooms[room["id"]]["watermarks"]["writer"] == 1

    # A terminal frame racing the stop acknowledgement is stale. It must not
    # erase the checkpoint or append a second copy of the same member turn.
    await service.handle_profile_event("writer", "chat", {
        "sessionKey": session_key,
        "runId": "partial-writer",
        "state": "aborted",
        "message": {"content": "Here is the part I finished"},
    })
    assert len((await service.list())[0]["messages"]) == 2

    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    durable = (await reloaded.list())[0]["messages"][-1]
    assert durable["content"] == "Here is the part I finished"
    assert durable["aborted"] is True
    assert any(event.get("type") == "run-state" for event in events)


@pytest.mark.asyncio
async def test_stop_keeps_terminal_text_while_member_commit_is_in_flight(tmp_path: Path) -> None:
    async def rpc(profile: str, method: str, _params: dict[str, Any], _timeout: float):
        if method == "chat.send":
            return {"runId": f"terminal-{profile}"}
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])

    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    original_commit = service._commit_member_terminal

    async def blocked_commit(*args: Any, **kwargs: Any) -> str:
        commit_entered.set()
        await release_commit.wait()
        return await original_commit(*args, **kwargs)

    service._commit_member_terminal = blocked_commit  # type: ignore[method-assign]
    await service.send(room["id"], "@writer answer")
    await _eventually(lambda: bool(service._waiters))
    await service.handle_profile_event("writer", "chat", {
        "sessionKey": f"desktop:profile-room:{room['id']}",
        "runId": "terminal-writer",
        "state": "final",
        "message": {"content": "The final frame arrived first"},
    })
    await asyncio.wait_for(commit_entered.wait(), 1)

    await service.stop(room["id"])
    release_commit.set()
    await asyncio.sleep(0)

    reply = (await service.list())[0]["messages"][-1]
    assert reply["content"] == "The final frame arrived first"
    assert reply["aborted"] is True


@pytest.mark.asyncio
async def test_restart_marks_interrupted_run_stranded_without_losing_attention(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rooms.json"

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    created = await service.create("Council", ["default", "writer"])
    room = service._rooms[created["id"]]
    timestamp = "2026-08-27T01:00:00.000Z"
    room["messages"].append({
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "Please inspect this",
        "createdAt": timestamp,
    })
    room["run"] = {
        "id": str(uuid.uuid4()),
        "state": "running",
        "startedAt": timestamp,
        "finishedAt": "",
        "members": {
            "writer": {
                "state": "needs_user",
                "boundary": 1,
                "gatewayRunId": "gateway-run-1",
                "updatedAt": timestamp,
                "error": "",
                "attention": {
                    "kind": "approval",
                    "profile": "writer",
                    "id": "approval-1",
                    "request": {
                        "id": "approval-1",
                        "command": "echo safe",
                        "sessionKey": f"desktop:profile-room:{created['id']}",
                    },
                },
            },
        },
    }
    await service._persist()

    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    recovered = (await reloaded.list())[0]["runState"]

    assert recovered["state"] == "stranded"
    assert recovered["members"]["writer"]["state"] == "stranded"
    assert recovered["members"]["writer"]["attention"]["id"] == "approval-1"
    assert reloaded._rooms[created["id"]]["watermarks"]["writer"] == 0
    with sqlite3.connect(_sqlite_path(path)) as connection:
        durable = json.loads(
            connection.execute("SELECT run_json FROM rooms").fetchone()[0]
        )
    assert durable["state"] == "stranded"


@pytest.mark.asyncio
async def test_late_terminal_is_durable_but_never_committed_to_the_transcript(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rooms.json"

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    created = await service.create("Council", ["default", "writer"])
    room = service._rooms[created["id"]]
    timestamp = "2026-08-27T01:00:00.000Z"
    room["run"] = {
        "id": str(uuid.uuid4()),
        "state": "partial",
        "startedAt": timestamp,
        "finishedAt": timestamp,
        "members": {
            "writer": {
                "state": "failed",
                "boundary": 0,
                "gatewayRunId": "late-run",
                "updatedAt": timestamp,
                "error": "timed out",
            },
        },
    }
    await service._persist()

    consumed = await service.handle_profile_event("writer", "chat", {
        "sessionKey": f"desktop:profile-room:{created['id']}",
        "runId": "late-run",
        "state": "final",
        "message": {"content": "too late"},
    })

    assert consumed is True
    assert room["messages"] == []
    assert room["watermarks"]["writer"] == 0
    assert room["run"]["members"]["writer"]["lateResult"]["state"] == "final"
    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    public = (await reloaded.list())[0]
    assert public["runState"]["members"]["writer"]["lateResult"]["disposition"] == (
        "discarded"
    )


@pytest.mark.asyncio
async def test_unmentioned_room_turn_runs_members_in_parallel_and_persists_replies(
    tmp_path: Path,
) -> None:
    events: list[dict[str, Any]] = []
    sends: list[str] = []
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method != "chat.send":
            return {"ok": True}
        sends.append(profile)
        run_id = f"run-{profile}"

        async def finish() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "agent", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "stream": "assistant",
                "data": {"text": f"Hello from {profile}"},
            })
            await asyncio.sleep(0.06)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "state": "final",
                "message": {"content": [{"type": "text", "text": f"Hello from {profile}"}]},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=events.append,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    accepted = await service.send(room["id"], "Hello everyone")
    assert accepted["running"] is True

    await _eventually(lambda: (
        len([
            message for message in service._rooms[room["id"]]["messages"]
            if message["role"] == "assistant"
        ]) == 2
        and service._rooms[room["id"]]["run"]["state"] != "running"
    ))
    listed = (await service.list())[0]
    assert set(sends) == {"default", "writer"}
    assert listed["running"] is False
    assert {message.get("profile") for message in listed["messages"] if message["role"] == "assistant"} == {
        "default", "writer",
    }
    assert any(event["type"] == "stream" and "messages" not in event.get("patch", {}) for event in events)


@pytest.mark.asyncio
async def test_council_turns_are_sequential_bounded_and_see_prior_replies(
    tmp_path: Path,
) -> None:
    service: ProfileRoomService
    order: list[str] = []
    prompts: list[tuple[str, str]] = []
    responses = {
        ("default", 1): "First perspective",
        ("writer", 1): "Second perspective",
        ("default", 2): "(pass)",
    }
    counts = {"default": 0, "writer": 0}

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method != "chat.send":
            return {"ok": True}
        counts[profile] += 1
        order.append(profile)
        prompts.append((profile, params["message"]))
        run_id = f"run-{profile}-{counts[profile]}"
        response = responses[(profile, counts[profile])]

        async def finish() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "state": "final",
                "message": {"content": response},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create(
        "Review board", ["default", "writer"], "council",
    )
    await service.send(room["id"], "Review this plan")
    await _eventually(
        lambda: service._rooms[room["id"]].get("run", {}).get("state") != "running"
    )

    public = (await service.list())[0]
    assert order == ["default", "writer", "default"]
    assert "default: First perspective" in prompts[1][1]
    assert "writer: Second perspective" in prompts[2][1]
    assert [message["content"] for message in public["messages"]] == [
        "Review this plan", "First perspective", "Second perspective",
    ]
    assert public["runState"]["mode"] == "council"
    assert public["runState"]["round"] == 2
    assert public["runState"]["turnCount"] == 3


@pytest.mark.asyncio
async def test_directed_council_follow_up_can_mention_another_member(
    tmp_path: Path,
) -> None:
    service: ProfileRoomService
    order: list[str] = []
    responses = {"writer": "@reviewer please validate", "reviewer": "(pass)"}

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method != "chat.send":
            return {"ok": True}
        order.append(profile)
        run_id = f"run-{profile}"

        async def finish() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "state": "final",
                "message": {"content": responses[profile]},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer", "reviewer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create(
        "Review board", ["default", "writer", "reviewer"], "council",
    )
    await service.send(room["id"], "@writer inspect this")
    await _eventually(
        lambda: service._rooms[room["id"]].get("run", {}).get("state") != "running"
    )

    assert order == ["writer", "reviewer"]
    assert [
        message.get("profile")
        for message in service._rooms[room["id"]]["messages"]
        if message["role"] == "assistant"
    ] == ["writer"]


@pytest.mark.asyncio
async def test_mention_targets_only_selected_member_and_ignores_code_mentions(
    tmp_path: Path,
) -> None:
    sends: list[str] = []
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method != "chat.send":
            return {"ok": True}
        sends.append(profile)
        run_id = f"run-{len(sends)}"

        async def finish() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "state": "final",
                "message": {"content": "Selected reply"},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer", "reviewer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer", "reviewer"])
    await service.send(room["id"], "`@reviewer` please ignore; @writer answer")
    await _eventually(lambda: _room_settled(service, room["id"]))
    assert sends == ["writer"]


@pytest.mark.asyncio
async def test_group_rpc_fans_attachment_bytes_only_to_selected_member(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method != "chat.send":
            return {"ok": True}
        calls.append((profile, params))
        run_id = f"run-{profile}"

        async def finish() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "state": "final",
                "message": {"content": "Reviewed"},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    store_path = tmp_path / "rooms.json"
    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=store_path,
    )
    room = await service.create("Council", ["default", "writer"])
    encoded = base64.b64encode(b"private report bytes").decode("ascii")
    result = await service.dispatch("profiles.rooms.send", {
        "roomId": room["id"],
        "content": "@writer inspect this report",
        "attachments": [{
            "fileName": "report.pdf",
            "mimeType": "application/pdf",
            "content": encoded,
        }],
    })

    durable = result["room"]["messages"][-1]["attachments"][0]
    assert durable["fileName"] == "report.pdf"
    assert durable["mimeType"] == "application/pdf"
    assert durable["kind"] == "file"
    assert durable["status"] == "ready"
    assert durable["size"] == len(b"private report bytes")
    assert durable["mediaId"].startswith(f"group-{room['id'][:8]}-")
    assert (tmp_path / "media" / durable["mediaId"]).read_bytes() == b"private report bytes"
    await _eventually(lambda: _room_settled(service, room["id"]))
    assert [profile for profile, _ in calls] == ["writer"]
    assert calls[0][1]["attachments"] == [{
        "fileName": "report.pdf",
        "mimeType": "application/pdf",
        "content": encoded,
    }]
    assert all(encoded not in payload for payload in _sqlite_message_payloads(store_path))
    assert any(
        durable["mediaId"] in payload
        for payload in _sqlite_message_payloads(store_path)
    )
    assert room["id"] not in service._pending_attachments
    await service.shutdown()

    # A gateway restart must retain the safe descriptor and its host-local
    # original; deleting the room must reclaim that original as one lifecycle.
    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=store_path,
    )
    reopened = (await reloaded.list())[0]
    reopened_user = next(
        message for message in reopened["messages"] if message["role"] == "user"
    )
    assert reopened_user["attachments"][0] == durable
    await reloaded.delete(room["id"])
    assert not (tmp_path / "media" / durable["mediaId"]).exists()
    await reloaded.shutdown()


@pytest.mark.asyncio
async def test_group_reply_imports_generated_media_into_room_lifecycle(
    tmp_path: Path,
) -> None:
    # 1x1 PNG. The member profile owns these bytes; the room must copy them
    # into its host-owned media namespace before the profile runtime can stop.
    media = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method == "media.read":
            assert profile == "writer"
            assert params["mediaId"] == "generated.png"
            offset = params["offset"]
            # Deliberately return small windows to exercise the bounded loop,
            # rather than assuming one RPC always contains the whole file.
            chunk = media[offset:offset + 7]
            return {
                "mediaId": "generated.png",
                "size": len(media),
                "mimeType": "image/png",
                "offset": offset,
                "eof": offset + len(chunk) == len(media),
                "data": base64.b64encode(chunk).decode("ascii"),
            }
        if method != "chat.send":
            return {"ok": True}
        run_id = "run-generated-media"

        async def finish() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"],
                "runId": run_id,
                "state": "final",
                "message": {
                    "content": "Here is the image.",
                    "attachments": [{
                        "fileName": "generated.png",
                        "mimeType": "image/png",
                        "mediaId": "generated.png",
                        "kind": "image",
                        "width": 1,
                        "height": 1,
                        "status": "ready",
                    }],
                },
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    store_path = tmp_path / "rooms.json"
    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=store_path,
    )
    room = await service.create("Council", ["default", "writer"])
    await service.send(room["id"], "@writer make an image")
    await _eventually(lambda: _room_settled(service, room["id"]))

    reply = (await service.list())[0]["messages"][-1]
    attachment = reply["attachments"][0]
    assert attachment["mediaId"].startswith(f"group-{room['id'][:8]}-")
    assert attachment["mediaId"] != "generated.png"
    assert attachment["kind"] == "image"
    assert attachment["width"] == 1
    assert attachment["height"] == 1
    copied = tmp_path / "media" / attachment["mediaId"]
    assert copied.read_bytes() == media
    assert any(
        attachment["mediaId"] in payload
        for payload in _sqlite_message_payloads(store_path)
    )

    await service.delete(room["id"])
    assert not copied.exists()


@pytest.mark.asyncio
async def test_group_rpc_rejects_paths_insecure_urls_and_invalid_file_data(
    tmp_path: Path,
) -> None:
    rpc = AsyncMock()
    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    base = {"fileName": "report.pdf", "mimeType": "application/pdf"}

    with pytest.raises(ProfileHostError, match="remotely readable"):
        await service.send(room["id"], "inspect", [{**base, "filePath": "/private.pdf"}])
    with pytest.raises(ProfileHostError, match="URL is invalid"):
        await service.send(room["id"], "inspect", [{**base, "cdnUrl": "http://example.com/report.pdf"}])
    with pytest.raises(ProfileHostError, match="invalid file data"):
        await service.send(room["id"], "inspect", [{**base, "content": "%%%"}])

    assert rpc.await_count == 0
    assert (await service.list())[0]["messages"] == []


@pytest.mark.asyncio
async def test_legacy_room_import_is_transactional_and_idempotent(tmp_path: Path) -> None:
    service = ProfileRoomService(
        target_rpc=AsyncMock(),
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    created_at = "2026-08-27T01:00:00.000Z"
    room_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    legacy = {
        "id": room_id,
        "title": "Legacy council",
        "members": ["default", "writer"],
        "messages": [{
            "id": message_id,
            "role": "user",
            "content": "Review this",
            "createdAt": created_at,
            "attachments": [{
                "fileName": "brief.pdf",
                "mimeType": "application/pdf",
                "filePath": "/must/not/cross-the-boundary",
            }],
            "private": "discard me",
        }],
        "watermarks": {"default": 1, "writer": 1},
        "createdAt": created_at,
        "updatedAt": created_at,
    }

    first = await service.import_rooms([legacy])
    assert first == {"ok": True, "imported": 1, "existing": 0, "conflicts": []}
    listed = (await service.list())[0]
    assert listed["messages"][0]["attachments"] == [{
        "fileName": "brief.pdf", "mimeType": "application/pdf",
    }]
    assert "filePath" not in json.dumps(listed)
    assert "private" not in json.dumps(listed)

    retry = await service.import_rooms([legacy])
    assert retry == {"ok": True, "imported": 0, "existing": 1, "conflicts": []}

    service._rooms[room_id]["messages"].append({
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "profile": "writer",
        "content": "A newer remote reply",
        "createdAt": "2026-08-27T01:01:00.000Z",
    })
    advanced = await service.import_rooms([legacy])
    assert advanced == {"ok": True, "imported": 0, "existing": 1, "conflicts": []}


@pytest.mark.asyncio
async def test_legacy_room_import_rejects_collision_without_partial_write(tmp_path: Path) -> None:
    service = ProfileRoomService(
        target_rpc=AsyncMock(),
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    existing = await service.create("Existing", ["default", "writer"])
    timestamp = "2026-08-27T01:00:00.000Z"
    missing_id = str(uuid.uuid4())
    result = await service.import_rooms([
        {
            "id": missing_id,
            "title": "Would otherwise import",
            "members": ["default", "writer"],
            "messages": [],
            "watermarks": {"default": 0, "writer": 0},
            "createdAt": timestamp,
            "updatedAt": timestamp,
        },
        {
            "id": existing["id"],
            "title": "Collision",
            "members": ["default", "writer"],
            "messages": [],
            "watermarks": {"default": 0, "writer": 0},
            "createdAt": timestamp,
            "updatedAt": timestamp,
        },
    ])
    assert result == {
        "ok": False, "imported": 0, "existing": 0, "conflicts": [existing["id"]],
    }
    assert {room["id"] for room in await service.list()} == {existing["id"]}


@pytest.mark.asyncio
async def test_tool_activity_is_projected_and_attached_to_durable_reply(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        if method != "chat.send":
            return {"ok": True}
        run_id = "run-tools"

        async def finish() -> None:
            await asyncio.sleep(0)
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"], "runId": run_id,
                "state": "iteration_step", "role": "assistant",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "/safe/file.txt", "secret": "drop-me"}),
                    },
                }],
            })
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"], "runId": run_id,
                "state": "iteration_step", "role": "tool",
                "tool_call_id": "call-1", "name": "read_file",
            })
            await service.handle_profile_event(profile, "chat", {
                "sessionKey": params["sessionKey"], "runId": run_id,
                "state": "final", "message": {"content": "Finished"},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=events.append,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    await service.send(room["id"], "@writer inspect")
    await _eventually(lambda: _room_settled(service, room["id"]))
    reply = (await service.list())[0]["messages"][-1]
    assert reply["toolCalls"] == [{
        "id": "call-1",
        "name": "read_file",
        "argumentsJson": '{"path":"/safe/file.txt"}',
    }]
    assert not any("drop-me" in json.dumps(event) for event in events)


@pytest.mark.asyncio
async def test_room_approval_is_scoped_to_requesting_member(tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _timeout: float):
        calls.append((profile, method, params))
        if method == "chat.send":
            return {"runId": "run-1"}
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    await service.send(room["id"], "@writer run it")
    await _eventually(lambda: bool(service._runs.get(room["id"])))
    await service.handle_profile_event("writer", "exec.approval.requested", {
        "sessionKey": f"desktop:profile-room:{room['id']}",
        "runId": "run-1", "id": "approval-1", "command": "echo ok",
    })
    assert (await service.list())[0]["needsUser"] is True
    await service.resolve_approval(room["id"], "approval-1", "allow-once")
    assert calls[-1] == (
        "writer", "exec.approval.resolve",
        {"id": "approval-1", "decision": "allow-once"},
    )
    await service.stop(room["id"])


@pytest.mark.asyncio
async def test_profile_room_events_are_scoped_to_room_subscribers() -> None:
    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)

    def socket():
        ws = SimpleNamespace(closed=False, messages=[])

        async def send_json(payload):
            ws.messages.append(payload)

        async def close():
            ws.closed = True

        ws.send_json = send_json
        ws.close = close
        return ws

    rooms = socket()
    compact_rooms = socket()
    directory = socket()
    server._ws_clients.update({
        "rooms": rooms, "compact": compact_rooms, "directory": directory,
    })
    server._bind_profile_client_request("rooms", "profiles.rooms.list", {})
    server._bind_profile_client_request(
        "compact", "profiles.rooms.list", {"eventMode": "delta-v1"}
    )
    server._bind_profile_client_request("directory", "profiles.list", {})
    assert server._profile_subscription_uses_default(
        server._profile_client_subscriptions["rooms"]
    ) is True

    payload = {
        "hostId": "host", "roomId": "room", "type": "updated",
        "room": {
            "id": "room",
            "messages": [{"id": "message-1"}],
            "messageCount": 1,
            "latestMessage": {"id": "message-1"},
        },
    }
    await server._broadcast_profile_room_event(payload)
    assert rooms.messages == [{
        "type": "event",
        "event": "profile.room",
        "data": payload,
    }]
    assert compact_rooms.messages[0]["data"]["room"] == {
        "id": "room",
        "messageCount": 1,
        "latestMessage": {"id": "message-1"},
    }
    assert directory.messages == []
    await server.stop()


@pytest.mark.asyncio
async def test_unknown_room_method_neither_dispatches_nor_subscribes() -> None:
    rooms = SimpleNamespace(
        methods=("profiles.rooms.list",),
        dispatch=lambda *_args: None,
    )
    host = object.__new__(ProfileHost)
    host.host_id = "host-1"
    host._rooms = rooms
    with pytest.raises(ProfileHostError) as error:
        await host.dispatch("profiles.rooms.untrusted", {})
    assert error.value.code == "METHOD_NOT_ALLOWED"

    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)
    server._bind_profile_client_request("client", "profiles.rooms.untrusted", {})
    assert server._profile_client_subscriptions["client"].rooms is False
    await server.stop()


@pytest.mark.asyncio
async def test_profile_host_room_contract_includes_host_identity() -> None:
    class Rooms:
        methods = ("profiles.rooms.list",)

        async def dispatch(self, method: str, params: dict[str, Any]):
            assert method == "profiles.rooms.list"
            assert params == {}
            return {"rooms": []}

    host = object.__new__(ProfileHost)
    host.host_id = "host-1"
    host._rooms = Rooms()
    result = await host.dispatch("profiles.rooms.list", {})
    assert result == {"hostId": "host-1", "rooms": []}


@pytest.mark.asyncio
async def test_history_outlives_the_live_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the window must not mean leaving the group."""
    monkeypatch.setattr(rooms_module, "_MAX_MESSAGES", 10)
    # The window is spent in bytes now, with a floor of recent messages the
    # budget cannot take away. Both have to come down for a ten-message
    # window to be reachable in a test.
    monkeypatch.setattr(rooms_module, "_MIN_RESIDENT_MESSAGES", 1)
    monkeypatch.setattr(rooms_module, "_RESIDENT_WINDOW_BYTES", 10 * 1024 * 1024)

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]
    for index in range(25):
        service._append_message(durable, {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": f"m{index}",
            "createdAt": f"2026-08-27T00:00:{index:02d}.000Z",
        })
        service._trim(durable)
        await service._persist()

    # Memory is bounded by the window...
    assert len(durable["messages"]) == 10
    assert [message["content"] for message in durable["messages"]] == [
        f"m{index}" for index in range(15, 25)
    ]
    # ...while the group still reports, and can serve, everything it has.
    assert service._public(durable, include_messages=False)["messageCount"] == 25

    collected: list[str] = []
    cursor = None
    while True:
        page = await service.history(room["id"], cursor, 10)
        assert page["totalCount"] == 25
        collected = [message["content"] for message in page["messages"]] + collected
        cursor = page["nextCursor"]
        if cursor is None:
            assert page["hasMore"] is False
            break
    assert collected == [f"m{index}" for index in range(25)]

    # Sequences are handed out once and never reused, so the numbering has no
    # gaps and no repeats across the window boundary.
    with sqlite3.connect(_sqlite_path(tmp_path / "rooms.json")) as connection:
        sequences = [
            int(row[0])
            for row in connection.execute(
                "SELECT seq FROM room_messages ORDER BY seq"
            )
        ]
        trimmed = int(
            connection.execute(
                "SELECT COUNT(*) FROM room_messages WHERE trimmed = 1"
            ).fetchone()[0]
        )
    assert sequences == list(range(25))
    assert trimmed == 15


@pytest.mark.asyncio
async def test_persisting_one_reply_does_not_rewrite_the_whole_room(
    tmp_path: Path,
) -> None:
    """A turn should cost one row, not one row per message in the group."""
    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]
    for index in range(40):
        service._append_message(durable, {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": f"m{index}",
            "createdAt": "2026-08-27T00:00:00.000Z",
        })
    await service._persist()

    statements: list[str] = []
    original_connect = SQLiteRoomStore._connect

    def tracing_connect(path: Path, *, journal_mode: str):
        connection = original_connect(path, journal_mode=journal_mode)
        connection.set_trace_callback(lambda sql: statements.append(" ".join(sql.split())))
        return connection

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(SQLiteRoomStore, "_connect", staticmethod(tracing_connect))
    try:
        service._append_message(durable, {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "profile": "writer",
            "content": "the reply",
            "createdAt": "2026-08-27T00:01:00.000Z",
        })
        await service._persist()
    finally:
        monkeypatched.undo()

    # The room is no longer cleared and rebuilt on every turn — that is what
    # made a reply cost a write per message, and what forced sequences to be
    # re-derived from position on each save.
    assert not [sql for sql in statements if sql.startswith("DELETE FROM room_messages")]
    assert [sql for sql in statements if sql.startswith("INSERT INTO room_messages")]

    database = _sqlite_path(tmp_path / "rooms.json")
    with sqlite3.connect(database) as connection:
        total = int(
            connection.execute("SELECT COUNT(*) FROM room_messages").fetchone()[0]
        )
        contents = [
            json.loads(row[0])["content"]
            for row in connection.execute(
                "SELECT payload_json FROM room_messages ORDER BY seq"
            )
        ]
    assert total == 41
    assert contents[-1] == "the reply"


@pytest.mark.asyncio
async def test_room_events_carry_the_messages_they_add(tmp_path: Path) -> None:
    """A snapshot event should let a receiver apply the change, not refetch."""
    events: list[dict[str, Any]] = []

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=on_event,
        store_path=tmp_path / "rooms.json",
    )
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]

    # The first snapshot a receiver sees establishes its baseline, so it
    # carries no delta — there is nothing yet to be a delta against.
    assert "appended" not in events[-1]

    service._append_message(durable, {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "first",
        "createdAt": "2026-08-27T00:00:00.000Z",
    })
    await service._persist()
    await service._emit({
        "roomId": room["id"], "type": "updated", "room": service._public(durable),
    })
    assert [message["content"] for message in events[-1]["appended"]] == ["first"]
    assert events[-1]["lastSeq"] == 0

    service._append_message(durable, {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "profile": "writer",
        "content": "second",
        "createdAt": "2026-08-27T00:00:01.000Z",
    })
    await service._persist()
    await service._emit({
        "roomId": room["id"], "type": "updated", "room": service._public(durable),
    })
    assert [message["content"] for message in events[-1]["appended"]] == ["second"]
    assert events[-1]["windowFirstSeq"] == 0
    assert events[-1]["lastSeq"] == 1


def test_v1_store_migrates_to_the_sequenced_schema(tmp_path: Path) -> None:
    """A database written before sequencing keeps every message and its order."""
    path = tmp_path / "rooms.sqlite3"
    timestamp = "2026-08-27T00:00:00.000Z"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            PRAGMA application_id = {SQLITE_APPLICATION_ID};
            PRAGMA user_version = 1;
            CREATE TABLE room_store_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE rooms (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, mode TEXT NOT NULL,
                members_json TEXT NOT NULL, watermarks_json TEXT NOT NULL,
                run_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                row_revision INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE room_messages (
                room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, message_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (room_id, ordinal), UNIQUE (room_id, message_id)
            ) WITHOUT ROWID;
            INSERT INTO room_store_meta(key, value) VALUES('revision', '3');
            """
        )
        connection.execute(
            "INSERT INTO rooms VALUES(?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                "55fc1b75-0b89-47e6-8974-504eef89249c", "Legacy", "panel",
                '["default","writer"]', '{"default":0,"writer":0}',
                timestamp, timestamp, 3,
            ),
        )
        for ordinal in range(3):
            connection.execute(
                "INSERT INTO room_messages VALUES(?, ?, ?, ?)",
                (
                    "55fc1b75-0b89-47e6-8974-504eef89249c", ordinal, f"msg-{ordinal}",
                    json.dumps({
                        "id": f"msg-{ordinal}", "role": "user",
                        "content": f"m{ordinal}", "createdAt": timestamp,
                    }),
                ),
            )

    loaded = SQLiteRoomStore(path).load()

    assert loaded.revision == 3
    assert [message["seq"] for message in loaded.rooms[0]["messages"]] == [0, 1, 2]
    assert [message["content"] for message in loaded.rooms[0]["messages"]] == [
        "m0", "m1", "m2",
    ]
    assert loaded.rooms[0]["nextSeq"] == 3
    assert loaded.rooms[0]["trimmedCount"] == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            SQLITE_SCHEMA_VERSION
        )


async def _seeded_service(path: Path, *, turns: int) -> ProfileRoomService:
    """A room that has been through a real durable round trip."""
    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]
    for index in range(turns):
        service._append_message(durable, {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": f"message {index}",
            "createdAt": "2026-08-27T00:00:00.000Z",
        })
    await service._persist()
    return service


@pytest.mark.asyncio
async def test_durable_load_preserves_the_coordinates_it_read(tmp_path: Path) -> None:
    """A load must return the room the store holds, sequences included.

    Message validation rebuilds every record from an allowlist. When that
    allowlist omitted ``seq`` — and the room rebuild omitted ``nextSeq`` and
    ``trimmedCount`` — a load silently returned a room with no coordinates at
    all, and the next write had to invent them from position.
    """
    path = tmp_path / "rooms.json"
    seeded = await _seeded_service(path, turns=6)
    room_id = next(iter(seeded._rooms))

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    reloaded = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    await reloaded.list(include_messages=False)
    room = reloaded._rooms[room_id]

    assert [message["seq"] for message in room["messages"]] == list(range(6))
    assert room["nextSeq"] == 6
    assert room["trimmedCount"] == 0


@pytest.mark.asyncio
async def test_a_send_after_a_durable_load_does_not_collide(tmp_path: Path) -> None:
    """The reported failure, end to end.

    Losing the sequences on load made the next append reserve ``0`` — a
    coordinate the store had already given to the room's first message. The
    write then tried to move every existing message one place up, collided
    with its own ``UNIQUE (room_id, message_id)``, and surfaced as "the local
    group database could not commit the change safely".
    """
    path = tmp_path / "rooms.json"
    seeded = await _seeded_service(path, turns=6)
    room_id = next(iter(seeded._rooms))

    async def rpc(*_args, **_kwargs):
        return {"ok": True}

    reloaded = ProfileRoomService(
        target_rpc=rpc,
        target_prepare=AsyncMock(return_value=None),
        profile_directory=lambda: ["default", "writer"],
        on_event=None,
        store_path=path,
    )
    await reloaded.send(room_id, "hello")

    room = reloaded._rooms[room_id]
    assert [message["seq"] for message in room["messages"]] == list(range(7))
    assert room["messages"][-1]["content"] == "hello"


@pytest.mark.asyncio
async def test_one_unwritable_room_cannot_block_every_other_write(
    tmp_path: Path,
) -> None:
    """A room whose in-memory sequences drifted must not poison the store.

    Rooms are committed in one transaction, so a single room the writer
    could not place used to fail every unrelated operation in the same
    process — creating a group stopped working because a different group's
    window had drifted. The writer now re-keys a message that moved instead
    of colliding with itself.
    """
    path = tmp_path / "rooms.json"
    service = await _seeded_service(path, turns=4)
    room_id = next(iter(service._rooms))
    drifted = service._rooms[room_id]
    for message in drifted["messages"]:
        message["seq"] = int(message["seq"]) + 1
    drifted["nextSeq"] = len(drifted["messages"]) + 1

    await service.create("Second", ["default", "writer"])

    assert len(service._rooms) == 2
    reloaded_ids = {
        room["id"] for room in (await ProfileRoomService(
            target_rpc=AsyncMock(return_value={"ok": True}),
            profile_directory=lambda: ["default", "writer"],
            on_event=None,
            store_path=path,
        ).list(include_messages=False))
    }
    assert reloaded_ids == set(service._rooms)


@pytest.mark.asyncio
async def test_a_refused_write_leaves_memory_untouched(tmp_path: Path) -> None:
    """A failed commit must not change what the process is holding.

    Sequencing ran against the live rooms before the write, so a refused
    transaction left those assignments behind. The caller's rollback restored
    the message list but not the records inside it, and every later write in
    that process inherited the drift.
    """
    path = tmp_path / "rooms.json"
    service = await _seeded_service(path, turns=3)
    room_id = next(iter(service._rooms))
    room = service._rooms[room_id]

    def explode(**_kwargs):
        raise RoomStoreError("disk went away")

    service._sqlite_store.apply = explode  # type: ignore[method-assign]
    # A room adopted from an import or a legacy snapshot reaches memory
    # without coordinates; sequencing is what the write was about to give it.
    for message in room["messages"]:
        message.pop("seq", None)
    room.pop("nextSeq", None)
    stripped = json.loads(json.dumps(room))

    with pytest.raises(ProfileHostError):
        await service._persist()

    assert room == stripped
    assert all("seq" not in message for message in room["messages"])


@pytest.mark.asyncio
async def test_a_cancelled_turn_cannot_desynchronise_the_store(
    tmp_path: Path,
) -> None:
    """Cancelling a write must not leave the store ahead of this process.

    A transaction runs on a worker thread and cannot be interrupted once it
    is under way. Cancelling the coroutine that awaited it — a stopped group
    turn, a gateway shutting down — abandoned the result: the commit landed,
    the in-memory revision stayed behind, and the very next write refused
    itself as a foreign change and cleared every room this process held.
    """
    path = tmp_path / "rooms.json"
    service = await _seeded_service(path, turns=2)
    room_id = next(iter(service._rooms))
    room = service._rooms[room_id]
    released = asyncio.Event()
    inner = service._sqlite_store.apply

    def slow_apply(**kwargs):
        released.set()
        # Long enough that the awaiting coroutine is cancelled while the
        # transaction is still in flight, exactly as a stopped turn does.
        import time
        time.sleep(0.25)
        return inner(**kwargs)

    service._sqlite_store.apply = slow_apply  # type: ignore[method-assign]
    service._append_message(room, {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "committed under cancellation",
        "createdAt": "2026-08-27T00:00:00.000Z",
    })

    writer = asyncio.ensure_future(service._persist())
    await released.wait()
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    service._sqlite_store.apply = inner  # type: ignore[method-assign]
    await service.flush_pending_commits()

    # The next unrelated write must still be accepted.
    await service.create("Second", ["default", "writer"])
    assert len(service._rooms) == 2


def _weighed(content: str, *, tool_arguments: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": content,
        "createdAt": "2026-08-27T00:00:00.000Z",
    }
    if tool_arguments:
        message["role"] = "assistant"
        message["profile"] = "writer"
        message["toolCalls"] = [{
            "id": "call-1", "name": "read_file", "argumentsJson": tool_arguments,
        }]
    return message


def test_a_heavy_room_is_trimmed_long_before_a_thousand_messages() -> None:
    """The window is a residency budget, so it is spent in bytes.

    Counting messages is the wrong measure and quietly so: a thousand short
    replies and a thousand carrying file listings are the same number and
    nowhere near the same room. The window is deep-copied into every snapshot
    and carried in every full update, so the heavy room has to give way first.
    """
    light = [_weighed("ok") for _ in range(200)]
    heavy = [_weighed("here is the tree", tool_arguments="x" * 100_000) for _ in range(200)]

    # Two hundred one-word replies are nothing; nothing leaves.
    assert rooms_module._resident_trim_count(light) == 0
    # The same number of file listings is twenty megabytes; most must leave.
    trimmed = rooms_module._resident_trim_count(heavy)
    assert trimmed > 150
    assert len(heavy) - trimmed >= rooms_module._MIN_RESIDENT_MESSAGES


def test_the_current_turn_survives_however_heavy_it_is() -> None:
    """A floor the budget cannot take away.

    A room whose last turns are each larger than the whole budget would
    otherwise trim itself down to nothing and show the reader an empty
    transcript for the conversation they are having.
    """
    enormous = [
        _weighed("dump", tool_arguments="x" * (rooms_module._RESIDENT_WINDOW_BYTES + 1))
        for _ in range(50)
    ]
    kept = len(enormous) - rooms_module._resident_trim_count(enormous)
    assert kept == rooms_module._MIN_RESIDENT_MESSAGES


def test_the_message_ceiling_still_holds() -> None:
    """Weight replaced the count as the measure, not as the limit.

    The durable store refuses to load a room carrying more than this many
    resident rows, so the window must never hand it one.
    """
    featherweight = [_weighed("") for _ in range(rooms_module._MAX_MESSAGES + 250)]
    kept = len(featherweight) - rooms_module._resident_trim_count(featherweight)
    assert kept == rooms_module._MAX_MESSAGES


def test_weight_counts_what_actually_varies() -> None:
    """Content, tool arguments and inline thumbnails; not the envelope."""
    base = rooms_module._message_weight(_weighed(""))
    assert base == rooms_module._MESSAGE_WEIGHT_OVERHEAD
    assert rooms_module._message_weight(_weighed("x" * 500)) == base + 500
    assert rooms_module._message_weight(
        _weighed("hi", tool_arguments="y" * 400)
    ) == base + 2 + 400

    with_thumbnail = _weighed("hi")
    with_thumbnail["attachments"] = [{
        "fileName": "a.png", "mimeType": "image/png", "thumbnail": "z" * 900,
    }]
    assert rooms_module._message_weight(with_thumbnail) == base + 2 + 900


def _room_service(tmp_path: Path, calls: list, *, running: set[str] | None = None):
    async def rpc(profile: str, method: str, params: dict, _timeout: float):
        calls.append((profile, method, params))
        if method == "sessions.list":
            return {"sessions": [
                {"key": f"desktop:profile-room:{room}"} for room in _SEEDED_SESSIONS
            ] + [{"key": "web:ordinary-chat"}]}
        return {"ok": True}

    return ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: ["default", "writer", "reviewer"],
        on_event=None,
        store_path=tmp_path / "rooms.json",
        target_is_running=(lambda name: name in running) if running is not None else None,
    )


_SEEDED_SESSIONS: list[str] = []


@pytest.mark.asyncio
async def test_a_dropped_member_keeps_nothing_of_the_group(tmp_path: Path) -> None:
    """Removing somebody from a group removes the group from them.

    Their watermark is reset, so a re-add starts clean either way; leaving
    the transcript behind only hid it from view — the room session prefix is
    filtered out of every session list.
    """
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer", "reviewer"])

    calls.clear()
    await service.update(room["id"], "Council", ["default", "writer"])

    deletes = [call for call in calls if call[1] == "sessions.delete"]
    assert [call[0] for call in deletes] == ["reviewer"]
    assert deletes[0][2]["sessionKey"] == f"desktop:profile-room:{room['id']}"


@pytest.mark.asyncio
async def test_tidying_a_group_never_starts_a_bot(tmp_path: Path) -> None:
    """A cold member is left alone; its leftovers wait for its own next run.

    Deleting a group used to reach every member through the path that starts
    one, so tidying up could spin up a process per member — minutes of work,
    and processes the reader never asked for.
    """
    calls: list = []
    service = _room_service(tmp_path, calls, running={"default"})
    room = await service.create("Council", ["default", "writer"])

    calls.clear()
    await service.delete(room["id"])

    deletes = [call for call in calls if call[1] == "sessions.delete"]
    assert [call[0] for call in deletes] == ["default"]


@pytest.mark.asyncio
async def test_a_running_bot_settles_what_groups_left_on_it(tmp_path: Path) -> None:
    """The reconciliation that closes what a cascade cannot reach."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    live = await service.create("Council", ["default", "writer"])
    gone = "9f1d4d70-58c3-4a53-9d0a-3b6b6a2f4f21"
    _SEEDED_SESSIONS[:] = [live["id"], gone]

    calls.clear()
    retired = await service.retire_orphaned_sessions("writer")

    assert retired == 1
    deleted = [
        call[2]["sessionKey"] for call in calls if call[1] == "sessions.delete"
    ]
    # The dead group goes; the live one, and ordinary chats, are untouched.
    assert deleted == [f"desktop:profile-room:{gone}"]


@pytest.mark.asyncio
async def test_a_bot_that_left_a_live_group_is_settled_too(tmp_path: Path) -> None:
    """A membership lost while the bot was stopped is still a leftover."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    _SEEDED_SESSIONS[:] = [room["id"]]

    calls.clear()
    # "reviewer" is not a member of any group, so its copy answers to nothing.
    assert await service.retire_orphaned_sessions("reviewer") == 1


@pytest.mark.asyncio
async def test_a_store_that_will_not_load_sweeps_nothing(tmp_path: Path) -> None:
    """The failure mode that would delete every group transcript in reach.

    An unreadable store leaves no rooms in memory, and an empty live set
    reads as "no group owns anything". Sweeping against it would retire every
    session it could see, so it must refuse instead.
    """
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    _SEEDED_SESSIONS[:] = [room["id"], "9f1d4d70-58c3-4a53-9d0a-3b6b6a2f4f21"]

    # A fresh service that cannot read the store must not conclude anything.
    broken = _room_service(tmp_path, calls)

    async def explode() -> None:
        raise ProfileHostError("ROOM_STORE_INVALID", "unreadable")

    broken._load = explode  # type: ignore[method-assign]
    calls.clear()

    assert await broken.retire_orphaned_sessions("writer") == 0
    assert not [call for call in calls if call[1] == "sessions.delete"]


def _seed_media(service: ProfileRoomService, *names: str) -> Path:
    service._media_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (service._media_dir / name).write_bytes(b"bytes")
    return service._media_dir


def _attach(service: ProfileRoomService, room_id: str, media_id: str) -> None:
    service._rooms[room_id]["messages"] = [{
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "Review the attached files.",
        "createdAt": "2026-08-27T00:00:00.000Z",
        "attachments": [{
            "fileName": "brief.pdf",
            "mimeType": "application/pdf",
            "mediaId": media_id,
            "kind": "file",
            "size": 5,
            "status": "ready",
        }],
    }]


@pytest.mark.asyncio
async def test_an_attachment_no_message_points_at_is_swept(tmp_path: Path) -> None:
    """The other half of taking group files out of the age sweeper's reach.

    That sweeper used to clear crash leftovers as a side effect of pruning by
    age — the same pass that deleted attachments transcripts still pointed at.
    Now that it leaves the prefix alone, the group has to collect its own.
    """
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    kept = f"group-{room['id'][:8]}-kept.pdf"
    media = _seed_media(
        service,
        kept,
        f"group-{room['id'][:8]}-stranded.pdf",
        "vid-generated.mp4",
    )
    _attach(service, room["id"], kept)

    assert await service.retire_orphaned_media() == 1

    # The referenced one stays; so does everything that is not ours to judge.
    assert {path.name for path in media.iterdir()} == {kept, "vid-generated.mp4"}


@pytest.mark.asyncio
async def test_an_attachment_of_another_group_is_not_stranded(tmp_path: Path) -> None:
    """Reference, not room ownership, is what keeps a file.

    The prefix carries a room id, so a sweep that matched on it would delete
    every attachment belonging to a room that happens not to be loaded.
    """
    calls: list = []
    service = _room_service(tmp_path, calls)
    first = await service.create("Council", ["default", "writer"])
    second = await service.create("Panel", ["default", "reviewer"])
    borrowed = f"group-{second['id'][:8]}-shared.pdf"
    media = _seed_media(service, borrowed)
    _attach(service, first["id"], borrowed)

    assert await service.retire_orphaned_media() == 0
    assert {path.name for path in media.iterdir()} == {borrowed}


@pytest.mark.asyncio
async def test_a_store_that_will_not_load_sweeps_no_media(tmp_path: Path) -> None:
    """An unreadable store means an empty referenced set, which reads as
    "no message points at anything" — and would clear the folder."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    media = _seed_media(service, f"group-{room['id'][:8]}-kept.pdf")

    broken = _room_service(tmp_path, calls)

    async def explode() -> None:
        raise ProfileHostError("ROOM_STORE_INVALID", "unreadable")

    broken._load = explode  # type: ignore[method-assign]

    assert await broken.retire_orphaned_media() == 0
    assert len(list(media.iterdir())) == 1


@pytest.mark.asyncio
async def test_the_media_sweep_takes_only_one_turn(tmp_path: Path) -> None:
    """Once per process, because a crash is the only thing that strands a
    file and a crash is what runs this again. Six bots coming up must not
    walk the folder six times."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    media = _seed_media(service, f"group-{room['id'][:8]}-stranded.pdf")

    assert await service.retire_orphaned_media() == 1

    later = f"group-{room['id'][:8]}-later.pdf"
    _seed_media(service, later)
    assert await service.retire_orphaned_media() == 0
    assert {path.name for path in media.iterdir()} == {later}


# ── what a group has spent ──────────────────────────────────────────────────


def _usage_block(prompt, completion, cache_read, cache_write):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _terminal(
    *, prompt=0, completion=0, cache_read=0, cache_write=0, model="m/one", nested=True
):
    """A member's final frame.

    ``nested`` picks which of the two real shapes to send: a gateway puts the
    counts inside the assistant message, the relay bridge lifts them to the
    top of the frame. Both reach a room, so both are exercised.
    """
    usage = _usage_block(prompt, completion, cache_read, cache_write)
    message = {"role": "assistant", "content": [{"type": "text", "text": "done"}]}
    frame = {"state": "final", "model": model, "message": message}
    if nested:
        message["usage"] = usage
    else:
        frame["usage"] = usage
    return frame


def _priced(model="m/one", *, pricing_in=10.0, pricing_out=20.0, cache=1.0):
    from flowly.integrations import model_catalog as mc
    from flowly.integrations.model_catalog import Model

    mc._CACHE["rooms-test"] = [Model(
        id=model, name=model, pricing_in=pricing_in,
        pricing_out=pricing_out, pricing_cache_read=cache,
    )]


def _unprice():
    from flowly.integrations import model_catalog as mc

    mc._CACHE.pop("rooms-test", None)


@pytest.mark.asyncio
async def test_a_group_counts_what_each_member_spent(tmp_path: Path) -> None:
    """Tokens are stored per member; the model comes from the same frame."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]

    service._fold_usage(durable, "writer", _terminal(prompt=1_000, completion=100))
    service._fold_usage(durable, "writer", _terminal(prompt=500, completion=50))
    service._fold_usage(durable, "default", _terminal(prompt=200, completion=20))

    usage = durable["usage"]
    assert usage["turns"] == 3
    assert usage["inputTokens"] == 1_700
    assert usage["outputTokens"] == 170
    assert usage["members"]["writer"] == {
        "calls": 2, "model": "m/one", "inputTokens": 1_500,
        "outputTokens": 150, "cacheReadTokens": 0, "cacheWriteTokens": 0,
    }


@pytest.mark.asyncio
async def test_a_frame_without_usage_counts_nothing(tmp_path: Path) -> None:
    """All-zero means the provider stayed silent, not that a turn was free —
    counting it would inflate the turn count with turns nobody can price."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]

    assert service._fold_usage(durable, "writer", {"state": "final"}) is None
    assert service._fold_usage(durable, "writer", _terminal()) is None
    assert durable["usage"]["turns"] == 0
    assert service._public_usage(durable) is None


@pytest.mark.asyncio
async def test_a_room_is_priced_when_asked_not_when_counted(tmp_path: Path) -> None:
    """The catalogue is a cache that can be cold when a turn lands and warm an
    hour later. Nothing about the stored room changes in between."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]
    service._fold_usage(
        durable, "writer", _terminal(prompt=1_000_000, completion=0, cache_read=800_000)
    )

    _unprice()
    cold = service._public_usage(durable)
    assert cold is not None and "costUsd" not in cold
    assert cold["inputTokens"] == 1_000_000       # tokens are reported regardless

    try:
        _priced()
        warm = service._public_usage(durable)
        # 200K fresh @ $10 + 800K cached @ $1 = $2.00 + $0.80
        assert warm["costUsd"] == pytest.approx(2.80)
        assert warm["costPartial"] is False
        assert warm["members"][0]["costUsd"] == pytest.approx(2.80)
    finally:
        _unprice()


@pytest.mark.asyncio
async def test_one_unpriced_member_makes_the_total_a_floor(tmp_path: Path) -> None:
    """A partial total that says so beats both a wrong total and no total."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]
    service._fold_usage(durable, "writer", _terminal(prompt=1_000, model="m/one"))
    service._fold_usage(durable, "default", _terminal(prompt=1_000, model="m/unknown"))

    try:
        _priced()
        usage = service._public_usage(durable)
        assert usage["costUsd"] == pytest.approx(1_000 * 10.0 / 1_000_000)
        assert usage["costPartial"] is True
    finally:
        _unprice()


@pytest.mark.asyncio
async def test_a_full_breakdown_still_counts_the_room_total(tmp_path: Path) -> None:
    """A cost that stops being complete is worse than a breakdown that stops
    naming everybody — and the projection has to admit which happened."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]
    for index in range(rooms_module._MAX_USAGE_MEMBERS):
        service._fold_usage(durable, f"bot{index}", _terminal(prompt=100))
    service._fold_usage(durable, "latecomer", _terminal(prompt=100))

    assert len(durable["usage"]["members"]) == rooms_module._MAX_USAGE_MEMBERS
    assert "latecomer" not in durable["usage"]["members"]
    assert durable["usage"]["inputTokens"] == 2_500      # every turn counted
    try:
        _priced()
        assert service._public_usage(durable)["costPartial"] is True
    finally:
        _unprice()


@pytest.mark.asyncio
async def test_a_meter_survives_a_reload(tmp_path: Path) -> None:
    """The counter is durable, so it is the room's life and not the process's."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    service._fold_usage(
        service._rooms[room["id"]], "writer", _terminal(prompt=1_234, completion=56)
    )
    await service._persist()

    reloaded = _room_service(tmp_path, calls)
    listed = await reloaded.list(include_messages=False)
    usage = listed[0]["usage"]
    assert usage["turns"] == 1
    assert usage["inputTokens"] == 1_234
    assert usage["members"][0]["profile"] == "writer"


@pytest.mark.asyncio
async def test_a_broken_meter_never_costs_a_room(tmp_path: Path) -> None:
    """Every other loader refuses a malformed record, and rightly. A cost
    meter is not one: nothing depends on it, so it restarts at zero rather
    than making an intact transcript unopenable."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    service._fold_usage(service._rooms[room["id"]], "writer", _terminal(prompt=10))
    await service._persist()

    path = _sqlite_path(tmp_path / "rooms.json")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE rooms SET usage_json = ?", ('{"turns": "lots"}',))

    reloaded = _room_service(tmp_path, calls)
    listed = await reloaded.list(include_messages=False)
    assert listed[0]["title"] == "Council"          # the room still opens
    assert listed[0]["usage"] is None               # the meter simply restarts


def test_the_store_upgrades_a_v2_database_in_place(tmp_path: Path) -> None:
    """A database that predates the meter gains the column and keeps its
    rooms. The chain applies steps in order, so a v1 install that skipped a
    release still lands on the current schema."""
    path = tmp_path / "rooms.sqlite3"
    store = SQLiteRoomStore(path)
    room_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    store.initialize_verified({room_id: {
        "id": room_id, "title": "Old",
        "mode": "panel", "members": ["default"], "watermarks": {"default": 0},
        "messages": [], "createdAt": "2026-08-01T00:00:00.000Z",
        "updatedAt": "2026-08-01T00:00:00.000Z",
    }})
    # Pose as the previous schema, column and all.
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE rooms DROP COLUMN usage_json")
        connection.execute("PRAGMA user_version = 2")

    loaded = store.load()

    assert [room["title"] for room in loaded.rooms] == ["Old"]
    with sqlite3.connect(path) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == (
            SQLITE_SCHEMA_VERSION
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rooms)")
        }
    assert "usage_json" in columns


@pytest.mark.asyncio
async def test_a_gateway_frame_carries_its_counts_inside_the_message(
    tmp_path: Path,
) -> None:
    """The shape this shipped blind to.

    A gateway nests usage inside the assistant message; only the relay bridge
    lifts it to the top of the frame. Reading just the top level meant every
    group served by a gateway — which is every desktop group — counted
    nothing, and the header stayed empty with no error to show for it.
    """
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]

    service._fold_usage(
        durable, "writer", _terminal(prompt=900, completion=90, nested=True)
    )
    service._fold_usage(
        durable, "default", _terminal(prompt=100, completion=10, nested=False)
    )

    usage = durable["usage"]
    assert usage["turns"] == 2
    assert usage["inputTokens"] == 1_000
    assert usage["members"]["writer"]["inputTokens"] == 900
    assert usage["members"]["default"]["inputTokens"] == 100


@pytest.mark.asyncio
async def test_a_frame_with_no_counts_in_either_place_folds_nothing(
    tmp_path: Path,
) -> None:
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    durable = service._rooms[room["id"]]

    frame = {"state": "final", "model": "m/one", "message": {"content": []}}
    assert service._fold_usage(durable, "writer", frame) is None
    assert durable["usage"]["turns"] == 0


# ── giving the disk back ─────────────────────────────────────────────────────


def _bulky_store(tmp_path: Path, rooms: int = 6, messages: int = 400) -> SQLiteRoomStore:
    """A store big enough that deleting from it leaves real free pages."""
    store = SQLiteRoomStore(tmp_path / "rooms.sqlite3")
    payload = {
        str(uuid.uuid4()): {
            "id": str(uuid.uuid4()), "title": f"Room {index}", "mode": "panel",
            "members": ["default", "writer"], "watermarks": {"default": 0, "writer": 0},
            "messages": [
                {
                    "id": str(uuid.uuid4()), "role": "user",
                    "content": "x" * 512,
                    "createdAt": "2026-08-01T00:00:00.000Z",
                }
                for _ in range(messages)
            ],
            "createdAt": "2026-08-01T00:00:00.000Z",
            "updatedAt": "2026-08-01T00:00:00.000Z",
        }
        for index in range(rooms)
    }
    payload = {room["id"]: room for room in payload.values()}
    store.initialize_verified(payload)
    return store


def _free_pages(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as connection:
        free = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        total = int(connection.execute("PRAGMA page_count").fetchone()[0])
    return free, total


def test_space_is_reclaimed_once_a_deletion_has_left_enough_behind(
    tmp_path: Path,
) -> None:
    """SQLite keeps a deleted room's pages for reuse and never shrinks the
    file on its own. Nothing else here would ever hand them back."""
    store = _bulky_store(tmp_path)
    loaded = store.load()
    keep = loaded.rooms[0]
    store.apply(
        expected_revision=loaded.revision,
        changed_rooms=[],
        deleted_room_ids=[room["id"] for room in loaded.rooms[1:]],
    )
    before = store.path.stat().st_size
    free_before, _total = _free_pages(store.path)
    assert free_before > 0

    report = store.reclaim_space(min_free_bytes=1, min_free_ratio=0.0)

    assert report["vacuumed"] is True
    assert report["freedBytes"] > 0
    assert store.path.stat().st_size < before
    # The room that stayed is still readable afterwards.
    assert [room["id"] for room in store.load().rooms] == [keep["id"]]


def test_a_store_with_little_to_reclaim_is_left_alone(tmp_path: Path) -> None:
    """VACUUM holds an exclusive lock for a full rewrite. A quarter of a tiny
    database is not worth that, which is why there is a floor as well as a
    ratio."""
    store = _bulky_store(tmp_path, rooms=1, messages=5)

    report = store.reclaim_space()

    assert report["vacuumed"] is False
    assert report["skipped"] == "not-worth-it"


def test_reclaiming_is_throttled_across_processes(tmp_path: Path) -> None:
    """The last run is recorded in the store's own meta table, so a second
    Flowly sharing the database throttles against the same mark rather than
    keeping private bookkeeping."""
    store = _bulky_store(tmp_path)
    loaded = store.load()
    store.apply(
        expected_revision=loaded.revision,
        changed_rooms=[],
        deleted_room_ids=[room["id"] for room in loaded.rooms[1:]],
    )
    assert store.reclaim_space(min_free_bytes=1, min_free_ratio=0.0)["vacuumed"] is True

    # Reclaiming leaves nothing to reclaim, so a second call would stop at
    # "not worth it" before the throttle was ever consulted. Free more pages
    # so the throttle is what actually holds it back.
    remaining = store.load()
    store.apply(
        expected_revision=remaining.revision,
        changed_rooms=[],
        deleted_room_ids=[room["id"] for room in remaining.rooms],
    )

    # A different object over the same file is a different process, as far as
    # anything that is not written down is concerned.
    again = SQLiteRoomStore(store.path).reclaim_space(min_free_bytes=1, min_free_ratio=0.0)

    assert again["vacuumed"] is False
    assert again["skipped"] == "throttled"


def test_a_corrupt_mark_reads_as_never_reclaimed(tmp_path: Path) -> None:
    """Unparseable bookkeeping must not freeze maintenance forever."""
    store = _bulky_store(tmp_path)
    loaded = store.load()
    store.apply(
        expected_revision=loaded.revision,
        changed_rooms=[],
        deleted_room_ids=[room["id"] for room in loaded.rooms[1:]],
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO room_store_meta(key, value) VALUES('last_vacuum', 'soon')"
        )

    assert store.reclaim_space(min_free_bytes=1, min_free_ratio=0.0)["vacuumed"] is True


def test_a_missing_store_is_not_an_error(tmp_path: Path) -> None:
    """Maintenance must never be the reason something else fails."""
    report = SQLiteRoomStore(tmp_path / "absent.sqlite3").reclaim_space()
    assert report == {"vacuumed": False, "freedBytes": 0, "skipped": "absent"}


@pytest.mark.asyncio
async def test_the_service_reclaims_at_most_once_a_process(tmp_path: Path) -> None:
    calls: list = []
    service = _room_service(tmp_path, calls)
    await service.create("Council", ["default", "writer"])

    first = await service.reclaim_store_space()
    second = await service.reclaim_store_space()

    assert first["skipped"] != "already"
    assert second["skipped"] == "already"


@pytest.mark.asyncio
async def test_storage_is_reported_per_group_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    """A plan, not an action. Files somebody put in a conversation are theirs;
    the useful thing to do with them is notice, not tidy."""
    calls: list = []
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    media = service._media_dir
    media.mkdir(parents=True, exist_ok=True)
    (media / f"group-{room['id'][:8]}-a.png").write_bytes(b"x" * 2_048)
    (media / f"group-{room['id'][:8]}-b.png").write_bytes(b"x" * 1_024)
    (media / "vid-generated.mp4").write_bytes(b"x" * 9_999)

    report = await service.storage_report()

    assert report["rooms"] == [{
        "roomId": room["id"],
        "title": "Council",
        "mediaBytes": 3_072,
        "overNotice": False,
    }]
    # Generated media is somebody else's accounting.
    assert report["totalBytes"] == 3_072
    assert len(list(media.iterdir())) == 3


@pytest.mark.asyncio
async def test_a_group_over_the_notice_says_so_without_acting(
    tmp_path: Path, monkeypatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(rooms_module, "_ROOM_MEDIA_NOTICE_BYTES", 1_000)
    service = _room_service(tmp_path, calls)
    room = await service.create("Council", ["default", "writer"])
    service._media_dir.mkdir(parents=True, exist_ok=True)
    kept = service._media_dir / f"group-{room['id'][:8]}-big.png"
    kept.write_bytes(b"x" * 4_096)

    report = await service.storage_report()

    assert report["rooms"][0]["overNotice"] is True
    assert report["noticeBytes"] == 1_000
    assert kept.exists()


@pytest.mark.asyncio
async def test_a_store_that_will_not_load_reports_nothing(tmp_path: Path) -> None:
    calls: list = []
    service = _room_service(tmp_path, calls)

    async def explode() -> None:
        raise ProfileHostError("ROOM_STORE_INVALID", "unreadable")

    service._load = explode  # type: ignore[method-assign]

    assert (await service.storage_report())["rooms"] == []
