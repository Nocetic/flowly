"""The gateway's log as the owner reads it (Settings → Logs)."""

from __future__ import annotations

import logging
import os
import time

import pytest
from loguru import logger

import flowly.profile as profiles
from flowly.gateway_logs import bridge
from flowly.gateway_logs.events import classify, collapse, parse, read_tail
from flowly.gateway_logs.files import current_log_file, file_id


@pytest.fixture
def logs(tmp_path, monkeypatch):
    home = tmp_path / "flowly-home"
    (home / "logs").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    if hasattr(profiles, "_cached_home"):
        profiles._cached_home = None
    return home / "logs"


def _line(message: str, level: str = "INFO", source: str = "flowly.gateway.server") -> str:
    return f"2026-10-01 00:13:12.974 | {level:<8} | {source}:run:12 - {message}\n"


# ── which file ─────────────────────────────────────────────────────────────


def test_the_live_log_is_read_before_a_stale_service_capture(logs):
    (logs / "flowly-gateway.err.log").write_text("old capture\n")
    assert current_log_file() == logs / "flowly-gateway.err.log"
    (logs / "gateway.log").write_text(_line("live"))
    assert current_log_file() == logs / "gateway.log"


def test_no_log_yet_is_not_an_error(logs):
    tail = read_tail(None, None)
    assert (tail.available, tail.lines) == (False, [])


# ── following the file ─────────────────────────────────────────────────────


def test_only_complete_lines_are_read_and_the_rest_waits(logs):
    path = logs / "gateway.log"
    path.write_text(_line("first") + "2026-10-01 00:13:13.000 | INFO     | x:y:1 - sec")
    tail = read_tail(None, None)
    assert [text for _o, text in tail.lines] == [_line("first").rstrip("\n")]

    with path.open("a") as handle:
        handle.write("ond\n")
    later = read_tail(tail.cursor, tail.file_id)
    assert [text for _o, text in later.lines] == ["2026-10-01 00:13:13.000 | INFO     | x:y:1 - second"]
    assert later.reset is False
    assert read_tail(later.cursor, later.file_id).lines == []


def test_a_rotated_or_truncated_file_starts_over(logs):
    path = logs / "gateway.log"
    path.write_text(_line("yesterday") * 50)
    tail = read_tail(None, None)
    # Rotation: a new file, soon longer than yesterday's cursor.
    os.rename(path, logs / "gateway.old.log")
    time.sleep(0.01)
    path.write_text(_line("today") * 80)
    rotated = read_tail(tail.cursor, tail.file_id)
    assert rotated.reset is True
    assert rotated.lines[0][1].endswith("today")
    assert len(rotated.lines) == 80
    # Truncation: same file, cursor past the end.
    path.write_text(_line("fresh"))
    assert read_tail(10_000_000, file_id(path)).reset is True


def test_a_new_log_on_a_reused_inode_is_still_a_new_file(logs):
    # Linux hands a freed inode to the next file: rewriting in place keeps the
    # inode, so only the first line tells the new log from the old one.
    path = logs / "gateway.log"
    path.write_text(_line("yesterday") * 5)
    tail = read_tail(None, None)
    path.write_text(_line("today", "WARNING") * 80)
    again = read_tail(tail.cursor, tail.file_id)
    assert again.reset is True
    assert again.lines[0][1].endswith("today") and len(again.lines) == 80


def test_a_fresh_tail_of_a_big_file_skips_the_cut_line(logs, monkeypatch):
    import flowly.gateway_logs.events as events

    monkeypatch.setattr(events, "TAIL_BYTES", 100)
    (logs / "gateway.log").write_text(_line("one") + _line("two") + _line("three"))
    tail = read_tail(None, None)
    assert all(text.startswith("2026-10-01") for _o, text in tail.lines)
    assert tail.lines[-1][1].endswith("three")


# ── reading lines as events ────────────────────────────────────────────────


def test_both_line_shapes_become_events_and_tracebacks_their_detail(logs):
    lines = [
        (0, _line("[WS] Desktop client connected: 8845d887-ec13-42c6-97a8-a5253622ddca").rstrip()),
        (100, "ERROR:aiohttp.server:Error handling request from 66.132.186.199"),
        (160, "Traceback (most recent call last):"),
        (200, '  File "web_protocol.py", line 433, in data_received'),
        (260, "aiohttp.http_exceptions.BadHttpMessage: 400, message: Pause on PRI/Upgrade: b''"),
        (330, _line("MCP server 'linear' disconnected: Linear is temporarily unavailable.; reconnecting in 0.8s",
                    "WARNING", "flowly.mcp.client").rstrip()),
    ]
    events = parse(lines, "gateway.log:1:1")
    assert [event["level"] for event in events] == ["info", "error", "warning"]
    assert events[0]["code"] == "client.connected" and events[0]["params"] == {"surface": "Desktop"}
    assert events[0]["ts"] is not None and events[1]["ts"] is None
    assert events[1]["code"] == "net.rejected_request" and events[1]["params"] == {"ip": "66.132.186.199"}
    assert events[1]["detail"].endswith("Pause on PRI/Upgrade: b''")
    assert events[2]["code"] == "mcp.disconnected"
    assert events[2]["params"] == {"server": "linear", "reason": "Linear is temporarily unavailable."}
    assert events[2]["id"] == "gateway.log:1:1@330"


def test_an_unknown_message_keeps_its_words_and_no_code(logs):
    [event] = parse([(0, _line("Something new happened", "WARNING").rstrip())], "f")
    assert event["message"] == "Something new happened" and "code" not in event


def test_repeats_collapse_to_the_latest_with_a_count(logs):
    lines = [(index, _line(f"[WebChannel] Browser connected: id-{index}").rstrip()) for index in range(5)]
    lines.insert(2, (99, _line("Other thing", "WARNING").rstrip()))
    collapsed = collapse(parse(lines, "f"))
    assert [(event.get("code"), event["count"]) for event in collapsed] == [(None, 1), ("client.connected", 5)]
    # Same message with different ids is still the same thing.
    two = collapse(parse([(0, _line("Retry 1 for job 42", "WARNING").rstrip()),
                          (1, _line("Retry 2 for job 43", "WARNING").rstrip())], "f"))
    assert [event["count"] for event in two] == [2]


def test_slack_reconnects_are_known_by_their_source():
    assert classify("slack_sdk.socket_mode.websockets",
                    "Failed to receive or enqueue a message: ConnectionClosedError, error: no close frame") == (
        "channel.reconnecting", {"channel": "Slack"})
    assert classify("flowly.x", "Slack connection dropped; reconnecting on its own.")[0] == "channel.reconnecting"


def test_an_app_leaving_is_known_with_its_surface():
    assert classify("flowly.gateway.server", "[WS] Desktop client disconnected: ae4f9228") == (
        "client.disconnected", {"surface": "Desktop"})


# ── third-party logging into the log ───────────────────────────────────────


@pytest.fixture
def captured():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG",
                      format="{level}|{name}|{message}{exception}")
    yield lines
    logger.remove(sink)


def _record(name: str, level: int, message: str, error: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(error), error, None) if error is not None else None
    return logging.LogRecord(name, level, "web_protocol.py", 433, message, (), exc_info, func="data_received")


def test_a_scanner_probe_is_one_quiet_line_per_window(captured, monkeypatch):
    from aiohttp.http_exceptions import BadHttpMessage

    clock = iter([1000.0, 1001.0, 1002.0, 1700.0])
    monkeypatch.setattr(bridge.time, "monotonic", lambda: next(clock))
    handler = bridge.LoguruBridge()
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"):
        handler.emit(_record("aiohttp.server", logging.ERROR, f"Error handling request from {ip}",
                             BadHttpMessage("Pause on PRI/Upgrade")))
    assert len(captured) == 2
    assert captured[0].startswith("INFO|aiohttp.server|Rejected 1 malformed request(s) from the internet; latest from 1.1.1.1")
    assert "Rejected 3 malformed request(s)" in captured[1] and "4.4.4.4" in captured[1]
    assert "Traceback" not in "".join(captured)


def test_a_channel_socket_drop_is_a_warning_without_a_traceback(captured):
    bridge.LoguruBridge().emit(_record("slack_sdk.socket_mode.websockets", logging.ERROR,
                                       "Failed to receive or enqueue a message: ConnectionClosedError, error: x"))
    assert captured == ["WARNING|slack_sdk.socket_mode.websockets|Slack connection dropped; reconnecting on its own.\n"]


def test_other_library_errors_keep_their_level_origin_and_exception(captured):
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        record = logging.LogRecord("httpx", logging.ERROR, "client.py", 7, "request failed: %s", ("timeout",),
                                   (type(error), error, error.__traceback__), func="send")
    bridge.LoguruBridge().emit(record)
    assert captured[0].startswith("ERROR|httpx|request failed: timeout")
    assert "RuntimeError: boom" in captured[0]


def test_installing_twice_keeps_one_bridge():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        bridge.install(logging.WARNING)
        bridge.install(logging.WARNING)
        assert sum(isinstance(handler, bridge.LoguruBridge) for handler in root.handlers) == 1
        assert root.level == logging.WARNING
    finally:
        root.handlers = before


# ── the RPCs ───────────────────────────────────────────────────────────────


def test_logs_events_reads_follows_and_filters(logs):
    from flowly.channels.feature_rpc import logs_events

    path = logs / "gateway.log"
    path.write_text(_line("Gateway ready") + _line("Disk almost full", "WARNING"))
    first = logs_events({})
    assert first["available"] and first["reset"]
    assert [event["message"] for event in first["events"]] == ["Gateway ready", "Disk almost full"]
    assert [event["message"] for event in logs_events({"level": "issues"})["events"]] == ["Disk almost full"]

    with path.open("a") as handle:
        handle.write(_line("Provider timed out", "ERROR"))
    later = logs_events({"cursor": first["cursor"], "file": first["file"]})
    assert later["reset"] is False
    assert [event["message"] for event in later["events"]] == ["Provider timed out"]


@pytest.mark.parametrize("params", [{"level": "loud"}, {"limit": 0}, {"limit": 501}, {"limit": "5"}])
def test_logs_events_rejects_bad_params(logs, params):
    from flowly.channels.feature_rpc import FeatureRpcError, logs_events

    with pytest.raises(FeatureRpcError):
        logs_events(params)


def test_logs_tail_still_serves_older_clients(logs):
    from flowly.channels.feature_rpc import logs_tail

    path = logs / "gateway.log"
    path.write_text(_line("one"))
    first = logs_tail({})
    assert first["lines"] == [_line("one").rstrip("\n")] and first["file"]
    with path.open("a") as handle:
        handle.write(_line("two"))
    # A client that predates ``file`` sends only the cursor.
    assert logs_tail({"cursor": first["cursor"]})["lines"] == [_line("two").rstrip("\n")]


def test_an_account_with_shared_access_cannot_read_the_machines_log(logs):
    from flowly.channels.feature_rpc import FeatureRpcError, logs_events, logs_tail
    from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope

    (logs / "gateway.log").write_text(_line("private"))
    with request_owner_scope(RequestOwner(uid="guest")):
        for call in (logs_events, logs_tail):
            with pytest.raises(FeatureRpcError) as denied:
                call({})
            assert denied.value.code == "FORBIDDEN"
    with request_owner_scope(HOST_OWNER):
        assert logs_events({})["events"][0]["message"] == "private"
