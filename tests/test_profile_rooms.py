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
