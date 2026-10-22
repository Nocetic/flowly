"""D31: the sandbox's computer service (step 1: commands and files).

The brain opens the only connection and must hold the key; the sandbox keeps
just its digest. Commands stream their output, stop on timeout or cancel with
their whole process group, and files move in bounded pieces.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from flowly_computer import PROTOCOL
from flowly_computer.account import Account
from flowly_computer.auth import authorized, key_digest
from flowly_computer.service import ComputerService

KEY = "brain-key-for-tests"


@pytest.fixture
async def computer(tmp_path):
    account = Account("tester", tmp_path)
    client = TestClient(TestServer(ComputerService(key_digest(KEY), account).app()))
    await client.start_server()
    yield client, tmp_path
    await client.close()


class Brain:
    def __init__(self, ws):
        self.ws = ws
        self.events: dict[str, list[dict]] = {}
        self.count = 0

    async def send(self, method: str, params: dict | None = None, rid: str | None = None) -> str:
        self.count += 1
        rid = rid or f"r{self.count}"
        await self.ws.send_str(json.dumps({"id": rid, "method": method, "params": params or {}}))
        return rid

    async def answer(self, rid: str) -> dict:
        while True:
            frame = json.loads((await asyncio.wait_for(self.ws.receive(), 10)).data)
            if frame.get("event"):
                self.events.setdefault(frame["id"], []).append(frame)
            elif frame["id"] == rid:
                return frame
            else:
                self.events.setdefault(frame["id"], []).append(frame)

    async def call(self, method: str, params: dict | None = None) -> dict:
        return await self.answer(await self.send(method, params))

    def output(self, rid: str, stream: str = "stdout") -> str:
        return "".join(e["data"] for e in self.events.get(rid, []) if e.get("stream") == stream)


async def connect(client, key: str = KEY) -> Brain:
    ws = await client.ws_connect("/computer", headers={"Authorization": f"Bearer {key}"})
    brain = Brain(ws)
    hello = await brain.call("hello", {"protocol": PROTOCOL})
    assert hello["result"]["protocol"] == PROTOCOL
    return brain


# ── the key ────────────────────────────────────────────────────────────────


def test_only_the_key_matches_its_digest():
    digest = key_digest(KEY)
    assert authorized(f"Bearer {KEY}", digest)
    assert not authorized(f"Bearer {digest}", digest)  # the digest the person can read is not a key
    assert not authorized("Bearer other", digest)
    assert not authorized(KEY, digest)
    assert not authorized(None, digest)
    assert not authorized(f"Bearer {KEY}", "")


async def test_a_wrong_key_never_reaches_the_socket(computer):
    client, _ = computer
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {key_digest(KEY)}"}):
        with pytest.raises(WSServerHandshakeError) as refused:
            await client.ws_connect("/computer", headers=headers)
        assert refused.value.status == 401


async def test_the_first_frame_must_be_hello_with_our_protocol(computer):
    client, _ = computer
    ws = await client.ws_connect("/computer", headers={"Authorization": f"Bearer {KEY}"})
    await ws.send_str(json.dumps({"id": "1", "method": "exec.run", "params": {"command": "id"}}))
    frame = json.loads((await ws.receive()).data)
    assert frame["error"]["code"] == "PROTOCOL_MISMATCH"
    await ws.receive()
    assert ws.closed


# ── commands ───────────────────────────────────────────────────────────────


async def test_a_command_streams_its_output_and_exit_code(computer):
    client, home = computer
    brain = await connect(client)
    rid = await brain.send("exec.run", {"command": "echo merhaba; echo hata >&2; pwd; exit 3"})
    answer = await brain.answer(rid)
    assert answer["result"]["exitCode"] == 3
    assert answer["result"]["timedOut"] is False
    assert brain.output(rid) == f"merhaba\n{home}\n"
    assert brain.output(rid, "stderr") == "hata\n"


async def test_the_brain_sends_the_environment_and_the_service_adds_only_the_account(computer, monkeypatch):
    client, home = computer
    monkeypatch.setenv("FLOWLY_COMPUTER_KEY_SHA256", "not-for-commands")
    brain = await connect(client)
    rid = await brain.send("exec.run", {"command": "env", "env": {"PROJECT": "x"}})
    await brain.answer(rid)
    seen = dict(line.split("=", 1) for line in brain.output(rid).splitlines() if "=" in line)
    assert seen["PROJECT"] == "x" and seen["HOME"] == str(home)
    assert "FLOWLY_COMPUTER_KEY_SHA256" not in seen


async def test_a_cd_is_reported_when_the_brain_tracks_it(computer):
    client, home = computer
    (home / "proje").mkdir()
    brain = await connect(client)
    answer = await brain.call("exec.run", {"command": "cd proje && echo ok", "trackCwd": True})
    assert Path(answer["result"]["cwd"]) == (home / "proje").resolve()
    assert "cwd" not in (await brain.call("exec.run", {"command": "cd proje"}))["result"]


async def test_output_past_the_limit_is_cut_but_the_command_finishes(computer):
    client, _ = computer
    brain = await connect(client)
    rid = await brain.send("exec.run", {"command": "head -c 300000 /dev/zero | tr '\\0' a; echo done >&2", "maxOutput": 1000})
    answer = await brain.answer(rid)
    assert answer["result"]["truncated"] is True and answer["result"]["exitCode"] == 0
    assert len(brain.output(rid)) == 1000
    assert brain.output(rid, "stderr") == "done\n"


def _alive(marker: str) -> bool:
    found = subprocess.run(["pgrep", "-f", marker], capture_output=True)
    return found.returncode == 0


async def test_a_timeout_kills_the_whole_process_group(computer):
    client, _ = computer
    brain = await connect(client)
    marker = "sleep 31.5"
    answer = await brain.call("exec.run", {"command": f"{marker} & {marker}; echo never", "timeout": 1})
    assert answer["result"]["timedOut"] is True and answer["result"]["exitCode"] is None
    await asyncio.sleep(0.2)
    assert not _alive(marker)


async def test_cancel_stops_a_running_command(computer):
    client, _ = computer
    brain = await connect(client)
    marker = "sleep 32.5"
    running = await brain.send("exec.run", {"command": f"echo started; {marker}"})
    while not brain.output(running):
        frame = json.loads((await asyncio.wait_for(brain.ws.receive(), 10)).data)
        brain.events.setdefault(frame["id"], []).append(frame)
    assert (await brain.call("cancel", {"id": running}))["result"]["cancelled"] is True
    assert (await brain.answer(running))["error"]["code"] == "CANCELLED"
    await asyncio.sleep(0.2)
    assert not _alive(marker)


async def test_closing_the_connection_stops_its_commands(computer):
    client, _ = computer
    brain = await connect(client)
    marker = "sleep 33.5"
    await brain.send("exec.run", {"command": marker})
    await asyncio.sleep(0.3)
    assert _alive(marker)
    await brain.ws.close()
    for _ in range(30):
        if not _alive(marker):
            break
        await asyncio.sleep(0.1)
    assert not _alive(marker)


@pytest.mark.parametrize("params, code", [
    ({"command": ""}, "BAD_REQUEST"),
    ({"command": "id", "timeout": 0}, "BAD_REQUEST"),
    ({"command": "id", "timeout": True}, "BAD_REQUEST"),
    ({"command": "id", "env": {"A=B": "c"}}, "BAD_REQUEST"),
    ({"command": "id", "cwd": "yok"}, "NOT_FOUND"),
])
async def test_bad_commands_are_refused_before_anything_runs(computer, params, code):
    client, _ = computer
    brain = await connect(client)
    assert (await brain.call("exec.run", params))["error"]["code"] == code


async def test_requests_need_fresh_ids_and_known_methods(computer):
    client, _ = computer
    brain = await connect(client)
    assert (await brain.call("shell.open"))["error"]["code"] == "UNKNOWN_METHOD"
    first = await brain.send("exec.run", {"command": "sleep 1"}, rid="same")
    await brain.send("status", rid="same")
    assert (await brain.answer("same"))["error"]["code"] == "BAD_REQUEST"
    assert (await brain.answer(first))["result"]["exitCode"] == 0


# ── files ──────────────────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_files_round_trip_in_pieces(computer):
    client, home = computer
    brain = await connect(client)
    body = bytes(range(256)) * 2048  # 512 KB, two pieces
    first = await brain.call("fs.write", {"path": "notlar/a.bin", "data": _b64(body[:262144])})
    assert first["result"]["size"] == 262144
    await brain.call("fs.write", {"path": "notlar/a.bin", "data": _b64(body[262144:]), "append": True})
    assert (home / "notlar" / "a.bin").read_bytes() == body
    pieces, offset = [], 0
    while True:
        part = (await brain.call("fs.read", {"path": "~/notlar/a.bin", "offset": offset}))["result"]
        pieces.append(base64.b64decode(part["data"]))
        offset += len(pieces[-1])
        if part["eof"]:
            break
    assert b"".join(pieces) == body and len(pieces) == 2


async def test_a_whole_write_replaces_the_file_at_once_and_keeps_its_mode(computer):
    client, home = computer
    script = home / "run.sh"
    script.write_text("old")
    script.chmod(0o755)
    brain = await connect(client)
    await brain.call("fs.write", {"path": str(script), "data": _b64(b"new")})
    assert script.read_text() == "new" and script.stat().st_mode & 0o777 == 0o755
    assert [p.name for p in home.iterdir()] == ["run.sh"]  # no temporary left behind


async def test_list_stat_mkdir_move_remove(computer):
    client, home = computer
    brain = await connect(client)
    await brain.call("fs.mkdir", {"path": "a/b"})
    await brain.call("fs.write", {"path": "a/b/c.txt", "data": _b64(b"x")})
    listing = (await brain.call("fs.list", {"path": "a/b"}))["result"]
    assert [(e["name"], e["type"], e["size"]) for e in listing["entries"]] == [("c.txt", "file", 1)]
    assert (await brain.call("fs.stat", {"path": "a"}))["result"]["type"] == "directory"
    await brain.call("fs.write", {"path": "d.txt", "data": _b64(b"y")})
    assert (await brain.call("fs.move", {"from": "d.txt", "to": "a/b/c.txt"}))["error"]["code"] == "EXISTS"
    await brain.call("fs.move", {"from": "d.txt", "to": "a/b/c.txt", "overwrite": True})
    assert (home / "a/b/c.txt").read_text() == "y"
    assert (await brain.call("fs.remove", {"path": "a"}))["error"]["code"] == "NOT_EMPTY"
    await brain.call("fs.remove", {"path": "a", "recursive": True})
    assert not (home / "a").exists()


@pytest.mark.parametrize("method, params, code", [
    ("fs.read", {"path": "yok.txt"}, "NOT_FOUND"),
    ("fs.read", {"path": "."}, "IS_A_DIRECTORY"),
    ("fs.read", {"path": "x", "length": 262145}, "BAD_REQUEST"),
    ("fs.write", {"path": "x", "data": "not base64!"}, "BAD_REQUEST"),
    ("fs.write", {"path": "x", "data": base64.b64encode(b"a" * 262145).decode()}, "TOO_LARGE"),
    ("fs.list", {"path": "yok"}, "NOT_FOUND"),
    ("fs.remove", {"path": "~"}, "PERMISSION_DENIED"),
    ("fs.remove", {"path": "/"}, "PERMISSION_DENIED"),
    ("fs.stat", {"path": ""}, "BAD_REQUEST"),
])
async def test_file_errors_have_codes(computer, method, params, code):
    client, _ = computer
    brain = await connect(client)
    assert (await brain.call(method, params))["error"]["code"] == code


# ── the boundary ───────────────────────────────────────────────────────────


def test_the_service_never_loads_the_brain():
    """K6: the sandbox gets only the hands; nothing of Core's brain comes with them."""
    probe = (
        "import sys, flowly_computer.service, flowly_computer.__main__;"
        "print(sorted(m for m in sys.modules if m == 'flowly' or m.startswith('flowly.')))"
    )
    loaded = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1])
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == "[]"
