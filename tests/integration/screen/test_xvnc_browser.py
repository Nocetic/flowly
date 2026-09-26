"""Opt-in real Linux browser/RFB spike; uses only generated test credentials.

Run in the disposable screen Docker image with FLOWLY_SCREEN_LINUX_TEST=1.
Chromium runs with its sandbox enabled. The disposable Docker runner needs
permission to create user namespaces (see README.md).
"""

import json
import os
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

    from flowly.agent.tools.browser_tab import BrowserTabTool
    from flowly.desktop_runtime.browser_provider import HostBrowserProvider
    from flowly.gateway.server import GatewayServer

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    runtime = ScreenRuntime(tmp_path / "gateway-screen")
    state = await asyncio.to_thread(runtime.start)
    provider = HostBrowserProvider(runtime)
    generation = state["generation"]
    assert (await provider.request("setup", "navigate", generation, "navigate", {"url": login_site}))["success"]
    await provider.execution.finish_run("setup", generation)
    reached_handoff, continue_test_run, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observations = []
    gateway = GatewayServer(host="127.0.0.1", port=0, auth_token="test-gateway-token", require_loopback_auth=True)
    gateway._host_screen_browser = provider
    gateway._host_screen_identity = {"hostId": "test-host", "profileId": "default", "screenId": "browser"}
    access = {**gateway._host_screen_identity, "providerId": "host-desktop-browser", "generation": generation}

    async def on_chat(*_):
        tool = BrowserTabTool(gateway)
        observations.append(json.loads(await tool.execute("read_page")))
        reached_handoff.set()
        # This event is the test's orchestration point, not a production
        # resume implementation. It keeps the same authenticated run alive.
        await continue_test_run.wait()
        observations.append(json.loads(await tool.execute("read_page")))
        result = json.loads(await tool.execute("get_page_text"))
        finished.set()
        return result["text"], {}

    gateway.on_chat_message = on_chat
    server = TestServer(gateway._create_app())
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            url = str(server.make_url("/ws?clientId=screen-desktop")).replace("http://", "ws://", 1)
            with pytest.raises(aiohttp.WSServerHandshakeError) as unauthenticated:
                await session.ws_connect(url)
            assert unauthenticated.value.status == 401
            async with session.post(server.make_url("/api/auth/ws-ticket"),
                                    headers={"Authorization": "Bearer test-gateway-token"}) as response:
                assert response.status == 200
                ticket = (await response.json())["ticket"]
            async with session.ws_connect(url + "&ticket=" + ticket) as desktop:
                await desktop.send_json({"type": "rpc", "id": "wrong-target", "method": "chat.send", "params": {
                    "sessionKey": "screen:test", "message": "work", "idempotencyKey": "wrong-run",
                    "browserAccess": {**access, "profileId": "other"},
                }})
                assert (await desktop.receive_json())["error"]["code"] == "BROWSER_PROVIDER_NOT_OWNED"
                await desktop.send_json({"type": "rpc", "id": "run", "method": "chat.send", "params": {
                    "sessionKey": "screen:test", "message": "work", "idempotencyKey": "origin-run",
                    "browserAccess": access,
                }})
                assert (await desktop.receive_json())["result"]["runId"] == "origin-run"
                await asyncio.wait_for(reached_handoff.wait(), 15)
                human = await provider.execution.acquire("test-human", generation, 1)
                assert human["control"] == "human"
                assert human["runId"] == "origin-run"
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['cdpPort']}")
                    page = browser.contexts[0].pages[0]
                    await page.locator("#code").fill("human-completed-login")
                    await page.locator("#login").click()
                runtime.authority.release("test-human", generation, human["epoch"])
                continue_test_run.set()
                final = await desktop.receive_json(timeout=15)
                assert final["data"]["state"] == "final"
                assert final["data"]["runId"] == "origin-run"
                assert "Signed in" in final["data"]["message"]["content"][0]["text"]
                assert all("_screenObservation" not in result for result in observations)
                assert observations[0]["elements"] != observations[1]["elements"]
                assert finished.is_set()
    finally:
        continue_test_run.set()
        await server.close()
        control = runtime.authority.snapshot()
        if control["control"] == "human":
            runtime.authority.release("test-human", generation, control["epoch"])
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
