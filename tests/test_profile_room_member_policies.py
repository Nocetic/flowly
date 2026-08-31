"""Who answers a message nobody addressed.

A group used to answer with everybody, and removing a bot from the
conversation meant removing it from the group. A member can now be kept on
call instead: present, readable, silent until somebody asks for it.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from flowly.profile_host_contract import ProfileHostError
from flowly.profile_room_store import SQLITE_SCHEMA_VERSION, SQLiteRoomStore
from flowly.profile_rooms import (
    MEMBER_POLICY_ALWAYS,
    MEMBER_POLICY_MENTIONED,
    ProfileRoomService,
    _clean_member_policies,
    _member_policies,
)

MEMBERS = ["default", "writer", "reviewer"]


def _service(tmp_path: Path, sends: list[str]) -> ProfileRoomService:
    """A group whose members reply the instant they are asked to."""
    service: ProfileRoomService

    async def rpc(profile: str, method: str, params: dict[str, Any], _t: float):
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
                "message": {"content": f"{profile} replied"},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    service = ProfileRoomService(
        target_rpc=rpc,
        profile_directory=lambda: list(MEMBERS),
        on_event=None,
        store_path=tmp_path / "rooms.json",
    )
    return service


async def _settle(service: ProfileRoomService) -> None:
    for _ in range(60):
        await asyncio.sleep(0.01)
        if not any(service._active.values()):
            return
    raise AssertionError("the group never finished responding")


# --- the shape, decided in one place -----------------------------------


def test_a_member_without_a_policy_answers_as_groups_always_did() -> None:
    assert _member_policies(MEMBERS, None) == {
        member: MEMBER_POLICY_ALWAYS for member in MEMBERS
    }


def test_the_map_is_built_from_membership_so_it_cannot_drift() -> None:
    # A policy for somebody who left does not survive, and a member who
    # arrived without one is not missing. Neither caller has to remember.
    resolved = _member_policies(
        ["default", "writer"],
        {"default": MEMBER_POLICY_MENTIONED, "ghost": MEMBER_POLICY_MENTIONED},
    )
    assert resolved == {
        "default": MEMBER_POLICY_MENTIONED,
        "writer": MEMBER_POLICY_ALWAYS,
    }


def test_a_store_somebody_edited_by_hand_still_opens() -> None:
    # Read leniently: a group that answers beats a group that will not load.
    assert _member_policies(["default"], {"default": "whenever"}) == {
        "default": MEMBER_POLICY_ALWAYS
    }
    assert _member_policies(["default"], "nonsense") == {
        "default": MEMBER_POLICY_ALWAYS
    }


def test_a_request_is_refused_rather_than_reinterpreted() -> None:
    # The same value from a client is a bug in that client, and answering
    # everything instead of saying so would hide it.
    with pytest.raises(ProfileHostError) as invalid:
        _clean_member_policies(["default"], {"default": "whenever"})
    assert invalid.value.code == "INVALID_PARAMS"
    with pytest.raises(ProfileHostError):
        _clean_member_policies(["default"], ["default"])
    with pytest.raises(ProfileHostError):
        _clean_member_policies(["default"], {"default": True})


def test_naming_somebody_who_is_not_a_member_is_not_an_error() -> None:
    # An editor that drops a member and rewrites the policies in one call is
    # doing exactly this, and it means nothing more than the member being gone.
    assert _clean_member_policies(
        ["default"], {"writer": MEMBER_POLICY_MENTIONED}
    ) == {"default": MEMBER_POLICY_ALWAYS}


# --- who runs ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unaddressed_message_skips_a_member_who_is_on_call(
    tmp_path: Path,
) -> None:
    sends: list[str] = []
    service = _service(tmp_path, sends)
    room = await service.create(
        "Council", ["default", "writer"],
        None, {"writer": MEMBER_POLICY_MENTIONED},
    )

    await service.send(room["id"], "morning, what is left today?")
    await _settle(service)

    assert sends == ["default"]


@pytest.mark.asyncio
async def test_being_called_by_name_reaches_a_member_who_is_on_call(
    tmp_path: Path,
) -> None:
    sends: list[str] = []
    service = _service(tmp_path, sends)
    room = await service.create(
        "Council", ["default", "writer"],
        None, {"writer": MEMBER_POLICY_MENTIONED},
    )

    await service.send(room["id"], "@writer take a look")
    await _settle(service)

    assert sends == ["writer"]


@pytest.mark.asyncio
async def test_everyone_means_everyone(tmp_path: Path) -> None:
    # Otherwise the word would not mean what it says: a member who only
    # answers when called has just been called.
    sends: list[str] = []
    service = _service(tmp_path, sends)
    room = await service.create(
        "Council", ["default", "writer"],
        None, {"writer": MEMBER_POLICY_MENTIONED},
    )

    await service.send(room["id"], "@everyone stand-up please")
    await _settle(service)

    assert sorted(sends) == ["default", "writer"]


@pytest.mark.asyncio
async def test_a_group_where_nobody_is_always_records_the_message_and_runs_nothing(
    tmp_path: Path,
) -> None:
    """The one state this feature introduces.

    Every member on call and none of them called. The message belongs to the
    group and is kept, but no run opens: one with an empty membership would
    leave the group reporting itself as responding with nothing to finish it,
    and the runner picks its executor with ``responders[0]``.
    """
    sends: list[str] = []
    service = _service(tmp_path, sends)
    room = await service.create(
        "Council", ["default", "writer"],
        None,
        {"default": MEMBER_POLICY_MENTIONED, "writer": MEMBER_POLICY_MENTIONED},
    )

    result = await service.send(room["id"], "a note to nobody in particular")

    assert sends == []
    assert result["running"] is False
    assert result["activeProfiles"] == []
    assert [message["content"] for message in result["messages"]] == [
        "a note to nobody in particular"
    ]
    # And the group is not wedged: the next mention runs normally.
    await service.send(room["id"], "@writer now you")
    await _settle(service)
    assert sends == ["writer"]


@pytest.mark.asyncio
async def test_an_unanswered_turn_keeps_the_last_real_run_on_the_record(
    tmp_path: Path,
) -> None:
    sends: list[str] = []
    service = _service(tmp_path, sends)
    room = await service.create("Council", ["default", "writer"])
    await service.send(room["id"], "@writer first")
    await _settle(service)

    await service.update(
        room["id"], "Council", ["default", "writer"], None,
        {"default": MEMBER_POLICY_MENTIONED, "writer": MEMBER_POLICY_MENTIONED},
    )
    result = await service.send(room["id"], "unaddressed")

    # Replacing it with an empty run would erase what the group actually did.
    assert set(result["runState"]["members"]) == {"writer"}
    assert result["runState"]["state"] == "completed"
    assert result["running"] is False


# --- staying agreed with membership ------------------------------------


@pytest.mark.asyncio
async def test_dropping_a_member_drops_its_policy(tmp_path: Path) -> None:
    service = _service(tmp_path, [])
    room = await service.create(
        "Council", ["default", "writer"],
        None, {"writer": MEMBER_POLICY_MENTIONED},
    )

    updated = await service.update(room["id"], "Council", ["default", "reviewer"])

    assert updated["memberPolicies"] == {
        "default": MEMBER_POLICY_ALWAYS, "reviewer": MEMBER_POLICY_ALWAYS
    }


@pytest.mark.asyncio
async def test_an_update_that_says_nothing_about_policies_changes_none(
    tmp_path: Path,
) -> None:
    # An older client renaming a group must not silently make every member
    # answer again.
    service = _service(tmp_path, [])
    room = await service.create(
        "Council", ["default", "writer"],
        None, {"writer": MEMBER_POLICY_MENTIONED},
    )

    updated = await service.update(room["id"], "Renamed", ["default", "writer"])

    assert updated["memberPolicies"]["writer"] == MEMBER_POLICY_MENTIONED


@pytest.mark.asyncio
async def test_deleting_a_bot_prunes_it_from_the_groups_it_was_in(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, [])
    room = await service.create(
        "Council", ["default", "writer", "reviewer"],
        None, {"reviewer": MEMBER_POLICY_MENTIONED},
    )

    await service.remove_profile("reviewer")

    survivor = await service.get(room["id"])
    assert survivor["memberPolicies"] == {
        "default": MEMBER_POLICY_ALWAYS, "writer": MEMBER_POLICY_ALWAYS
    }


# --- across a restart, and across an upgrade ---------------------------


@pytest.mark.asyncio
async def test_policies_survive_a_restart(tmp_path: Path) -> None:
    service = _service(tmp_path, [])
    room = await service.create(
        "Council", ["default", "writer"],
        None, {"writer": MEMBER_POLICY_MENTIONED},
    )

    reloaded = _service(tmp_path, [])
    restored = await reloaded.get(room["id"])

    assert restored["memberPolicies"]["writer"] == MEMBER_POLICY_MENTIONED


def test_a_database_from_before_policies_gains_the_column_and_keeps_answering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rooms.sqlite3"
    store = SQLiteRoomStore(path)
    room_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    store.initialize_verified({room_id: {
        "id": room_id, "title": "Old", "mode": "panel",
        "members": ["default", "writer"],
        "watermarks": {"default": 0, "writer": 0},
        "messages": [], "createdAt": "2026-08-01T00:00:00.000Z",
        "updatedAt": "2026-08-01T00:00:00.000Z",
    }})
    # Pose as the previous schema, column and all.
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE rooms DROP COLUMN member_policies_json")
        connection.execute("PRAGMA user_version = 3")

    loaded = store.load()

    assert [room["title"] for room in loaded.rooms] == ["Old"]
    # Absent is not a state a group can be in: it reads as everybody answers,
    # which is what this group did yesterday.
    assert "memberPolicies" not in loaded.rooms[0]
    assert _member_policies(loaded.rooms[0]["members"], None) == {
        "default": MEMBER_POLICY_ALWAYS, "writer": MEMBER_POLICY_ALWAYS
    }
    with sqlite3.connect(path) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == (
            SQLITE_SCHEMA_VERSION
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(rooms)")}
    assert "member_policies_json" in columns


# --- what a client is told ---------------------------------------------


def test_the_host_advertises_the_setting_rather_than_leaving_it_to_be_guessed() -> None:
    advertised = ProfileRoomService.capabilities()["memberPolicies"]
    assert advertised["values"] == [MEMBER_POLICY_ALWAYS, MEMBER_POLICY_MENTIONED]
    assert advertised["default"] == MEMBER_POLICY_ALWAYS
    assert advertised["everyoneOverrides"] is True
    assert advertised["silentWhenNoneAlways"] is True
