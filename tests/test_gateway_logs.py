"""The gateway's log as the owner reads it (Settings → Logs)."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import re
import time

import pytest
from loguru import logger

import flowly.profile as profiles
from flowly.gateway_logs import bridge
from flowly.gateway_logs.events import classify, collapse, parse, read_tail
from flowly.gateway_logs.files import current_log_file, file_id
from flowly.gateway_logs.notable import NOTABLE, notable
from flowly.gateway_logs.redact import redact


@pytest.fixture
def captured_plain():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG", format="{level}|{name}|{message}")
    yield lines
    logger.remove(sink)


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
        (0, _line("Telegram connected as @flowly_bot", source="flowly.channels.telegram").rstrip()),
        (100, "ERROR:aiohttp.server:Error handling request from 66.132.186.199"),
        (160, "Traceback (most recent call last):"),
        (200, '  File "web_protocol.py", line 433, in data_received'),
        (260, "aiohttp.http_exceptions.BadHttpMessage: 400, message: Pause on PRI/Upgrade: b''"),
        (330, _line("MCP server 'linear' disconnected: Linear is temporarily unavailable.; reconnecting in 0.8s",
                    "WARNING", "flowly.mcp.client").rstrip()),
    ]
    events = parse(lines, "gateway.log:1:1")
    assert [event["level"] for event in events] == ["info", "error", "warning"]
    assert events[0]["code"] == "channel.connected"
    assert events[0]["params"] == {"channel": "Telegram", "account": "@flowly_bot"}
    assert events[0]["ts"] is not None and events[1]["ts"] is None
    assert all(event["notable"] for event in events)
    assert events[1]["code"] == "net.rejected_request" and events[1]["params"] == {"ip": "66.132.186.199"}
    assert events[1]["detail"].endswith("Pause on PRI/Upgrade: b''")
    assert events[2]["code"] == "mcp.disconnected"
    assert events[2]["params"] == {"server": "linear", "reason": "Linear is temporarily unavailable."}
    assert events[2]["id"] == "gateway.log:1:1@330"


def test_an_unknown_message_keeps_its_words_and_no_code(logs):
    [warning, info] = parse([(0, _line("Something new happened", "WARNING").rstrip()),
                             (1, _line("Session index rebuilt: 799 sessions").rstrip())], "f")
    assert warning["message"] == "Something new happened" and "code" not in warning
    # A warning is worth a look; routine chatter is only in the technical log.
    assert warning["notable"] is True and info["notable"] is False


def test_repeats_collapse_to_the_latest_with_a_count(logs):
    lines = [(index, _line("Lost the connection to Flowly Cloud; retrying in "
                           f"{2 ** index}s: timeout {index}", "WARNING").rstrip()) for index in range(5)]
    lines.insert(2, (99, _line("Other thing", "WARNING").rstrip()))
    collapsed = collapse(parse(lines, "f"))
    assert [(event.get("code"), event["count"]) for event in collapsed] == [(None, 1), ("relay.lost", 5)]
    assert collapsed[-1]["params"] == {"delay": "16", "reason": "timeout 4"}
    # Same message with different ids is still the same thing.
    two = collapse(parse([(0, _line("Retry 1 for job 42", "WARNING").rstrip()),
                          (1, _line("Retry 2 for job 43", "WARNING").rstrip())], "f"))
    assert [event["count"] for event in two] == [2]


def test_slack_reconnects_are_known_by_their_source():
    assert classify("slack_sdk.socket_mode.websockets",
                    "Failed to receive or enqueue a message: ConnectionClosedError, error: no close frame") == (
        "channel.reconnecting", {"channel": "Slack"})
    assert classify("flowly.x", "Slack connection dropped; reconnecting on its own.")[0] == "channel.reconnecting"


def test_app_connections_are_not_events_worth_showing():
    # They happen every minute; they are DEBUG now, and never a code.
    assert classify("flowly.gateway.server", "[WS] Desktop client disconnected: ae4f9228") == (None, {})
    assert classify("flowly.channels.web", "[WebChannel] Browser connected: 14a1df0c") == (None, {})


@pytest.mark.parametrize("code", sorted(NOTABLE))
def test_every_notable_line_is_read_back_as_itself(code, captured_plain):
    params = {name: f"v-{name}" for name in re.findall(r"\{(\w+)\}", NOTABLE[code][1])}
    notable(code, **params)
    [line] = captured_plain
    level, source, message = line.split("|", 2)
    assert level == NOTABLE[code][0]
    # Attributed to whoever called it, so the owner sees where it came from.
    assert source == __name__
    assert classify(source, message.rstrip("\n")) == (code, params)


def test_a_notable_line_never_breaks_its_caller(captured_plain):
    notable("channel.failed", channel="Slack")  # no reason given
    notable("no.such.code")
    assert len(captured_plain) == 2 and all(line.startswith("WARNING|") for line in captured_plain)


def test_a_notable_reason_is_one_short_line(captured_plain):
    notable("routine.failed", name="Brief", reason="line one\nline two " + "x" * 1000)
    [line] = captured_plain
    assert "\n" not in line.rstrip("\n") and len(line) < 400


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
    path.write_text(_line("Gateway started on http://127.0.0.1:18790") + _line("Session index rebuilt: 12")
                    + _line("store ready at /tmp/x", "DEBUG") + _line("Disk almost full", "WARNING"))
    first = logs_events({})
    assert first["available"] and first["reset"]
    # By default: lifecycle moments and problems, not the chatter.
    assert [event["message"] for event in first["events"]] == [
        "Gateway started on http://127.0.0.1:18790", "Disk almost full"]
    assert [event["message"] for event in logs_events({"view": "issues"})["events"]] == ["Disk almost full"]
    # Technical shows every line — except debug, which is never served.
    assert [event["message"] for event in logs_events({"view": "technical"})["events"]] == [
        "Gateway started on http://127.0.0.1:18790", "Session index rebuilt: 12", "Disk almost full"]

    with path.open("a") as handle:
        handle.write(_line("Provider timed out", "ERROR"))
    later = logs_events({"cursor": first["cursor"], "file": first["file"]})
    assert later["reset"] is False
    assert [event["message"] for event in later["events"]] == ["Provider timed out"]


def test_nothing_secret_or_personal_is_served(logs):
    from flowly.channels.feature_rpc import logs_events, logs_tail

    home = str(pathlib.Path.home())
    secret = "sk-proj-abcdefghijklmnopqrstuvwx"
    path = logs / "gateway.log"
    path.write_text(
        _line(f"Provider rejected key {secret} for {home}/projects/app", "ERROR")
        + f"Traceback: Authorization: Bearer abcdefghijklmnop at {home}/x.py\n"
        + _line("Routine 'Brief' failed: token=hunter2secret session 8845d887-ec13-42c6-97a8-a5253622ddca", "ERROR")
    )
    served = repr(logs_events({"view": "technical"})) + repr(logs_tail({}))
    for leaked in (secret, "abcdefghijklmnop", "hunter2secret", home, "ec13-42c6-97a8-a5253622ddca"):
        assert leaked not in served
    events = logs_events({"view": "technical"})["events"]
    assert events[0]["message"] == "Provider rejected key ••• for ~/projects/app"
    assert "Bearer •••" in events[0]["detail"] and "~/x.py" in events[0]["detail"]
    assert events[1]["params"] == {"name": "Brief", "reason": "token=••• session 8845d887…"}


@pytest.mark.parametrize("text, expected", [
    ("key sk-ant-api03-AbCdEfGhIjKlMnOpQrSt used", "key ••• used"),
    ("slack xoxb-1234567890-abcdefghij ok", "slack ••• ok"),
    ("gh ghp_abcdefghijklmnopqrstuvwxyz0123 ok", "gh ••• ok"),
    ("bot 1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawE", "bot •••"),
    ("jwt eyJhbGciOiJI.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4 end", "jwt ••• end"),
    ('password="p a s s" and api_key: abc123', "password=••• and api_key: •••"),
    ("/Users/someone/Library and /home/deploy/.flowly and C:\\Users\\Ada\\x", "~/Library and ~/.flowly and ~\\x"),
    ("plain words, 42 tools, http://127.0.0.1:18790", "plain words, 42 tools, http://127.0.0.1:18790"),
])
def test_redact(text, expected):
    assert redact(text, home="/nowhere") == expected


def test_redact_hides_any_home_directory_but_only_as_a_whole():
    # A server agent runs as root: its home is not under /Users or /home.
    assert redact("File /root/.local/share/flowly/venv/web.py, line 4", home="/root") == (
        "File ~/.local/share/flowly/venv/web.py, line 4")
    assert redact("mounted /rootfs and /root", home="/root") == "mounted /rootfs and ~"


@pytest.mark.parametrize("params", [{"view": "loud"}, {"level": "issues", "view": 3}, {"limit": 0},
                                    {"limit": 501}, {"limit": "5"}])
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
        assert logs_events({"view": "technical"})["events"][0]["message"] == "private"


# ── the lines themselves ───────────────────────────────────────────────────


def test_the_same_push_registration_again_changes_nothing(tmp_path, captured_plain):
    from flowly.push.relay_push import PushRegistry

    store = tmp_path / "push.json"
    registry = PushRegistry(store)
    for _ in range(3):
        registry.register(push_id="f6462950abc", push_secret="s1", gateway_id="g", platform="ios")
    written = store.stat().st_mtime_ns
    assert len(registry.list()) == 1
    assert [line for line in captured_plain if "registered device" in line] == [
        "DEBUG|flowly.push.relay_push|[push] registered device f6462950 (ios)\n"]
    # A changed secret is a real change: saved again.
    time.sleep(0.01)
    registry.register(push_id="f6462950abc", push_secret="s2", gateway_id="g", platform="ios")
    assert store.stat().st_mtime_ns != written
    assert [sub["pushSecret"] for sub in PushRegistry(store).list()] == ["s2"]


def test_a_channel_that_cannot_start_says_so(captured_plain):
    from flowly.channels.manager import ChannelManager

    class Broken:
        async def start(self):
            raise RuntimeError("invalid bot token")

    with pytest.raises(RuntimeError):
        asyncio.run(ChannelManager._run_channel("slack", Broken()))
    [line] = captured_plain
    level, _source, message = line.split("|", 2)
    assert level == "ERROR"
    assert classify("flowly.channels.manager", message.rstrip("\n")) == (
        "channel.failed", {"channel": "Slack", "reason": "invalid bot token"})
