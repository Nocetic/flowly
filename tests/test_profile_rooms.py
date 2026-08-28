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
from flowly.gateway.server import GatewayServer
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.profile_room_store import (
    SQLITE_APPLICATION_ID,
    RoomStoreError,
    SQLiteRoomStore,
)
from flowly.profile_rooms import ProfileRoomService


def _sqlite_path(legacy_path: Path) -> Path:
    return legacy_path.with_suffix(".sqlite3")


def _sqlite_message_payloads(legacy_path: Path) -> list[str]:
    with sqlite3.connect(_sqlite_path(legacy_path)) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM room_messages ORDER BY room_id, ordinal"
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
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
    directory = socket()
    server._ws_clients.update({"rooms": rooms, "directory": directory})
    server._bind_profile_client_request("rooms", "profiles.rooms.list", {})
    server._bind_profile_client_request("directory", "profiles.list", {})
    assert server._profile_subscription_uses_default(
        server._profile_client_subscriptions["rooms"]
    ) is True

    await server._broadcast_profile_room_event({
        "hostId": "host", "roomId": "room", "type": "updated",
    })
    assert rooms.messages == [{
        "type": "event",
        "event": "profile.room",
        "data": {"hostId": "host", "roomId": "room", "type": "updated"},
    }]
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
