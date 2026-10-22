"""D31: the sandbox's computer service (commands, files, lossless delivery).

The brain opens the only connection and must hold the key; the sandbox keeps
just its digest. Nothing is lost or done twice (K9): a command outlives a
dropped connection and a restarted service, a retried id is answered from the
ledger, every output byte arrives once and in order, and a file is only ever
replaced whole and checked.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from flowly_computer import PROTOCOL, commands
from flowly_computer import ledger as ledger_module
from flowly_computer.account import Account
from flowly_computer.auth import authorized, key_digest
from flowly_computer.ledger import Ledger, write_json
from flowly_computer.service import ComputerService

KEY = "brain-key-for-tests"


class Sandbox:
    """One sandbox: its home and ledger outlive the service that serves them."""

    def __init__(self, root: Path):
        self.home = root / "home"
        self.home.mkdir()
        self.ledger = root / "ledger"
        self.client: TestClient | None = None

    async def start(self) -> TestClient:
        service = ComputerService(key_digest(KEY), Account("tester", self.home), self.ledger)
        self.client = TestClient(TestServer(service.app()))
        await self.client.start_server()
        return self.client

    async def stop(self) -> None:
        if self.client:
            await self.client.close()
            self.client = None


@pytest.fixture
async def sandbox(tmp_path):
    box = Sandbox(tmp_path)
    await box.start()
    yield box
    await box.stop()


class Brain:
    """A minimal brain: checks every output byte arrives once, in order."""

    def __init__(self, ws):
        self.ws = ws
        self.output: dict[str, dict[str, bytes]] = {}
        self.count = 0

    async def send(self, method: str, params: dict | None = None, rid: str | None = None) -> str:
        self.count += 1
        rid = rid or f"r{self.count}-{time.monotonic_ns()}"
        await self.ws.send_str(json.dumps({"id": rid, "method": method, "params": params or {}}))
        return rid

    def _take(self, frame: dict) -> None:
        streams = self.output.setdefault(frame["id"], {"stdout": b"", "stderr": b""})
        have = streams[frame["stream"]]
        assert frame["offset"] == len(have), "a byte was skipped or sent twice"
        streams[frame["stream"]] = have + base64.b64decode(frame["data"])

    async def frame(self) -> dict:
        message = await asyncio.wait_for(self.ws.receive(), 10)
        frame = json.loads(message.data)
        if frame.get("event") == "output":
            self._take(frame)
        return frame

    async def answer(self, rid: str) -> dict:
        while True:
            frame = await self.frame()
            if frame.get("id") == rid and not frame.get("event"):
                return frame

    async def call(self, method: str, params: dict | None = None, rid: str | None = None) -> dict:
        return await self.answer(await self.send(method, params, rid))

    def text(self, rid: str, stream: str = "stdout") -> str:
        return self.output.get(rid, {}).get(stream, b"").decode()

    def carry(self, other: "Brain", rid: str) -> dict:
        """Take over what an earlier connection received; the offsets to resume from."""
        self.output[rid] = dict(other.output.get(rid, {"stdout": b"", "stderr": b""}))
        return {k: len(v) for k, v in self.output[rid].items()}


async def connect(box: Sandbox, key: str = KEY) -> Brain:
    ws = await box.client.ws_connect("/computer", headers={"Authorization": f"Bearer {key}"})
    brain = Brain(ws)
    hello = await brain.call("hello", {"protocol": PROTOCOL})
    assert hello["result"]["protocol"] == PROTOCOL
    return brain


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _alive(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


async def _until(condition, seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never held")


# ── the key ────────────────────────────────────────────────────────────────


def test_only_the_key_matches_its_digest():
    digest = key_digest(KEY)
    assert authorized(f"Bearer {KEY}", digest)
    assert not authorized(f"Bearer {digest}", digest)  # the digest the person can read is not a key
    assert not authorized("Bearer other", digest)
    assert not authorized(KEY, digest)
    assert not authorized(None, digest)
    assert not authorized(f"Bearer {KEY}", "")


async def test_a_wrong_key_never_reaches_the_socket(sandbox):
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {key_digest(KEY)}"}):
        with pytest.raises(WSServerHandshakeError) as refused:
            await sandbox.client.ws_connect("/computer", headers=headers)
        assert refused.value.status == 401


async def test_the_first_frame_must_be_hello_with_our_protocol(sandbox):
    ws = await sandbox.client.ws_connect("/computer", headers={"Authorization": f"Bearer {KEY}"})
    await ws.send_str(json.dumps({"id": "1", "method": "exec.run", "params": {"command": "id"}}))
    frame = json.loads((await ws.receive()).data)
    assert frame["error"]["code"] == "PROTOCOL_MISMATCH"
    await ws.receive()
    assert ws.closed


# ── commands ───────────────────────────────────────────────────────────────


async def test_a_command_streams_its_output_and_exit_code(sandbox):
    brain = await connect(sandbox)
    rid = await brain.send("exec.run", {"command": "echo merhaba; echo hata >&2; pwd; exit 3"})
    result = (await brain.answer(rid))["result"]
    assert result["exitCode"] == 3 and not result["timedOut"] and not result["lost"]
    assert brain.text(rid) == f"merhaba\n{sandbox.home}\n"
    assert brain.text(rid, "stderr") == "hata\n"
    assert result["stdoutBytes"] == len(brain.output[rid]["stdout"])
    assert result["stderrBytes"] == len(brain.output[rid]["stderr"])


async def test_bytes_arrive_exactly_even_when_they_are_not_text(sandbox):
    brain = await connect(sandbox)
    rid = await brain.send("exec.run", {"command": "printf 'ç\\377\\000ğ'"})
    await brain.answer(rid)
    assert brain.output[rid]["stdout"] == "ç".encode() + b"\xff\x00" + "ğ".encode()


async def test_the_brain_sends_the_environment_and_the_service_adds_only_the_account(sandbox, monkeypatch):
    monkeypatch.setenv("FLOWLY_COMPUTER_KEY_SHA256", "not-for-commands")
    brain = await connect(sandbox)
    rid = await brain.send("exec.run", {"command": "env", "env": {"PROJECT": "x"}})
    await brain.answer(rid)
    seen = dict(line.split("=", 1) for line in brain.text(rid).splitlines() if "=" in line)
    assert seen["PROJECT"] == "x" and seen["HOME"] == str(sandbox.home)
    assert "FLOWLY_COMPUTER_KEY_SHA256" not in seen


async def test_a_cd_is_reported_when_the_brain_tracks_it_even_after_exit(sandbox):
    (sandbox.home / "proje").mkdir()
    brain = await connect(sandbox)
    result = (await brain.call("exec.run", {"command": "cd proje && exit 4", "trackCwd": True}))["result"]
    assert Path(result["cwd"]) == (sandbox.home / "proje").resolve() and result["exitCode"] == 4
    assert "cwd" not in (await brain.call("exec.run", {"command": "cd proje"}))["result"]


async def test_output_past_the_limit_is_cut_but_the_command_finishes(sandbox):
    brain = await connect(sandbox)
    rid = await brain.send("exec.run", {"command": "head -c 300000 /dev/zero | tr '\\0' a; echo done >&2", "maxOutput": 1000})
    result = (await brain.answer(rid))["result"]
    assert result["truncated"] is True and result["exitCode"] == 0
    assert result["stdoutBytes"] == 1000 == len(brain.output[rid]["stdout"])
    assert brain.text(rid, "stderr") == "done\n"


async def test_a_command_that_floods_the_disk_is_stopped(sandbox, monkeypatch):
    monkeypatch.setattr(commands, "MAX_SPOOL", 100_000)
    brain = await connect(sandbox)
    result = (await brain.call("exec.run", {"command": "yes flood", "maxOutput": 10}))["result"]
    assert result["outputLimit"] is True and result["exitCode"] is None


async def test_a_timeout_kills_the_whole_process_group(sandbox):
    brain = await connect(sandbox)
    marker = "sleep 31.5"
    result = (await brain.call("exec.run", {"command": f"{marker} & {marker}; echo never", "timeout": 1}))["result"]
    assert result["timedOut"] is True and result["exitCode"] is None
    await _until(lambda: not _alive(marker))


async def test_cancel_stops_a_running_command(sandbox):
    brain = await connect(sandbox)
    marker = "sleep 32.5"
    running = await brain.send("exec.run", {"command": f"echo started; {marker}"})
    while not brain.text(running):
        await brain.frame()
    assert (await brain.call("cancel", {"id": running}))["result"]["cancelled"] is True
    result = (await brain.answer(running))["result"]
    assert result["cancelled"] is True and result["exitCode"] is None
    assert brain.text(running) == "started\n"
    await _until(lambda: not _alive(marker))


@pytest.mark.parametrize("params, code", [
    ({"command": ""}, "BAD_REQUEST"),
    ({"command": "id", "timeout": 0}, "BAD_REQUEST"),
    ({"command": "id", "timeout": True}, "BAD_REQUEST"),
    ({"command": "id", "env": {"A=B": "c"}}, "BAD_REQUEST"),
    ({"command": "id", "cwd": "yok"}, "NOT_FOUND"),
    ({"command": "id", "resume": {"stdout": -1}}, "BAD_REQUEST"),
])
async def test_bad_commands_are_refused_before_anything_is_recorded(sandbox, params, code):
    brain = await connect(sandbox)
    assert (await brain.call("exec.run", params))["error"]["code"] == code
    assert (await brain.call("status"))["result"]["unacknowledged"] == 0


async def test_requests_need_valid_fresh_ids_and_known_methods(sandbox):
    brain = await connect(sandbox)
    assert (await brain.call("shell.open"))["error"]["code"] == "UNKNOWN_METHOD"
    assert (await brain.call("status", rid="a/b"))["error"]["code"] == "BAD_REQUEST"
    first = await brain.send("exec.run", {"command": "sleep 1"}, rid="same")
    await brain.send("status", rid="same")
    assert (await brain.answer("same"))["error"]["code"] == "BAD_REQUEST"
    assert (await brain.answer(first))["result"]["exitCode"] == 0


# ── lossless delivery (K9) ─────────────────────────────────────────────────


async def test_a_dropped_connection_loses_no_byte_and_runs_nothing_twice(sandbox):
    brain = await connect(sandbox)
    params = {"command": "echo x >> runs; echo one; sleep 1; echo two; echo err >&2"}
    rid = await brain.send("exec.run", params, rid="job-1")
    while not brain.text(rid):
        await brain.frame()
    await brain.ws.close()  # the link drops mid-command

    again = await connect(sandbox)
    start = again.carry(brain, rid)
    assert start == {"stdout": 4, "stderr": 0}
    result = (await again.call("exec.run", {**params, "resume": start}, rid=rid))["result"]
    assert again.text(rid) == "one\ntwo\n" and again.text(rid, "stderr") == "err\n"
    assert result["exitCode"] == 0 and result["stdoutBytes"] == 8
    assert (sandbox.home / "runs").read_text() == "x\n"


async def test_a_finished_command_is_answered_again_from_the_ledger(sandbox):
    brain = await connect(sandbox)
    params = {"command": "echo x >> runs; echo hello"}
    first = (await brain.call("exec.run", params, rid="job-2"))["result"]
    again = await connect(sandbox)
    second = (await again.call("exec.run", params, rid="job-2"))["result"]
    assert second == first and again.text("job-2") == "hello\n"
    assert (sandbox.home / "runs").read_text() == "x\n"


async def test_an_id_cannot_be_reused_for_another_request(sandbox):
    brain = await connect(sandbox)
    await brain.call("exec.run", {"command": "true"}, rid="job-3")
    assert (await brain.call("exec.run", {"command": "rm -rf ~"}, rid="job-3"))["error"]["code"] == "ID_REUSED"
    assert (await brain.call("fs.remove", {"path": "x"}, rid="job-3"))["error"]["code"] == "ID_REUSED"


async def test_a_command_outlives_a_restart_of_the_service(sandbox):
    brain = await connect(sandbox)
    params = {"command": "echo before; sleep 1; echo after; exit 7"}
    rid = await brain.send("exec.run", params, rid="job-4")
    while not brain.text(rid):
        await brain.frame()
    await sandbox.stop()  # the service goes away (update, crash); the command does not
    await sandbox.start()

    again = await connect(sandbox)
    start = again.carry(brain, rid)
    result = (await again.call("exec.run", {**params, "resume": start}, rid=rid))["result"]
    assert again.text(rid) == "before\nafter\n"
    assert result["exitCode"] == 7 and not result["lost"]


async def test_a_command_lost_with_the_machine_says_so(tmp_path):
    box = Sandbox(tmp_path)
    params = {"command": "sleep 100"}
    entry = Ledger(box.ledger).begin("job-5", "exec.run", params)
    for name in ("stdout", "stderr"):
        entry.path(name).write_bytes(b"half\n" if name == "stdout" else b"")
    (entry.path("run")).mkdir()
    gone = subprocess.Popen(["true"])
    gone.wait()
    write_json(entry.path("job.json"), {"pid": gone.pid, "pgid": gone.pid, "started": time.time(),
                                         "deadline": time.time() + 100, "maxOutput": 1000, "trackCwd": False})
    await box.start()  # the machine came back without the command
    try:
        brain = await connect(box)
        result = (await brain.call("exec.run", params, rid="job-5"))["result"]
        assert result["lost"] is True and result["exitCode"] is None
        assert brain.text("job-5") == "half\n"
    finally:
        await box.stop()


async def test_ack_releases_answers_and_status_counts_the_rest(sandbox):
    brain = await connect(sandbox)
    await brain.call("exec.run", {"command": "true"}, rid="done-1")
    running = await brain.send("exec.run", {"command": "sleep 1"}, rid="running-1")
    await asyncio.sleep(0.2)
    status = (await brain.call("status"))["result"]
    assert status["unacknowledged"] == 2 and status["running"] == 1
    assert (await brain.call("ack", {"ids": ["done-1", "running-1"]}))["result"]["removed"] == 1
    await brain.answer(running)
    await brain.call("ack", {"ids": ["running-1"]})
    assert (await brain.call("status"))["result"]["unacknowledged"] == 0


async def test_a_full_ledger_refuses_new_changes_until_acknowledged(sandbox, monkeypatch):
    monkeypatch.setattr(ledger_module, "MAX_OPEN", 2)
    brain = await connect(sandbox)
    await brain.call("exec.run", {"command": "true"}, rid="a1")
    await brain.call("fs.mkdir", {"path": "d"}, rid="a2")
    assert (await brain.call("exec.run", {"command": "true"}, rid="a3"))["error"]["code"] == "BUSY"
    assert "result" in await brain.call("fs.stat", {"path": "d"})  # reading never needs the ledger
    await brain.call("ack", {"ids": ["a1"]})
    assert (await brain.call("exec.run", {"command": "true"}, rid="a3"))["result"]["exitCode"] == 0


def test_unacknowledged_answers_and_abandoned_uploads_expire(tmp_path):
    book = Ledger(tmp_path)
    book.begin("old", "fs.mkdir", {"path": "x"}).finish({"result": {}})
    book.begin("open", "exec.run", {"command": "sleep 1"})
    upload = tmp_path / ".big.flowly-upload-1"
    upload.write_bytes(b"part")
    book.note_upload(upload)
    later = time.time() + ledger_module.EXPIRES + 1
    assert book.sweep(now=later) == 1
    assert [e.request["id"] for e in book.all()] == ["open"]  # never answered: kept
    assert not upload.exists()


async def test_a_file_change_retried_after_a_crash_counts_as_done(sandbox):
    book = Ledger(sandbox.ledger)
    (sandbox.home / "b.txt").write_text("moved")
    book.begin("mv-1", "fs.move", {"from": "a.txt", "to": "b.txt"})  # recorded, then the service died
    book.begin("rm-1", "fs.remove", {"path": "gone.txt"})
    brain = await connect(sandbox)
    assert (await brain.call("fs.move", {"from": "a.txt", "to": "b.txt"}, rid="mv-1"))["result"]["path"].endswith("b.txt")
    assert (await brain.call("fs.remove", {"path": "gone.txt"}, rid="rm-1"))["result"] == {}
    # A fresh request gets the plain answer.
    assert (await brain.call("fs.remove", {"path": "gone.txt"}))["error"]["code"] == "NOT_FOUND"


async def test_a_repeated_file_change_is_not_done_twice(sandbox):
    brain = await connect(sandbox)
    await brain.call("fs.write", {"path": "a.txt", "data": _b64(b"1")}, rid="w-1")
    await brain.call("fs.move", {"from": "a.txt", "to": "b.txt"}, rid="mv-2")
    (sandbox.home / "a.txt").write_text("new a")
    again = await connect(sandbox)
    assert "result" in await again.call("fs.move", {"from": "a.txt", "to": "b.txt"}, rid="mv-2")
    assert (sandbox.home / "a.txt").read_text() == "new a" and (sandbox.home / "b.txt").read_text() == "1"


# ── files ──────────────────────────────────────────────────────────────────


async def _upload(brain: Brain, path: str, body: bytes, upload: str, *, digest: str | None = None) -> dict:
    piece = 262144
    offsets = list(range(0, len(body), piece)) or [0]
    for offset in offsets[:-1]:
        await brain.call("fs.write", {"path": path, "upload": upload, "offset": offset,
                                      "data": _b64(body[offset:offset + piece])})
    last = offsets[-1]
    return await brain.call("fs.write", {
        "path": path, "upload": upload, "offset": last, "data": _b64(body[last:]), "done": True,
        "size": len(body), "sha256": digest or hashlib.sha256(body).hexdigest(),
    })


async def test_a_big_file_uploads_checked_and_reads_back_whole(sandbox):
    brain = await connect(sandbox)
    body = bytes(range(256)) * 3000  # 768,000 bytes: three pieces
    assert (await _upload(brain, "notlar/a.bin", body, "u1"))["result"]["size"] == len(body)
    assert (sandbox.home / "notlar" / "a.bin").read_bytes() == body
    assert not list((sandbox.home / "notlar").glob(".*"))  # no upload file left behind
    pieces, offset, version = [], 0, None
    while True:
        params = {"path": "~/notlar/a.bin", "offset": offset, **({"version": version} if version else {})}
        part = (await brain.call("fs.read", params))["result"]
        version = part["version"]
        pieces.append(base64.b64decode(part["data"]))
        offset += len(pieces[-1])
        if part["eof"]:
            break
    assert b"".join(pieces) == body and len(pieces) == 3


async def test_a_repeated_upload_piece_changes_nothing(sandbox):
    brain = await connect(sandbox)
    body = b"a" * 262144 + b"b" * 10
    piece = {"path": "x.bin", "upload": "u2", "offset": 0, "data": _b64(body[:262144])}
    await brain.call("fs.write", piece)
    await brain.call("fs.write", piece)  # sent again under a new id: the same bytes at the same place
    last = await brain.call("fs.write", {"path": "x.bin", "upload": "u2", "offset": 262144, "data": _b64(body[262144:]),
                                         "done": True, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    assert last["result"]["size"] == len(body) and (sandbox.home / "x.bin").read_bytes() == body


async def test_an_upload_that_does_not_match_leaves_the_file_alone(sandbox):
    (sandbox.home / "keep.txt").write_text("original")
    brain = await connect(sandbox)
    answer = await _upload(brain, "keep.txt", b"corrupted", "u3", digest="0" * 64)
    assert answer["error"]["code"] == "INTEGRITY"
    assert (sandbox.home / "keep.txt").read_text() == "original"
    assert [p.name for p in sandbox.home.iterdir()] == ["keep.txt"]


async def test_a_file_that_changes_while_it_is_read_is_refused(sandbox):
    target = sandbox.home / "log.txt"
    target.write_bytes(b"a" * 300_000)
    brain = await connect(sandbox)
    first = (await brain.call("fs.read", {"path": "log.txt"}))["result"]
    await asyncio.sleep(0.01)
    target.write_bytes(b"b" * 300_001)
    second = await brain.call("fs.read", {"path": "log.txt", "offset": 262144, "version": first["version"]})
    assert second["error"]["code"] == "CHANGED"


async def test_a_whole_write_replaces_the_file_at_once_and_keeps_its_mode(sandbox):
    script = sandbox.home / "run.sh"
    script.write_text("old")
    script.chmod(0o755)
    brain = await connect(sandbox)
    await brain.call("fs.write", {"path": str(script), "data": _b64(b"new")})
    assert script.read_text() == "new" and script.stat().st_mode & 0o777 == 0o755
    assert [p.name for p in sandbox.home.iterdir()] == ["run.sh"]  # no temporary left behind


async def test_list_stat_mkdir_move_remove(sandbox):
    brain = await connect(sandbox)
    await brain.call("fs.mkdir", {"path": "a/b"})
    await brain.call("fs.write", {"path": "a/b/c.txt", "data": _b64(b"x")})
    listing = (await brain.call("fs.list", {"path": "a/b"}))["result"]
    assert [(e["name"], e["type"], e["size"]) for e in listing["entries"]] == [("c.txt", "file", 1)]
    assert (await brain.call("fs.stat", {"path": "a"}))["result"]["type"] == "directory"
    await brain.call("fs.write", {"path": "d.txt", "data": _b64(b"y")})
    assert (await brain.call("fs.move", {"from": "d.txt", "to": "a/b/c.txt"}))["error"]["code"] == "EXISTS"
    await brain.call("fs.move", {"from": "d.txt", "to": "a/b/c.txt", "overwrite": True})
    assert (sandbox.home / "a/b/c.txt").read_text() == "y"
    assert (await brain.call("fs.remove", {"path": "a"}))["error"]["code"] == "NOT_EMPTY"
    await brain.call("fs.remove", {"path": "a", "recursive": True})
    assert not (sandbox.home / "a").exists()


@pytest.mark.parametrize("method, params, code", [
    ("fs.read", {"path": "yok.txt"}, "NOT_FOUND"),
    ("fs.read", {"path": "."}, "IS_A_DIRECTORY"),
    ("fs.read", {"path": "x", "length": 262145}, "BAD_REQUEST"),
    ("fs.write", {"path": "x", "data": "not base64!"}, "BAD_REQUEST"),
    ("fs.write", {"path": "x", "data": base64.b64encode(b"a" * 262145).decode()}, "TOO_LARGE"),
    ("fs.write", {"path": "x", "upload": "u", "data": "", "done": True}, "BAD_REQUEST"),
    ("fs.list", {"path": "yok"}, "NOT_FOUND"),
    ("fs.remove", {"path": "~"}, "PERMISSION_DENIED"),
    ("fs.remove", {"path": "/"}, "PERMISSION_DENIED"),
    ("fs.stat", {"path": ""}, "BAD_REQUEST"),
])
async def test_file_errors_have_codes(sandbox, method, params, code):
    brain = await connect(sandbox)
    assert (await brain.call(method, params))["error"]["code"] == code


# ── the boundary ───────────────────────────────────────────────────────────


def test_the_service_never_loads_the_brain():
    """K6: the sandbox gets only the hands; nothing of Core's brain comes with them."""
    root = Path(__file__).resolve().parents[1]
    probe = (
        f"import sys; sys.path.insert(0, {str(root)!r});"
        "import flowly_computer.service, flowly_computer.__main__;"
        "print(sorted(m for m in sys.modules if m == 'flowly' or m.startswith('flowly.')))"
    )
    loaded = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True)
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == "[]"


# ── on the sandbox itself (Linux) ──────────────────────────────────────────

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the sandbox is Linux")


@linux
async def test_a_background_process_holding_the_output_is_waited_for_like_a_local_run(sandbox):
    brain = await connect(sandbox)
    started = time.monotonic()
    rid = await brain.send("exec.run", {"command": "(sleep 1; echo late) & echo now"})
    result = (await brain.answer(rid))["result"]
    assert time.monotonic() - started >= 0.9 and brain.text(rid) == "now\nlate\n"
    assert result["exitCode"] == 0


@linux
async def test_a_detached_background_process_is_left_running(sandbox):
    brain = await connect(sandbox)
    marker = "sleep 34.5"
    started = time.monotonic()
    result = (await brain.call("exec.run", {"command": f"nohup {marker} >/dev/null 2>&1 & echo ok"}))["result"]
    assert result["exitCode"] == 0 and time.monotonic() - started < 2
    assert _alive(marker)
    subprocess.run(["pkill", "-f", marker])


@linux
async def test_a_background_process_holding_the_output_runs_into_the_timeout(sandbox):
    brain = await connect(sandbox)
    marker = "sleep 35.5"
    result = (await brain.call("exec.run", {"command": f"{marker} & echo ok", "timeout": 1}))["result"]
    assert result["timedOut"] is True
    await _until(lambda: not _alive(marker))


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(__import__("os"), "geteuid")
    or __import__("os").geteuid() != 0 or "FLOWLY_COMPUTER_TEST_ACCOUNT" not in __import__("os").environ,
    reason="needs root on Linux and a test account (the sandbox's real setup)",
)
async def test_commands_and_files_belong_to_the_application_account(tmp_path):
    import os
    import pwd

    from flowly_computer.account import account

    name = os.environ["FLOWLY_COMPUTER_TEST_ACCOUNT"]
    user = account(name)
    work = user.home / f"t{time.monotonic_ns()}"
    service = ComputerService(key_digest(KEY), user, tmp_path / "ledger")
    box = Sandbox.__new__(Sandbox)
    box.client = TestClient(TestServer(service.app()))
    await box.client.start_server()
    try:
        brain = await connect(box)
        rid = await brain.send("exec.run", {"command": f"id -un; mkdir {work}; touch {work}/by-command"})
        await brain.answer(rid)
        assert brain.text(rid).splitlines()[0] == name
        await brain.call("fs.write", {"path": f"{work}/sub/by-service", "data": _b64(b"x")})
        await brain.call("fs.write", {"path": f"{work}/by-command", "data": _b64(b"y")})
        for created in ("by-command", "sub", "sub/by-service"):
            assert pwd.getpwuid((work / created).stat().st_uid).pw_name == name, created
        upload = await _upload(brain, f"{work}/big.bin", b"z" * 300_000, "u9")
        assert upload["result"]["size"] == 300_000
        assert pwd.getpwuid((work / "big.bin").stat().st_uid).pw_name == name
        # The ledger stays the service's: the account cannot read another command's output.
        assert (tmp_path / "ledger").stat().st_mode & 0o077 == 0
    finally:
        await box.client.close()
        subprocess.run(["rm", "-rf", str(work)])
