"""Opt-in real Linux browser/RFB spike; uses only generated test credentials.

Run in the disposable screen Docker image with FLOWLY_SCREEN_LINUX_TEST=1.
Chromium runs with its sandbox enabled. The disposable Docker runner needs
permission to create user namespaces (see README.md).
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime, is_alive
from flowly.desktop_runtime.rfb import RfbClientGate

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOWLY_SCREEN_LINUX_TEST") != "1", reason="requires isolated Linux screen runner",
)


@contextmanager
def running_screen(directory: Path):
    runtime = ScreenRuntime(directory)
    state = runtime.start()
    try:
        yield dict(endpoint=f"http://127.0.0.1:{state['cdpPort']}",
                   socket=Path(state["socket"]), profile=directory / "browser",
                   processes=state["children"], runtime=runtime, state=state)
    finally:
        runtime.stop(state["generation"])


def recv_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        part = connection.recv(size - len(result))
        assert part, "RFB peer closed"
        result.extend(part)
    return bytes(result)


def test_desktop_processes_do_not_inherit_agent_environment(tmp_path, monkeypatch):
    forbidden = {"OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "CUSTOM_AGENT_TOKEN", "HTTP_PROXY"}
    for key in forbidden:
        monkeypatch.setenv(key, "synthetic-screen-environment-secret")
    wrappers = tmp_path / "executables"
    wrappers.mkdir()
    reports = []
    for program in ("Xvnc", "xfwm4", "xfsettingsd", "xfdesktop", "xfce4-panel", "chromium"):
        executable = shutil.which(program)
        assert executable
        report = tmp_path / (program + ".json")
        reports.append(report)
        # Some applications clear /proc environ after startup. Capture only
        # names and profile locations at the real exec boundary, then exec the
        # actual application in the same PID without altering its environment.
        script = ("#!/usr/bin/python3\nimport json,os,sys\n"
                  f"fd=os.open({str(report)!r},os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n"
                  "with os.fdopen(fd,'w') as output:\n"
                  " json.dump({'names':list(os.environ),'HOME':os.environ.get('HOME'),"
                  "'XDG_DATA_HOME':os.environ.get('XDG_DATA_HOME'),'XAUTHORITY':os.environ.get('XAUTHORITY')},output)\n"
                  f"os.execv({executable!r},[{executable!r},*sys.argv[1:]])\n")
        wrapper = wrappers / program
        wrapper.write_text(script)
        wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(wrappers) + os.pathsep + os.environ["PATH"])
    directory = tmp_path / "environment-profile"
    with running_screen(directory) as screen:
        runtime = screen["runtime"]
        assert screen["state"]["socketLayoutVersion"] == 2
        assert screen["socket"].parent == runtime.application_socket_directory
        assert (runtime.socket_directory / "input.sock").is_socket()
        assert (runtime.socket_directory / "observers.sock").is_socket()
        assert not (runtime.socket_directory / "rfb.sock").exists()
        assert not (runtime.application_socket_directory / "input.sock").exists()
        assert not (runtime.application_socket_directory / "observers.sock").exists()
        for report in reports:
            environment = json.loads(report.read_text())
            assert not forbidden.intersection(environment["names"])
            assert environment["HOME"] == str(directory / "home")
            assert environment["XDG_DATA_HOME"] == str(directory / "data")
            assert environment["XAUTHORITY"].endswith("/Xauthority")
        assert screen["state"]["phase"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["default", "writer"])
@pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_GATEWAY_TEST") != "1", reason="requires full Core Linux runner")
async def test_public_lifecycle_survives_disconnect_and_fences_old_operations(tmp_path, monkeypatch, profile):
    import asyncio
    from unittest.mock import AsyncMock

    import aiohttp
    from aiohttp.test_utils import TestServer

    import flowly.profile as profiles
    from flowly.gateway.server import GatewayServer

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    profiles.create_profile("writer", local_runtime=True)
    runtime = ScreenRuntime(profiles.describe_profile(profile).path / "desktop")
    gateway = GatewayServer(host="127.0.0.1", port=0, auth_token="lifecycle-fixture-token",
                            require_loopback_auth=True, enable_profile_host=True)
    gateway.on_chat_message = AsyncMock()
    server = TestServer(gateway._create_app())
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as client:
            async def connect():
                async with client.post(server.make_url("/api/auth/ws-ticket"),
                                       headers={"Authorization": "Bearer lifecycle-fixture-token"}) as response:
                    ticket = (await response.json())["ticket"]
                return await client.ws_connect(server.make_url("/ws?ticket=" + ticket))
            chat = await connect()
            async def rpc(method, params):
                await chat.send_json({"type": "rpc", "id": "lifecycle", "method": method, "params": params})
                response = await chat.receive_json(timeout=10)
                assert "result" in response, response
                return response["result"]
            async def terminal(operation):
                async with asyncio.timeout(45):
                    while True:
                        result = await rpc("display.operation.status", {**target, "operationId": operation})
                        if result["state"] not in {"pending", "running"}:
                            return result
                        await asyncio.sleep(0.05)
            status = await rpc("display.status", {"profileId": profile})
            assert status["phase"] == "stopped" and status["generation"] is None
            target = {"profileId": profile, "expectedHostId": status["hostId"], "expectedBotId": status["botId"]}
            original = {**target, "operationId": "start-original", "generation": None}
            assert (await rpc("display.start", original))["state"] == "pending"
            await chat.close()
            chat = await connect()
            started = await terminal("start-original")
            assert started["state"] == "succeeded"
            generation = started["resultGeneration"]
            internal = runtime.status()
            assert internal["generation"] == generation and is_alive(internal["supervisor"])
            stopped_request = {**target, "operationId": "stop-original", "generation": generation}
            await rpc("display.stop", stopped_request)
            stopped = await terminal("stop-original")
            assert stopped["state"] == "succeeded"
            assert runtime.status()["phase"] == "stopped" and not is_alive(internal["supervisor"])
            assert await rpc("display.start", original) == started
            assert runtime.status()["phase"] == "stopped"
            await rpc("display.start", {**target, "operationId": "restart", "generation": generation})
            restarted = await terminal("restart")
            assert restarted["state"] == "succeeded" and restarted["resultGeneration"] != generation
            assert await rpc("display.stop", stopped_request) == stopped
            await rpc("display.stop", {**stopped_request, "operationId": "stale-stop"})
            assert (await terminal("stale-stop"))["error"] == "STALE_GENERATION"
            assert runtime.status()["phase"] == "running"
            other = ScreenRuntime(profiles.describe_profile("writer" if profile == "default" else "default").path / "desktop")
            assert other.status() is None
            await chat.close()
    finally:
        await server.close()
        state = runtime.status()
        if state and state["phase"] == "running":
            await asyncio.to_thread(runtime.stop, state["generation"])


@contextmanager
def human_viewer(path):
    gate = RfbClientGate()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        assert recv_exact(connection, 12) == b"RFB 003.008\n"
        connection.sendall(gate.feed(b"RFB 003.008\n", lambda: False))
        count = recv_exact(connection, 1)[0]
        assert 1 in recv_exact(connection, count)
        connection.sendall(gate.feed(b"\x01", lambda: False))
        assert recv_exact(connection, 4) == bytes(4)
        connection.sendall(gate.feed(b"\x00", lambda: False))
        init = recv_exact(connection, 24)
        assert struct.unpack("!HH", init[:4]) == (1440, 900)
        recv_exact(connection, int.from_bytes(init[20:24], "big"))
        yield connection, gate


@pytest.fixture
def login_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'''<!doctype html><html><body style="margin:40px">
              <input id="code" autofocus placeholder="Test code">
              <button id="login" onclick="document.cookie='session=test-login; path=/';
                document.querySelector('#result').textContent='Signed in'">Sign in</button>
              <p id="result"></p></body></html>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_human_rfb_input_and_agent_share_one_browser(tmp_path, login_site):
    from playwright.sync_api import sync_playwright

    with running_screen(tmp_path / "primary") as runtime, sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(runtime["endpoint"])
        page = browser.contexts[0].pages[0]
        page.goto(login_site)
        page.evaluate("""() => { window.clickedCode = false;
            document.querySelector('#code').addEventListener('mousedown', () => {
                window.clickedCode = true;
            }); }""")
        with human_viewer(runtime["socket"]) as (connection, gate):
            # Request a raw rectangle through RFB, independently of CDP capture.
            connection.sendall(gate.feed(struct.pack("!BBHi", 2, 0, 1, 0), lambda: False))
            connection.sendall(gate.feed(struct.pack("!BBHHHH", 3, 0, 0, 0, 32, 32), lambda: False))
            update = recv_exact(connection, 4)
            assert update[0] == 0
            rectangles = int.from_bytes(update[2:4], "big")
            assert rectangles > 0
            total_pixels = 0
            for _ in range(rectangles):
                _, _, width, height, encoding = struct.unpack("!HHHHi", recv_exact(connection, 12))
                assert encoding == 0
                pixels = recv_exact(connection, width * height * 4)
                total_pixels += len(pixels)
            assert total_pixels > 0
            # DOM focus alone does not activate the native X window. A human
            # clicks into the document before typing, through the RFB path.
            point = page.locator("#code").evaluate("""element => {
                const box = element.getBoundingClientRect();
                return {x: Math.round(screenX + box.x + 10),
                        y: Math.round(screenY + outerHeight - innerHeight + box.y + 10)};
            }""")
            for buttons in (0, 1, 0):
                connection.sendall(gate.feed(struct.pack("!BBHH", 5, buttons, point["x"], point["y"]), lambda: True))
            # X window activation is asynchronous. Wait for the real click and
            # focus before sending keys, as an interactive viewer would.
            page.wait_for_function("document.hasFocus() && window.clickedCode")
            for letter in "246810":
                for down in (1, 0):
                    event = struct.pack("!BBHI", 4, down, 0, ord(letter))
                    connection.sendall(gate.feed(event, lambda: True))
            page.wait_for_function("document.querySelector('#code').value === '246810'")
            # A read-only observer's injected input never reaches the real browser.
            connection.sendall(gate.feed(struct.pack("!BBHI", 4, 1, 0, ord("X")), lambda: False))
            assert page.locator("#code").input_value() == "246810"
            # Tab then Return use the real X keyboard path to click Sign in.
            for key in (0xFF09, 0xFF0D):
                for down in (1, 0):
                    connection.sendall(gate.feed(struct.pack("!BBHI", 4, down, 0, key), lambda: True))
            page.wait_for_function("document.querySelector('#result').textContent === 'Signed in'")
        # Viewer close did not end the runtime; the agent sees the exact same session.
        assert page.evaluate("document.cookie") == "session=test-login"
        assert all(is_alive(process) for process in runtime["processes"])
        screenshot = page.screenshot()
        assert screenshot.startswith(b"\x89PNG")
        # Independent attachment, not a new browser or a copied cookie jar.
        second = playwright.chromium.connect_over_cdp(runtime["endpoint"])
        assert second.contexts[0].pages[0].evaluate("document.cookie") == "session=test-login"


def test_input_broker_drains_real_xvnc_and_releases_pressed_keys_before_handback(tmp_path, login_site):
    from playwright.sync_api import sync_playwright

    from flowly.desktop_runtime.input_broker import ScreenInputBroker

    with running_screen(tmp_path / "input-broker") as runtime, sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(runtime["endpoint"])
        page = browser.contexts[0].pages[0]
        page.goto(login_site)
        page.evaluate("""() => {
            window.shiftHeld = false; window.pointerHeld = false;
            addEventListener('keydown', e => { if (e.key === 'Shift') window.shiftHeld = true; });
            addEventListener('keyup', e => { if (e.key === 'Shift') window.shiftHeld = false; });
            addEventListener('mousedown', () => { window.pointerHeld = true; });
            addEventListener('mouseup', () => { window.pointerHeld = false; });
        }""")
        point = page.locator("#code").evaluate("""element => {
            const box = element.getBoundingClientRect();
            return { x: Math.round(screenX + box.x + 10),
                     y: Math.round(screenY + outerHeight - innerHeight + box.y + 10) };
        }""")
        authority = runtime["runtime"].authority
        generation = runtime["state"]["generation"]
        admission = authority.begin_action("original-run", "before", generation)
        assert authority.end_action(admission)
        human = authority.acquire("viewer", generation, authority.snapshot()["epoch"])
        broker = ScreenInputBroker(authority, "viewer", generation, human["epoch"], runtime["socket"])
        try:
            broker.send(b"".join(struct.pack("!BBHH", 5, buttons, point["x"], point["y"])
                                 for buttons in (0, 1, 0)))
            page.wait_for_function("document.hasFocus()")
            broker.send(struct.pack("!BBHI", 4, 1, 0, 0xFFE1)
                        + struct.pack("!BBHH", 5, 1, point["x"], point["y"]))
            page.wait_for_function("window.shiftHeld && window.pointerHeld")
            with pytest.raises(ScreenError, match="INPUT_DRAIN_REQUIRED"):
                authority.release("viewer", generation, human["epoch"])
            released = broker.finish(release=True)
            assert released["control"] == "agent" and released["runId"] == "original-run"
            assert not released["inputDrainRequired"]
            page.wait_for_function("!window.shiftHeld && !window.pointerHeld")
            admission = authority.begin_action("original-run", "after", generation)
            assert authority.end_action(admission)
            authority.finish_run("original-run", generation)
        finally:
            broker.abandon()


def test_supervisor_keeps_input_after_gateway_worker_exit(tmp_path, login_site):
    from playwright.sync_api import sync_playwright

    from flowly.desktop_runtime.input_service import InputServiceClient
    from flowly.desktop_runtime.tickets import StreamScope, StreamTickets
    from flowly.desktop_runtime.viewers import ViewerCredentials

    with running_screen(tmp_path / "supervisor-input") as runtime, sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(runtime["endpoint"])
        page = browser.contexts[0].pages[0]
        page.goto(login_site)
        page.evaluate("""() => {
            window.shiftDowns = 0; window.shiftUps = 0;
            addEventListener('keydown', e => { if (e.key === 'Shift') window.shiftDowns++; });
            addEventListener('keyup', e => { if (e.key === 'Shift') window.shiftUps++; });
        }""")
        point = page.locator("#code").evaluate("""element => {
            const box = element.getBoundingClientRect();
            return {x: Math.round(screenX + box.x + 10),
                    y: Math.round(screenY + outerHeight - innerHeight + box.y + 10)};
        }""")
        authority = runtime["runtime"].authority
        generation = runtime["state"]["generation"]
        viewers = ViewerCredentials(StreamTickets(runtime["profile"].parent / "admission"))
        viewer = viewers.create(StreamScope("owner", "host", "default", generation), "bot")["viewerId"]
        human = authority.acquire(viewer, generation, authority.snapshot()["epoch"])
        identity = dict(generation=generation, viewer=viewer, epoch=human["epoch"])
        path = runtime["runtime"].socket_directory / "input.sock"
        assert runtime["state"]["inputServiceVersion"] == 3
        client = InputServiceClient(path)
        opened = client.request("open", **identity)
        clicks = b"".join(struct.pack("!BBHH", 5, mask, point["x"], point["y"]) for mask in (0, 1, 0))
        client.request("send", **identity, handle=opened["handle"], sequence=0, data=base64.b64encode(clicks).decode())
        page.wait_for_function("document.hasFocus()")
        shift = base64.b64encode(struct.pack("!BBHI", 4, 1, 0, 0xFFE1)).decode()
        request = dict(method="send", **identity, handle=opened["handle"], sequence=1, data=shift)
        script = """import json, sys
from pathlib import Path
from flowly.desktop_runtime.input_service import InputServiceClient
value = json.load(sys.stdin)
print(json.dumps(InputServiceClient(Path(value['path'])).request(**value['request'])))
"""
        child = subprocess.run([sys.executable, "-c", script], input=json.dumps(dict(path=str(path), request=request)),
                               text=True, capture_output=True, timeout=10, check=True)
        assert json.loads(child.stdout) == {"nextSequence": 2}
        page.wait_for_function("window.shiftDowns === 1")
        # The worker has exited. A new worker/client adopts the same broker;
        # replay of its acknowledged input does not press Shift again.
        resumed = InputServiceClient(path).request("open", **identity)
        assert resumed["handle"] == opened["handle"] and resumed["nextSequence"] == 2
        assert client.request(**request) == {"nextSequence": 2}
        released = client.request("viewer.close", **identity, operationId="terminal-close")
        assert released["control"] == "agent" and not released["inputDrainRequired"]
        assert client.request("viewer.close", **identity, operationId="terminal-close") == released
        with pytest.raises(ScreenError, match="VIEWER_REVOKED"):
            client.request("open", **identity)
        page.wait_for_function("window.shiftUps === 1")
        assert page.evaluate("window.shiftDowns") == 1
        assert all(is_alive(process) for process in runtime["processes"])


def test_two_profiles_have_separate_browser_cookies(tmp_path, login_site):
    from playwright.sync_api import sync_playwright

    with (running_screen(tmp_path / "primary") as left,
          running_screen(tmp_path / "research") as right,
          sync_playwright() as playwright):
        first = playwright.chromium.connect_over_cdp(left["endpoint"])
        second = playwright.chromium.connect_over_cdp(right["endpoint"])
        first_page, second_page = first.contexts[0].pages[0], second.contexts[0].pages[0]
        first_page.goto(login_site)
        second_page.goto(login_site)
        first_page.evaluate("document.cookie = 'session=primary; path=/'")
        assert second_page.evaluate("document.cookie") == ""
        second_page.evaluate("document.cookie = 'session=research; path=/'")
        assert first_page.evaluate("document.cookie") == "session=primary"
        assert second_page.evaluate("document.cookie") == "session=research"
        assert left["profile"] != right["profile"]


def test_runtime_survives_starting_client_exit_and_concurrent_adoption(tmp_path, login_site):
    from playwright.sync_api import sync_playwright

    directory = tmp_path / "persistent"
    caller = subprocess.run([
        sys.executable, "-c",
        "import json,sys; from pathlib import Path; "
        "from flowly.desktop_runtime.launcher import ScreenRuntime; "
        "print(json.dumps(ScreenRuntime(Path(sys.argv[1])).start()))",
        str(directory),
    ], capture_output=True, text=True, timeout=40, check=True)
    state = json.loads(caller.stdout)
    runtime = ScreenRuntime(directory)
    try:
        assert is_alive(state["supervisor"])
        assert all(is_alive(child) for child in state["children"])
        # Independent controller instances compete for the same lifecycle lock.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: ScreenRuntime(directory).start(), range(2)))
        assert results == [state, state]
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['cdpPort']}")
            page = browser.contexts[0].pages[0]
            page.goto(login_site)
            page.evaluate("document.cookie='session=persisted; max-age=3600; path=/'")
        human = runtime.authority.acquire("test-viewer", state["generation"], 1)
        with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
            runtime.stop(state["generation"])
        restored = ScreenRuntime(directory)
        assert restored.start() == state
        assert restored.authority.snapshot()["control"] == "human"
        restored.authority.release("test-viewer", state["generation"], human["epoch"])
    finally:
        runtime.stop(state["generation"])
    assert not is_alive(state["supervisor"])
    assert all(not is_alive(child) for child in state["children"])
    with running_screen(directory) as restarted, sync_playwright() as playwright:
        assert restarted["state"]["generation"] != state["generation"]
        browser = playwright.chromium.connect_over_cdp(restarted["endpoint"])
        page = browser.contexts[0].pages[0]
        page.goto(login_site)
        assert page.evaluate("document.cookie") == "session=persisted"


@pytest.mark.asyncio
async def test_agent_provider_uses_human_browser_and_fences_old_references(tmp_path, login_site):
    import asyncio
    import re

    from playwright.async_api import async_playwright

    from flowly.desktop_runtime.browser_provider import HostBrowserProvider

    runtime = ScreenRuntime(tmp_path / "provider")
    state = await asyncio.to_thread(runtime.start)
    provider = HostBrowserProvider(runtime)
    generation = state["generation"]
    run_id = "origin-chat-run"
    count = 0

    async def call(action, **params):
        nonlocal count
        count += 1
        return await provider.request(run_id, str(count), generation, action, params)

    try:
        assert (await call("navigate", url=login_site))["success"]
        snapshot = await call("read_page")
        ref = re.search(r'(ref_\d+) textbox "Test code"', snapshot["elements"])[1]
        assert (await call("type", ref=ref, text="agent-before-human"))["success"]
        assert (await call("screenshot"))["screenshot"].startswith("data:image/jpeg;base64,")
        human = await provider.execution.acquire("test-viewer", generation, 1)
        assert human["control"] == "human"
        for action in ("read_page", "screenshot", "get_page_text"):
            assert (await call(action))["error_code"] == "HUMAN_HAS_CONTROL"
        # Independent attachment to the browser represents the already-tested
        # human RFB path. The provider must see its resulting session intact.
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['cdpPort']}")
            page = browser.contexts[0].pages[0]
            await page.locator("#code").fill("human-login-code")
            await page.locator("#login").click()
            assert await page.evaluate("document.cookie") == "session=test-login"
        runtime.authority.release("test-viewer", generation, human["epoch"])
        assert (await call("type", ref=ref, text="must-not-type"))["error_code"] == "STALE_REFERENCE"
        result = await call("get_page_text")
        assert "Signed in" in result["text"]
        assert "human-login-code" not in result["text"]
        fresh = await call("read_page")
        assert ref not in fresh["elements"]
        assert "human-login-code" not in fresh["elements"]
        assert (await call("evaluate", code="document.cookie"))["error_code"] == "UNSUPPORTED_ACTION"
        assert not provider.observation_valid(snapshot)
        assert provider.observation_valid(fresh)
    finally:
        control = runtime.authority.snapshot()
        if control["control"] == "human":
            runtime.authority.release("test-viewer", generation, control["epoch"])
        await provider.execution.finish_run(run_id, generation)
        await provider.disconnect()
        await asyncio.to_thread(runtime.stop, generation)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_GATEWAY_TEST") != "1", reason="requires full Core Linux runner")
async def test_authenticated_gateway_browser_run_keeps_target_during_handoff(tmp_path, login_site, monkeypatch):
    import asyncio

    import aiohttp
    from aiohttp.test_utils import TestServer
    from playwright.async_api import async_playwright

    import flowly.profile as profiles
    from flowly.agent.loop import AgentLoop
    from flowly.agent.tools.browser_tab import BrowserTabTool
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.desktop_runtime.browser_provider import HostBrowserProvider
    from flowly.gateway.server import GatewayServer
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.delenv("FLOWLY_PROFILE", raising=False)
    runtime = ScreenRuntime(home / "desktop")
    state = await asyncio.to_thread(runtime.start)
    provider = HostBrowserProvider(runtime)
    generation = state["generation"]
    assert (await provider.request("setup", "navigate", generation, "navigate", {"url": login_site}))["success"]
    await provider.execution.finish_run("setup", generation)
    model_started, return_model = asyncio.Event(), asyncio.Event()
    actions = []
    dispatch = provider._dispatch

    async def track_dispatch(action, params):
        actions.append(action)
        return await dispatch(action, params)

    provider._dispatch = track_dispatch
    gateway = GatewayServer(host="127.0.0.1", port=0, auth_token="test-gateway-token", require_loopback_auth=True)
    gateway._host_screen_browser = provider
    gateway._host_screen_identity = {"hostId": profiles.get_or_create_profile_host_id(), "profileId": "default", "screenId": "browser"}
    access = {"botId": profiles.ensure_profile_bot_id("default").bot_id, **gateway._host_screen_identity, "providerId": "host-desktop-browser", "generation": generation}

    class LocalModel(LLMProvider):
        calls = 0

        def get_default_model(self):
            return "test/model"

        async def chat(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                model_started.set()
                # Only the model response is held to exercise takeover during
                # an in-flight model call. The production agent loop owns the
                # subsequent human wait and explicit continuation.
                await return_model.wait()
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="stale-click", name="browser_tab",
                    arguments={"action": "click", "selector": "#login"})])
            if self.calls == 2:
                assert "fresh browser observation" in str(kwargs["messages"])
                assert "ref_" in str(kwargs["messages"])
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="read-login", name="browser_tab",
                    arguments={"action": "get_page_text"})])
            assert "Signed in" in str(kwargs["messages"])
            return LLMResponse(content="Signed in in the original task")

    model = LocalModel(api_key="test")
    loop = AgentLoop(bus=MessageBus(), provider=model, workspace=tmp_path / "workspace", main_config=Config(),
                     max_iterations=4, soft_warn_at_iteration=0)
    loop.tools.register(BrowserTabTool(gateway))

    async def on_chat(*args):
        result = await loop._run_llm_tool_loop(
            [{"role": "system", "content": "Complete the browser task."}, {"role": "user", "content": "Read the login result."}],
            action_turn=False, session_key=args[0], outbound_run_id=args[2])
        return result[0], {"aborted": loop.is_run_aborted(args[2])}

    gateway.on_chat_message = on_chat
    server = TestServer(gateway._create_app())
    await server.start_server()
    viewer = None
    try:
        async with aiohttp.ClientSession() as session:
            url = str(server.make_url("/ws?clientId=screen-desktop")).replace("http://", "ws://", 1)
            with pytest.raises(aiohttp.WSServerHandshakeError) as unauthenticated:
                await session.ws_connect(url)
            assert unauthenticated.value.status == 401
            async with session.post(server.make_url("/api/auth/ws-ticket"),
                                    headers={"Authorization": "Bearer test-gateway-token"}) as response:
                ticket = (await response.json())["ticket"]
            async with session.ws_connect(url + "&ticket=" + ticket) as desktop:
                serial = 0
                events = []

                async def rpc(method, params):
                    nonlocal serial
                    serial += 1
                    request_id = f"request-{serial}"
                    await desktop.send_json({"type": "rpc", "id": request_id, "method": method, "params": params})
                    async with asyncio.timeout(15):
                        while True:
                            frame = await desktop.receive_json()
                            if frame.get("type") == "rpc" and frame.get("id") == request_id:
                                assert "error" not in frame, frame
                                return frame["result"]
                            events.append(frame)

                status = await rpc("display.status", {"profileId": "default"})
                pinned = {"profileId": "default", "expectedHostId": status["hostId"],
                          "expectedBotId": status["botId"], "generation": generation}
                viewer = await rpc("display.viewer.create", pinned)
                started = await rpc("chat.send", {"sessionKey": "screen:test", "message": "work",
                    "idempotencyKey": "origin-run", "browserAccess": access})
                assert started["runId"] == "origin-run"
                await asyncio.wait_for(model_started.wait(), 15)
                controlled = {**pinned, "viewerToken": viewer["viewerToken"]}
                acquired = await rpc("display.control.acquire", {**controlled, "epoch": 1, "operationId": "take-control"})
                assert acquired["control"]["control"] == "human"
                assert acquired["control"]["runId"] == "origin-run"
                return_model.set()
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['cdpPort']}")
                    page = browser.contexts[0].pages[0]
                    await page.locator("#code").fill("human-completed-login")
                    await page.locator("#login").click()
                released = await rpc("display.control.release", {**controlled, "epoch": acquired["control"]["epoch"],
                                                                  "operationId": "give-back"})
                status = await rpc("display.status", {"profileId": "default"})
                assert status["handoff"]["state"] == "waiting_resume"
                assert status["handoff"]["originRunId"] == "origin-run"
                await asyncio.sleep(0.3)
                assert model.calls == 1
                assert actions == []
                resumed = await rpc("display.run.resume", {**controlled, "epoch": released["control"]["epoch"],
                    "operationId": "continue-original", "handoffId": status["handoff"]["handoffId"]})
                assert resumed["requested"] is True
                async with asyncio.timeout(15):
                    while not any(f.get("data", {}).get("state") == "final" for f in events):
                        events.append(await desktop.receive_json())
                final = next(f for f in events if f.get("data", {}).get("state") == "final")
                assert final["data"]["runId"] == "origin-run"
                assert "Signed in" in final["data"]["message"]["content"][0]["text"]
                assert model.calls == 3
                assert actions == ["read_page", "get_page_text"]
    finally:
        return_model.set()
        await server.close()
        loop.stop()
        control = runtime.authority.snapshot()
        if control["control"] == "human" and viewer is not None:
            runtime.authority.release(viewer["viewerId"], generation, control["epoch"])
        await provider.execution.finish_run("origin-run", generation)
        await provider.disconnect()
        await asyncio.to_thread(runtime.stop, generation)


@pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_GATEWAY_TEST") != "1", reason="requires full Core Linux runner")
def test_real_cli_starts_adopts_and_stops_persistent_screen(tmp_path):
    home = tmp_path / "cli-home"
    env = {**os.environ, "FLOWLY_HOME": str(home)}
    env.pop("FLOWLY_PROFILE", None)
    runtime = ScreenRuntime(home / "desktop")

    def command(name):
        result = subprocess.run([sys.executable, "-m", "flowly.cli.entry", "screen", name],
                                env=env, capture_output=True, text=True, timeout=40, check=True)
        return json.loads(result.stdout)

    try:
        assert command("capabilities")["supported"]
        report = command("doctor")
        assert report["ready"] and report["doctorVersion"] == 1
        assert report["resources"]["memory"]["availableBytes"] >= report["resources"]["memory"]["requiredBytes"]
        started = command("start")
        assert started["phase"] == "running"
        assert command("status")["generation"] == started["generation"]
        assert command("start")["generation"] == started["generation"]
        internal = runtime.status()
        assert is_alive(internal["supervisor"])
        assert "socket" not in started and "cdpPort" not in started
        assert command("stop")["phase"] == "stopped"
        assert not is_alive(internal["supervisor"])
    finally:
        state = runtime.status()
        if state is not None and state["phase"] == "running":
            runtime.stop(state["generation"])


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["default", "writer"])
@pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_GATEWAY_TEST") != "1", reason="requires full Core Linux runner")
async def test_authenticated_display_endpoint_streams_real_profile_pixels(tmp_path, monkeypatch, profile):
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import aiohttp
    from aiohttp.test_utils import TestServer
    from playwright.async_api import async_playwright

    import flowly.profile as profiles
    from flowly.gateway.display import STREAM_PATH
    from flowly.gateway.server import GatewayServer
    from tests.test_screen_display_gateway import RfbSocket

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    profiles.create_profile("writer", local_runtime=True)
    runtime = ScreenRuntime(profiles.describe_profile(profile).path / "desktop")
    state = await asyncio.to_thread(runtime.start)
    gateway = GatewayServer(host="127.0.0.1", port=0, auth_token="test-display-token",
                            require_loopback_auth=True, enable_profile_host=True)
    gateway.on_chat_message = AsyncMock()
    server = TestServer(gateway._create_app())
    await server.start_server()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['cdpPort']}")
            page = browser.contexts[0].pages[0]
            await page.set_content(f"<html><body style='background:#237bb4'>{profile} browser fixture<input id='code'></body></html>")
            await page.evaluate("""() => {
                window.keys = [];
                for (const name of ['keydown', 'keyup']) addEventListener(name, e => window.keys.push([name, e.key]));
            }""")
            await page.bring_to_front()
            async with aiohttp.ClientSession() as client:
                auth = {"Authorization": "Bearer test-display-token"}
                async with client.post(server.make_url("/api/auth/ws-ticket"), headers=auth) as response:
                    ticket = (await response.json())["ticket"]
                async with client.ws_connect(server.make_url("/ws?ticket=" + ticket)) as chat:
                    await chat.send_json({"type": "rpc", "id": "status", "method": "display.status",
                                          "params": {"profileId": profile}})
                    status = (await chat.receive_json())["result"]
                    assert status["generation"] == state["generation"]
                    assert status["profileId"] == profile
                    assert status["statusRevision"] >= 1
                    assert "socket" not in status and "cdpPort" not in status
                    await chat.send_json({"type": "rpc", "id": "status-repeat", "method": "display.status",
                                          "params": {"profileId": profile}})
                    assert (await chat.receive_json())["result"] == status
                    await chat.send_json({"type": "rpc", "id": "observe", "method": "display.observe", "params": {
                        "profileId": profile, "expectedHostId": status["hostId"], "expectedBotId": status["botId"],
                        "generation": state["generation"],
                    }})
                    observed = (await chat.receive_json())["result"]
                    headers = {**auth, "X-Flowly-Profile": profile, "X-Flowly-Host-Id": status["hostId"],
                               "X-Flowly-Bot-Id": status["botId"], "X-Flowly-Screen-Generation": state["generation"],
                               "X-Flowly-Screen-Ticket": observed["ticket"]}

                    async def control(method, epoch):
                        await chat.send_json({"type": "rpc", "id": "control", "method": method, "params": {
                            "profileId": profile, "expectedHostId": status["hostId"], "expectedBotId": status["botId"],
                            "generation": state["generation"], "viewerToken": observed["viewerToken"],
                            "epoch": epoch, "operationId": f"{method}-{epoch}",
                        }})
                        return (await chat.receive_json())["result"]["control"]

                    @asynccontextmanager
                    async def input_stream(epoch):
                        await chat.send_json({"type": "rpc", "id": "resume", "method": "display.observe", "params": {
                            "profileId": profile, "expectedHostId": status["hostId"], "expectedBotId": status["botId"],
                            "generation": state["generation"], "viewerToken": observed["viewerToken"],
                        }})
                        resumed = (await chat.receive_json())["result"]
                        assert resumed["viewerId"] == observed["viewerId"]
                        assert resumed["inputProtocol"] == "epoch-u64-v1"
                        framed_headers = {**headers, "X-Flowly-Screen-Ticket": resumed["ticket"],
                                          "X-Flowly-Screen-Input": "epoch-u64-v1"}
                        async with client.ws_connect(server.make_url(STREAM_PATH), headers=framed_headers) as ws:
                            stream = RfbSocket(ws, epoch=epoch)
                            await stream.handshake()
                            await stream.send(struct.pack("!BBHi", 2, 0, 1, 0))
                            yield stream

                    async def frame_barrier(stream):
                        await stream.send(struct.pack("!BBHHHH", 3, 0, 200, 300, 8, 8))
                        update = await stream.read(4)
                        assert update[0] == 0
                        for _ in range(int.from_bytes(update[2:4], "big")):
                            _, _, width, height, encoding = struct.unpack("!HHHHi", await stream.read(12))
                            assert encoding == 0
                            await stream.read(width * height * 4)

                    def key(character):
                        return b"".join(struct.pack("!BBHI", 4, down, 0, ord(character)) for down in (1, 0))
                    async with client.ws_connect(server.make_url(STREAM_PATH), headers=headers) as screen:
                        rfb = RfbSocket(screen)
                        init = await rfb.handshake()
                        assert struct.unpack("!HH", init[:4]) == (1440, 900)
                        await screen.send_bytes(struct.pack("!BBHi", 2, 0, 1, 0))
                        await screen.send_bytes(struct.pack("!BBHHHH", 3, 0, 200, 300, 8, 8))
                        update = await rfb.read(4)
                        assert update[0] == 0
                        rectangles = int.from_bytes(update[2:4], "big")
                        assert rectangles > 0
                        pixels = bytearray()
                        for _ in range(rectangles):
                            _, _, width, height, encoding = struct.unpack("!HHHHi", await rfb.read(12))
                            assert encoding == 0
                            pixels.extend(await rfb.read(width * height * 4))
                        # Real Xvnc pixels, independent of Playwright screenshots.
                        assert pixels and any(pixels)
                        # Two real gateway captures share the supervisor registry.
                        # Takeover must close the other capture before acknowledging.
                        await chat.send_json({"type": "rpc", "id": "other", "method": "display.observe", "params": {
                            "profileId": profile, "expectedHostId": status["hostId"], "expectedBotId": status["botId"],
                            "generation": state["generation"],
                        }})
                        other_view = (await chat.receive_json())["result"]
                        other_headers = {**headers, "X-Flowly-Screen-Ticket": other_view["ticket"]}
                        async with client.ws_connect(server.make_url(STREAM_PATH), headers=other_headers) as other:
                            await RfbSocket(other).handshake()
                            control_params = {
                                "profileId": profile, "expectedHostId": status["hostId"],
                                "expectedBotId": status["botId"], "generation": state["generation"],
                                "viewerToken": observed["viewerToken"],
                                "epoch": runtime.authority.snapshot()["epoch"], "operationId": "takeover",
                            }
                            await chat.send_json({"type": "rpc", "id": "takeover",
                                                  "method": "display.control.acquire", "params": control_params})
                            human = (await chat.receive_json())["result"]["control"]
                            terminal_closed = False
                            try:
                                assert human["control"] == "human"
                                assert (await other.receive(timeout=5)).type in {
                                    aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                }
                                # The holder's original capture remains usable.
                                await screen.send_bytes(struct.pack("!BBHHHH", 3, 0, 200, 300, 8, 8))
                                assert (await rfb.read(4))[0] == 0
                                point = await page.locator("#code").evaluate("""element => {
                                    const box = element.getBoundingClientRect();
                                    return {x: Math.round(screenX + box.x + 10),
                                            y: Math.round(screenY + outerHeight - innerHeight + box.y + 10)};
                                }""")
                                async with input_stream(human["epoch"]) as active:
                                    clicks = b"".join(struct.pack("!BBHH", 5, mask, point["x"], point["y"])
                                                       for mask in (0, 1, 0))
                                    await active.send(clicks)
                                    try:
                                        await page.wait_for_function("document.activeElement.id === 'code'", timeout=5000)
                                    except Exception:
                                        detail = await page.evaluate("({focused:document.hasFocus(), element:document.activeElement.tagName, keys:window.keys})")
                                        raise AssertionError(f"RFB click did not focus input: {point}, {detail}, {runtime.authority.snapshot()}") from None
                                    await active.send(key("h"))
                                    await page.wait_for_function("document.querySelector('#code').value === 'h'", timeout=5000)
                                    await active.send(struct.pack("!BBHI", 4, 1, 0, 0xFFE1))
                                    await page.wait_for_function("window.keys.some(e => e[0] === 'keydown' && e[1] === 'Shift')")
                                    released = await control("display.control.release", human["epoch"])
                                    await page.wait_for_function("window.keys.some(e => e[0] === 'keyup' && e[1] === 'Shift')")
                                    await active.send(key("x"))
                                    await frame_barrier(active)
                                    assert await page.locator("#code").input_value() == "h"
                                    human = await control("display.control.acquire", released["epoch"])
                                    assert human["control"] == "human"
                                    # Still tagged with the previous epoch, even
                                    # though this same viewer owns control again.
                                    await active.send(key("q"))
                                    await frame_barrier(active)
                                    assert await page.locator("#code").input_value() == "h"
                                    async with input_stream(human["epoch"]) as replacement:
                                        await replacement.send(key("z"))
                                        await page.wait_for_function("document.querySelector('#code').value === 'hz'")
                                        await replacement.send(struct.pack("!BBHI", 4, 1, 0, 0xFFE1))
                                        await page.wait_for_function("window.keys.filter(e => e[0] === 'keydown' && e[1] === 'Shift').length === 2")
                                        released = await control("display.viewer.close", human["epoch"])
                                        terminal_closed = True
                                        assert released["control"] == "agent" and not released["inputDrainRequired"]
                                        await page.wait_for_function("window.keys.filter(e => e[0] === 'keyup' && e[1] === 'Shift').length === 2")
                                        assert (await replacement.ws.receive(timeout=5)).type in {
                                            aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                        }
                                        assert await control("display.viewer.close", human["epoch"]) == released
                            finally:
                                if not terminal_closed:
                                    released = await control("display.control.release", human["epoch"])
                                    assert released["control"] == "agent"
                    assert runtime.status()["generation"] == state["generation"]
                    assert is_alive(state["supervisor"])
                    assert await page.locator("body").inner_text() == f"{profile} browser fixture"
                    await chat.send_json({"type": "rpc", "id": "status-after-close", "method": "display.status",
                                          "params": {"profileId": profile}})
                    after_close = (await chat.receive_json())["result"]
                    assert after_close["statusRevision"] > status["statusRevision"]
                    assert after_close["generation"] == status["generation"]
    finally:
        await server.close()
        await asyncio.to_thread(runtime.stop, state["generation"])


@pytest.mark.asyncio
async def test_close_browser_keeps_runtime_and_reopen_reconnects_provider(tmp_path):
    import asyncio
    import time

    from flowly.desktop_runtime.browser_provider import HostBrowserProvider
    from flowly.desktop_runtime.supervisor import application_environment

    async def settled(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.1)
        pytest.fail("browser/runtime transition did not settle")

    with running_screen(tmp_path / "reopen") as screen:
        runtime, generation = screen["runtime"], screen["state"]["generation"]
        provider = HostBrowserProvider(runtime)
        try:
            await provider._connect(generation)
            first = provider._browser
            await first.contexts[0].add_cookies([{
                "url": "http://screen.test", "name": "persistent", "value": "same-profile",
                "expires": time.time() + 3600,
            }])
            cdp = await first.new_browser_cdp_session()
            await cdp.send("Browser.close")
            await settled(lambda: runtime.status().get("cdpPort") is None)
            assert runtime.status()["phase"] == "running"
            assert runtime.status()["generation"] == generation
            with pytest.raises(ScreenError, match="BROWSER_UNAVAILABLE"):
                await provider._connect(generation)
            env = application_environment(runtime.directory, runtime.application_socket_directory)
            env["DISPLAY"] = screen["state"]["display"]
            subprocess.run([str(runtime.application_socket_directory / "launch-browser")],
                           env=env, check=True, timeout=5)
            await settled(lambda: runtime.status().get("cdpPort") is not None)
            await provider._connect(generation)
            assert provider._browser is not first
            cookies = await provider._browser.contexts[0].cookies("http://screen.test")
            assert any(c["name"] == "persistent" and c["value"] == "same-profile" for c in cookies)
            assert runtime.status()["generation"] == generation
        finally:
            await provider.disconnect()


def test_detached_supervisor_holds_environment_activity_until_stop(tmp_path):
    from flowly.desktop_runtime.installation_gate import environment_activity

    with running_screen(tmp_path / "maintenance") as screen:
        # start() has returned, so only the detached supervisor holds the lease.
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with environment_activity(exclusive=True):
                pytest.fail("installation entered while detached desktop was running")
        assert screen["runtime"].status()["phase"] == "running"
    with environment_activity(exclusive=True):
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            screen["runtime"].start()
    assert screen["runtime"].status()["phase"] == "stopped"


def test_crashed_supervisor_is_recovered_by_stop_and_the_screen_starts_again(tmp_path):
    import signal
    import time

    runtime = ScreenRuntime(tmp_path / "crash")
    state = runtime.start()
    os.kill(state["supervisor"]["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 5
    while is_alive(state["supervisor"]) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not is_alive(state["supervisor"])
    assert any(is_alive(child) for child in state["children"]), "applications outlive the crash"
    assert runtime.status()["phase"] == "failed"
    # Without a dedicated account (unprivileged host) the exact recorded,
    # identity-checked children are terminated; the generation is finished.
    runtime.stop(state["generation"])
    assert not any(is_alive(child) for child in state["children"])
    assert runtime.status()["phase"] == "stopped"
    restarted = runtime.start()
    try:
        assert restarted["phase"] == "running" and restarted["generation"] != state["generation"]
    finally:
        runtime.stop(restarted["generation"])


def test_host_reboot_while_human_holds_control_is_recovered_by_explicit_stop(tmp_path):
    import signal
    import time

    from flowly.desktop_runtime.launcher import signal_process

    runtime = ScreenRuntime(tmp_path / "reboot")
    state = runtime.start()
    generation = state["generation"]
    authority = runtime.authority
    action = authority.begin_action("original-run", "before", generation)
    assert authority.end_action(action)
    human = authority.acquire("viewer", generation, authority.snapshot()["epoch"])
    assert human["control"] == "human" and human["runId"] == "original-run"
    # Everything of the screen dies at once, as in a host reboot or OOM kill.
    for identity in [state["supervisor"], *state["children"]]:
        if is_alive(identity):
            signal_process(identity, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while any(is_alive(identity) for identity in [state["supervisor"], *state["children"]]):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # A release needs a running screen and a start needs agent control: without
    # the explicit dead-generation stop the profile would be locked out forever.
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED|HUMAN_HAS_CONTROL"):
        runtime.start()
    runtime.stop(generation)
    assert authority.snapshot()["control"] == "agent" and authority.snapshot()["runId"] is None
    restarted = runtime.start()
    try:
        assert restarted["phase"] == "running" and restarted["generation"] != generation
    finally:
        runtime.stop(restarted["generation"])


def test_runtime_restored_from_another_host_never_signals_reused_pids_and_recovers(tmp_path):
    import signal
    import time

    from flowly.desktop_runtime.launcher import process_identity, signal_process, write_state

    directory = tmp_path / "restored"
    runtime = ScreenRuntime(directory)
    state = runtime.start()
    generation = state["generation"]
    runtime.authority.acquire("viewer", generation, runtime.authority.snapshot()["epoch"])
    for identity in [state["supervisor"], *state["children"]]:
        if is_alive(identity):
            signal_process(identity, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while any(is_alive(identity) for identity in [state["supervisor"], *state["children"]]):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # A backup taken while the screen ran, restored on another host: its PIDs
    # now name unrelated live processes there, under another kernel boot.
    strangers = [subprocess.Popen(["sleep", "60"]) for _ in range(1 + len(state["children"]))]
    try:
        foreign = [dict(process_identity(p.pid), boot="00000000-0000-4000-8000-000000000000") for p in strangers]
        write_state(directory / "runtime.json", {**state, "supervisor": foreign[0], "children": foreign[1:]})
        restored = ScreenRuntime(directory)
        with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED|HUMAN_HAS_CONTROL"):
            restored.start()
        restored.stop(generation)
        assert all(p.poll() is None for p in strangers), "a restored manifest never signals this host's processes"
        assert restored.authority.snapshot()["control"] == "agent"
        again = restored.start()
        try:
            assert again["phase"] == "running" and again["generation"] != generation
        finally:
            restored.stop(again["generation"])
    finally:
        for p in strangers:
            p.kill()
            p.wait()


def test_screen_yields_to_the_agent_under_memory_pressure_and_bounds_its_cache(tmp_path):
    runtime = ScreenRuntime(tmp_path / "bounded")
    state = runtime.start()
    try:
        for identity in [state["supervisor"], *state["children"]]:
            assert Path(f"/proc/{identity['pid']}/oom_score_adj").read_text().strip() == "500"
        assert Path("/proc/self/oom_score_adj").read_text().strip() == "0"
        profile = f"--user-data-dir={tmp_path / 'bounded' / 'browser'}".encode()
        browsers = []
        for entry in Path("/proc").iterdir():
            try:
                # Chromium rewrites its argv into one space-joined string.
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            if b"/chromium" in command and profile in command and b"--type=" not in command:
                browsers.append(command)
        assert browsers, "the browser process"
        assert all(b"--disk-cache-size=268435456" in command for command in browsers)
    finally:
        runtime.stop(state["generation"])


def test_idle_stop_of_a_real_screen_records_why_and_starts_again(tmp_path):
    runtime = ScreenRuntime(tmp_path / "idle")
    state = runtime.start()
    observed = runtime.authority.snapshot()["revision"]
    human = runtime.authority.acquire("viewer", state["generation"], runtime.authority.snapshot()["epoch"])
    runtime.authority.release("viewer", state["generation"], human["epoch"])
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        runtime.stop(state["generation"], idle_revision=observed)
    assert runtime.status()["phase"] == "running"
    runtime.stop(state["generation"], idle_revision=runtime.authority.snapshot()["revision"])
    stopped = runtime.status()
    assert (stopped["phase"], stopped["error"]) == ("stopped", "IDLE_STOPPED")
    assert not any(is_alive(identity) for identity in [state["supervisor"], *state["children"]])
    again = runtime.start()
    try:
        assert again["phase"] == "running" and again["error"] is None
    finally:
        runtime.stop(again["generation"])


# Keysym sequences exactly as Desktop's noVNC 1.7 viewer sends them (pinned by
# Desktop's screen-keyboard test). Core's gate strips QEMU extended keys, so the
# server always receives keysyms, never the client's physical scancodes.
SHIFT_L, MODE_SWITCH, LEVEL3 = 0xFFE1, 0xFF7E, 0xFE03
KEYBOARD_CASES = {
    "turkish-q-letters": ([(k, d) for k in (0x1BA, 0x2BB, 0x2B9, 0x69, 0xE7, 0xF6, 0xFC) for d in (1, 0)]
                          + [(SHIFT_L, 1)] + [(k, d) for k in (0x1AA, 0x2AB, 0x49, 0x2A9, 0xC7, 0xD6, 0xDC)
                                              for d in (1, 0)] + [(SHIFT_L, 0)], "şğıiçöüŞĞIİÇÖÜ"),
    "mac-option-q-at": ([(MODE_SWITCH, 1), (0x40, 1), (0x40, 0), (MODE_SWITCH, 0)], "@"),
    "mac-option-euro": ([(MODE_SWITCH, 1), (0x20AC, 1), (0x20AC, 0), (MODE_SWITCH, 0)], "€"),
    "mac-option-dead-acute": ([(MODE_SWITCH, 1), (MODE_SWITCH, 0), (0xE9, 1), (0xE9, 0)], "é"),
    "altgr-q-at": ([(LEVEL3, 1), (0x40, 1), (0x40, 0), (LEVEL3, 0)], "@"),
    "altgr-shift-braces": ([(LEVEL3, 1), (0x7B, 1), (0x7B, 0), (0x7D, 1), (0x7D, 0), (LEVEL3, 0)], "{}"),
}


@pytest.mark.parametrize("case", sorted(KEYBOARD_CASES))
def test_viewer_keysyms_type_layout_characters_into_the_real_browser(tmp_path, login_site, case):
    from playwright.sync_api import sync_playwright

    sequence, expected = KEYBOARD_CASES[case]
    with running_screen(tmp_path / "keys") as runtime, sync_playwright() as playwright:
        assert runtime["state"]["keymap"] == "seeded"
        page = playwright.chromium.connect_over_cdp(runtime["endpoint"]).contexts[0].pages[0]
        page.goto(login_site)
        page.evaluate("() => { window.clickedCode = false; document.querySelector('#code')"
                      ".addEventListener('mousedown', () => { window.clickedCode = true }) }")
        with human_viewer(runtime["socket"]) as (connection, gate):
            point = page.locator("#code").evaluate("""element => {
                const box = element.getBoundingClientRect();
                return {x: Math.round(screenX + box.x + 10),
                        y: Math.round(screenY + outerHeight - innerHeight + box.y + 10)};
            }""")
            for buttons in (0, 1, 0):
                connection.sendall(gate.feed(struct.pack("!BBHH", 5, buttons, point["x"], point["y"]), lambda: True))
            page.wait_for_function("document.hasFocus() && window.clickedCode")
            for keysym, down in sequence:
                connection.sendall(gate.feed(struct.pack("!BBHI", 4, down, 0, keysym), lambda: True))
            # A trailing marker proves every earlier key was processed.
            for down in (1, 0):
                connection.sendall(gate.feed(struct.pack("!BBHI", 4, down, 0, ord("#")), lambda: True))
            page.wait_for_function("document.querySelector('#code').value.endsWith('#')", timeout=10_000)
            assert page.locator("#code").input_value() == expected + "#"


def test_input_service_maps_other_unicode_before_typing_it(tmp_path, login_site):
    """Outside the seeded keymap Xvnc would add a keysym while it is typed and
    Chromium would read that key with its previous keymap (the character was
    lost or became the next new one). The supervisor's input path maps it first."""
    from playwright.sync_api import sync_playwright

    from flowly.desktop_runtime.input_service import InputServiceClient
    from flowly.desktop_runtime.tickets import StreamScope, StreamTickets
    from flowly.desktop_runtime.viewers import ViewerCredentials

    text = "ə✓ə漢字テスト©ąĄ😀"
    with running_screen(tmp_path / "unicode") as runtime, sync_playwright() as playwright:
        page = playwright.chromium.connect_over_cdp(runtime["endpoint"]).contexts[0].pages[0]
        page.goto(login_site)
        page.evaluate("() => { window.clickedCode = false; document.querySelector('#code')"
                      ".addEventListener('mousedown', () => { window.clickedCode = true }) }")
        point = page.locator("#code").evaluate("""element => {
            const box = element.getBoundingClientRect();
            return {x: Math.round(screenX + box.x + 10),
                    y: Math.round(screenY + outerHeight - innerHeight + box.y + 10)};
        }""")
        authority = runtime["runtime"].authority
        generation = runtime["state"]["generation"]
        viewers = ViewerCredentials(StreamTickets(runtime["profile"].parent / "admission"))
        viewer = viewers.create(StreamScope("owner", "host", "default", generation), "bot")["viewerId"]
        human = authority.acquire(viewer, generation, authority.snapshot()["epoch"])
        identity = dict(generation=generation, viewer=viewer, epoch=human["epoch"])
        client = InputServiceClient(runtime["runtime"].socket_directory / "input.sock")
        handle = client.request("open", **identity)["handle"]
        sequence = 0

        def send(data):
            nonlocal sequence
            client.request("send", **identity, handle=handle, sequence=sequence, data=base64.b64encode(data).decode())
            sequence += 1

        send(b"".join(struct.pack("!BBHH", 5, mask, point["x"], point["y"]) for mask in (0, 1, 0)))
        page.wait_for_function("document.hasFocus() && window.clickedCode")
        # One burst, as Desktop's paste sends it: noVNC's legacy keysym where one
        # exists (ą 0x1b1, Ą 0x1a1), otherwise 0x01000000 | code point.
        legacy = {"ą": 0x1B1, "Ą": 0x1A1}
        keysyms = [legacy.get(c, c.isascii() and ord(c) or (ord(c) if ord(c) <= 0xFF else 0x01000000 | ord(c)))
                   for c in text + "#"]
        send(b"".join(struct.pack("!BBHI", 4, down, 0, keysym) for keysym in keysyms for down in (1, 0)))
        page.wait_for_function("document.querySelector('#code').value.endsWith('#')", timeout=15_000)
        assert page.locator("#code").input_value() == text + "#"
        client.request("viewer.close", **identity, operationId="close")


def test_screen_clipboard_never_reaches_viewers(tmp_path, login_site):
    """What the person in control (or the agent) copies on the screen may be a
    password; Xvnc must not push it to any viewer as ServerCutText."""
    from playwright.sync_api import sync_playwright

    with running_screen(tmp_path / "clipboard") as runtime, sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(runtime["endpoint"])
        context = browser.contexts[0]
        page = context.pages[0]
        page.goto(login_site)
        with human_viewer(runtime["socket"]) as (connection, gate):
            # Plain RFB SetEncodings (raw only): a legacy client gets ServerCutText on every change.
            connection.sendall(gate.feed(struct.pack("!BBHi", 2, 0, 1, 0), lambda: False))
            point = page.locator("#code").evaluate("""element => {
                const box = element.getBoundingClientRect();
                return {x: Math.round(screenX + box.x + 10),
                        y: Math.round(screenY + outerHeight - innerHeight + box.y + 10)};
            }""")
            for buttons in (0, 1, 0):
                connection.sendall(gate.feed(struct.pack("!BBHH", 5, buttons, point["x"], point["y"]), lambda: True))
            page.wait_for_function("document.hasFocus()")
            page.evaluate("""() => { const input = document.querySelector('#code');
                input.value = 'copied-secret-4711'; input.focus(); input.select(); document.execCommand('copy'); }""")
            connection.settimeout(3)
            received = bytearray()
            try:
                while len(received) < 1 << 20:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    received.extend(chunk)
            except socket.timeout:
                pass
            assert b"copied-secret-4711" not in received
