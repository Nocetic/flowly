"""Private, bounded log storage and nonblocking diagnostic ingestion."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

from flowly.mcp.diagnostics import MCPDiagnostics, PrivateLogWriter, sdk_log_scope
from flowly.mcp.stderr_log import StderrCapture


def rows(home):
    return [json.loads(line) for path in sorted((home / "logs/mcp").glob("diagnostics.jsonl*"))
            for line in path.read_text().splitlines()]


def test_rotation_is_private_and_bounded(tmp_path):
    writer = PrivateLogWriter(tmp_path, max_bytes=1024)
    for index in range(100):
        assert writer.append((json.dumps({"index": index, "message": "x" * 100}) + "\n").encode())
    files = list((tmp_path / "logs/mcp").glob("diagnostics.jsonl*"))
    assert len(files) == 3
    assert sum(path.stat().st_size for path in files) <= 3072
    assert max(row["index"] for row in rows(tmp_path)) == 99
    for path in [tmp_path / "logs/mcp", *files]:
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "public", "directory"])
def test_existing_unsafe_log_is_never_opened_or_overwritten(tmp_path, unsafe):
    folder = tmp_path / "logs/mcp"
    folder.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "private-other-file"
    outside.write_text("untouched")
    path = folder / "diagnostics.jsonl"
    if unsafe == "symlink":
        path.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, path)
    elif unsafe == "fifo":
        os.mkfifo(path, mode=0o600)
    elif unsafe == "public":
        path.write_text("public log")
        path.chmod(0o644)
    else:
        path.mkdir()
    assert not PrivateLogWriter(tmp_path).append(b'{"message":"safe"}\n')
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("component", ["logs", "mcp"])
def test_symlinked_log_directory_is_rejected(tmp_path, component):
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    if component == "logs":
        (home / "logs").symlink_to(outside, target_is_directory=True)
    else:
        (home / "logs").mkdir()
        (home / "logs/mcp").symlink_to(outside, target_is_directory=True)
    assert not PrivateLogWriter(home).append(b'{"message":"safe"}\n')
    assert list(outside.iterdir()) == []


def test_busy_cross_process_lock_does_not_block_caller(tmp_path):
    import fcntl

    writer = PrivateLogWriter(tmp_path)
    assert writer.append(b'{}\n')
    with (tmp_path / "logs/mcp/.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        assert not writer.append(b'{}\n')
        assert time.monotonic() - started < 0.5


def test_independent_processes_share_rotation_lock_and_size_quota(tmp_path):
    script = """
import json, sys, time
from pathlib import Path
from flowly.mcp.diagnostics import PrivateLogWriter
writer = PrivateLogWriter(Path(sys.argv[1]), max_bytes=1024)
for index in range(30):
    record = (json.dumps({"worker": sys.argv[2], "index": index, "text": "x" * 100}) + "\\n").encode()
    deadline = time.monotonic() + 2
    while not writer.append(record):
        assert time.monotonic() < deadline, "Log lock never became available"
        time.sleep(.001)
"""
    workers = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(index)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
               for index in range(4)]
    try:
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr.decode()
            assert not stdout
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=2)
    files = list((tmp_path / "logs/mcp").glob("diagnostics.jsonl*"))
    assert len(files) == 3 and sum(path.stat().st_size for path in files) <= 3072
    assert rows(tmp_path)


def test_crashed_writer_releases_lock_and_only_incomplete_tail_is_removed(tmp_path):
    writer = PrivateLogWriter(tmp_path)
    assert writer.append(b'{"retained":true}\n')
    script = """
import fcntl, os, sys
from pathlib import Path
folder = Path(sys.argv[1]) / "logs/mcp"
lock = (folder / ".lock").open("rb")
fcntl.flock(lock, fcntl.LOCK_EX)
with (folder / "diagnostics.jsonl").open("ab", buffering=0) as out:
    out.write(b'{"partial":')
os._exit(23)
"""
    assert subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=5).returncode == 23
    assert writer.append(b'{"new":true}\n')
    assert rows(tmp_path) == [{"retained": True}, {"new": True}]


def test_oversized_preexisting_file_is_not_adopted_or_truncated(tmp_path):
    writer = PrivateLogWriter(tmp_path, max_bytes=1024)
    assert writer.append(b'{}\n')
    path = tmp_path / "logs/mcp/diagnostics.jsonl"
    path.write_bytes(b"x" * 2048)
    assert not writer.append(b'{}\n')
    assert path.stat().st_size == 2048


def test_sdk_logging_routes_only_owned_context_and_never_dumps_bodies(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    sink = MCPDiagnostics("owned", {"env": {"AUTH": "opaque-private"}}, tmp_path)
    remote = logging.getLogger("mcp.client.future_module")
    try:
        with sdk_log_scope(sink):
            remote.debug("full request body must not be logged")
            remote.info("Received session ID: do-not-log-this")
            remote.warning("failed with %s", "opaque-private")
            try:
                raise ValueError('{"refresh_token":"hidden-token"}')
            except ValueError:
                remote.exception("Parse failure")
        remote.warning("Unowned SDK logging still works")
    finally:
        sink.close()
    assert "Unowned SDK logging still works" in caplog.text
    assert "Parse failure" not in caplog.text and "opaque-private" not in caplog.text
    output = json.dumps(rows(tmp_path))
    assert "opaque-private" not in output and "hidden-token" not in output
    assert "do-not-log-this" not in output and "full request body" not in output
    assert "[REDACTED]" in output and "Parse failure" in output
    assert len(rows(tmp_path)) == 2


async def test_concurrent_diagnostic_contexts_cannot_mix_profiles(tmp_path):
    import asyncio

    first = MCPDiagnostics("first", {"env": {"AUTH": "first-secret"}}, tmp_path / "first")
    second = MCPDiagnostics("second", {"env": {"AUTH": "second-secret"}}, tmp_path / "second")

    async def send(sink, message):
        with sdk_log_scope(sink):
            await asyncio.sleep(0)
            logging.getLogger("httpx").error("failure %s", message)

    try:
        await asyncio.gather(send(first, "first-secret"), send(second, "second-secret"))
    finally:
        first.close()
        second.close()
    for name in ("first", "second"):
        output = rows(tmp_path / name)
        assert len(output) == 1 and output[0]["server"] == name
        assert "first-secret" not in str(output) and "second-secret" not in str(output)


def test_pipe_failure_uses_devnull_not_raw_terminal(tmp_path, monkeypatch):
    sink = MCPDiagnostics("fallback", {}, tmp_path)

    def unavailable():
        raise OSError("unavailable")

    monkeypatch.setattr(os, "pipe", unavailable)
    capture = StderrCapture(sink)
    try:
        assert capture.file.name == os.devnull
        assert capture._thread is None
        capture.file.write(b"private-value-must-not-leak\n")
    finally:
        capture.close()
        sink.close()
    output = json.dumps(rows(tmp_path))
    assert "private-value" not in output and "unavailable" in output


def test_disabled_notification_logging_does_not_disable_stderr_security(tmp_path):
    sink = MCPDiagnostics("disabled", {"logging": {"enabled": False}}, tmp_path)
    try:
        sink.emit("emergency", "remote", "do-not-store")
        sink.emit("warning", "stderr", "Error: token=hidden-value", source="stderr")
    finally:
        sink.close()
    output = json.dumps(rows(tmp_path))
    assert "do-not-store" not in output and "hidden-value" not in output
    assert "[REDACTED]" in output


def test_known_multiline_credentials_are_scrubbed_across_separate_lines(tmp_path):
    sink = MCPDiagnostics("multiline-config", {"env": {"AUTH": "first-private-line\nsecond-private-line"}}, tmp_path)
    capture = StderrCapture(sink)
    try:
        capture.file.write(b"first-private-line\nsecond-private-line\n")
    finally:
        capture.close()
        sink.close()
    assert "private-line" not in json.dumps(rows(tmp_path))


def test_python_dict_stderr_is_not_mistaken_for_unterminated_json(tmp_path):
    sink = MCPDiagnostics("python-log", {}, tmp_path)
    capture = StderrCapture(sink)
    try:
        capture.file.write(b"{'api_key': 'hidden-value', 'reason': 'first'}\nError: next message\n")
    finally:
        capture.close()
        sink.close()
    output = rows(tmp_path)
    assert len(output) == 2
    assert "hidden-value" not in str(output)
    assert "next message" in str(output)


@pytest.mark.parametrize("level", ["debug", "notice", "emergency"])
def test_log_levels_roundtrip_in_config_and_filter_locally(tmp_path, level):
    from flowly.config.schema import MCPServerConfig

    config = MCPServerConfig(command="unused", logging={"level": level}).model_dump()
    assert config["logging"] == {"enabled": True, "level": level}
    sink = MCPDiagnostics("levels", config, tmp_path)
    try:
        sink.emit("debug", "remote", "debug-value")
        sink.emit("emergency", "remote", "emergency-value")
    finally:
        sink.close()
    levels = [row["level"] for row in rows(tmp_path)]
    assert levels == (["debug", "emergency"] if level == "debug" else ["emergency"])


def test_protocol_records_redact_data_labels_and_controls(tmp_path):
    sink = MCPDiagnostics("server\nforgery", {"headers": {"X-Vendor": "opaque-private"}}, tmp_path)
    try:
        sink.emit("warning", "logger\x1b[2J", {"api_key": "labelled-private", "detail": "rejected opaque-private"})
    finally:
        sink.close()
    encoded = json.dumps(rows(tmp_path))
    assert "labelled-private" not in encoded and "opaque-private" not in encoded
    assert "[REDACTED]" in encoded
    record = rows(tmp_path)[0]
    assert record["level"] == "warning"
    assert record["source"] == "notification"
    assert "\n" not in record["server"] and "\x1b" not in record["logger"]
    assert sink.snapshot()["written"] == 1
    assert not sink._thread.is_alive()


def test_flood_is_rate_limited_before_expensive_redaction(tmp_path, monkeypatch):
    sink = MCPDiagnostics("flood", {}, tmp_path)
    try:
        for _ in range(2000):
            sink.emit("info", "fixture", "entry")
    finally:
        sink.close()
    state = sink.snapshot()
    assert state["received"] == 2000
    assert state["dropped"] >= 1900
    assert len(rows(tmp_path)) <= 101
    assert state["pending"] == 0


def test_blocked_disk_does_not_block_ingress_and_queue_is_bounded(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def write(self, record):
        entered.set()
        release.wait(2)
        return False

    monkeypatch.setattr(PrivateLogWriter, "append", write)
    sink = MCPDiagnostics("slow-disk", {}, tmp_path)
    try:
        sink.emit("info", None, "one")
        assert entered.wait(1)
        started = time.monotonic()
        for _ in range(200):
            sink.emit("info", None, "queued")
        assert time.monotonic() - started < 0.5
        assert sink.snapshot()["pending"] <= 64
        assert sink.snapshot()["dropped"] > 0
    finally:
        release.set()
        sink.close()
    assert sink.snapshot()["storageErrors"] > 0


def test_stderr_fragmentation_and_per_capture_attribution(tmp_path):
    sink = MCPDiagnostics("stderr", {"env": {"AUTH": "opaque-private"}}, tmp_path)
    first, second = StderrCapture(sink), StderrCapture(sink)
    try:
        for part in (b'Error: {"api_', b'key":"labelled-', b'private"} opaque-', b'private\n'):
            os.write(first.file.fileno(), part)
        os.write(second.file.fileno(), b'Error: unrelated-server-failure\n')
    finally:
        first.close()
        second.close()
        sink.close()
    assert "private" not in first.excerpt()
    assert "[REDACTED]" in first.excerpt()
    assert "unrelated-server" not in first.excerpt()
    assert "unrelated-server" in second.excerpt()
    assert not first._thread.is_alive() and not second._thread.is_alive()
    assert first.file.closed and second.file.closed


@pytest.mark.parametrize("payload", [
    b'{\n "api_key":\n "hidden-value"\n}\n',
    b'-----BEGIN PRIVATE KEY-----\nhidden-value\n-----END PRIVATE KEY-----\n',
    b'password:\n "hidden-value"\n',
    b"password:\n 'hidden-value\nsecond-secret-line'\n",
])
def test_multiline_secret_records_are_not_published_piecewise(tmp_path, payload):
    sink = MCPDiagnostics("multiline", {}, tmp_path)
    capture = StderrCapture(sink)
    try:
        for byte in payload:
            os.write(capture.file.fileno(), bytes([byte]))
    finally:
        capture.close()
        sink.close()
    assert "hidden-value" not in json.dumps(rows(tmp_path))
    assert "hidden-value" not in capture.excerpt()
    assert "second-secret-line" not in capture.excerpt()


def test_oversized_unterminated_line_is_discarded_without_secret_prefix(tmp_path):
    sink = MCPDiagnostics("long-line", {}, tmp_path)
    capture = StderrCapture(sink)
    try:
        for _ in range(64):
            os.write(capture.file.fileno(), b"private-fragment " * 512)
        os.write(capture.file.fileno(), b"\nError: usable next line\n")
    finally:
        capture.close()
        sink.close()
    output = json.dumps(rows(tmp_path))
    assert "private-fragment" not in output
    assert "usable next line" in output
    assert "limit" in output


def test_inherited_open_pipe_cannot_hang_shutdown(tmp_path):
    sink = MCPDiagnostics("inherited", {}, tmp_path)
    capture = StderrCapture(sink)
    inherited = os.dup(capture.file.fileno())
    try:
        started = time.monotonic()
        capture.close()
        assert time.monotonic() - started < 1.5
        assert not capture._thread.is_alive()
    finally:
        os.close(inherited)
        sink.close()
