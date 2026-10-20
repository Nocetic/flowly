"""Actual noVNC decoding/input through the authenticated product gateway.

The disposable runner mounts Desktop's pinned noVNC package at /novnc. The
test-only loopback bridge adds gateway headers and the production epoch prefix;
it does not bypass the gateway, supervisor input broker or capture service.
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flowly.desktop_runtime.launcher import ScreenRuntime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("FLOWLY_SCREEN_GATEWAY_TEST") != "1"
        or not Path("/novnc/core/rfb.js").is_file(),
        reason="requires isolated Linux gateway runner and pinned noVNC mount",
    ),
]


async def test_novnc_tight_cursor_input_and_reconnect(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright

    import flowly.profile as profiles
    from flowly.gateway.display import STREAM_PATH
    from flowly.gateway.server import GatewayServer

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    runtime = ScreenRuntime(home / "desktop")
    state = await asyncio.to_thread(runtime.start)
    assert state["captureServiceVersion"] == 1
    gateway = GatewayServer(
        host="127.0.0.1", port=0, auth_token="novnc-test", require_loopback_auth=True
    )
    gateway.on_chat_message = AsyncMock()
    server = TestServer(gateway._create_app())
    await server.start_server()
    bridge_server = None
    try:
        async with aiohttp.ClientSession() as client, async_playwright() as playwright:
            auth = {"Authorization": "Bearer novnc-test"}
            async with client.post(
                server.make_url("/api/auth/ws-ticket"), headers=auth
            ) as response:
                ticket = (await response.json())["ticket"]
            async with client.ws_connect(server.make_url("/ws?ticket=" + ticket)) as chat:

                async def rpc(method, params):
                    await chat.send_json(
                        {"type": "rpc", "id": method, "method": method, "params": params}
                    )
                    reply = await chat.receive_json()
                    assert "error" not in reply, reply
                    return reply["result"]

                status = await rpc("display.status", {"profileId": "default"})
                identity = dict(
                    profileId="default",
                    expectedHostId=status["hostId"],
                    expectedBotId=status["botId"],
                    generation=state["generation"],
                )
                observed = await rpc("display.observe", identity)
                control_params = {**identity, "viewerToken": observed["viewerToken"]}
                human = (
                    await rpc(
                        "display.control.acquire",
                        {
                            **control_params,
                            "epoch": status["control"]["epoch"],
                            "operationId": "acquire",
                        },
                    )
                )["control"]
                assert human["control"] == "human"
                prefix = human["epoch"].to_bytes(8, "big")

                async def bridge(request):
                    resumed = await rpc("display.observe", control_params)
                    headers = {
                        **auth,
                        "X-Flowly-Profile": "default",
                        "X-Flowly-Host-Id": status["hostId"],
                        "X-Flowly-Bot-Id": status["botId"],
                        "X-Flowly-Screen-Generation": state["generation"],
                        "X-Flowly-Screen-Ticket": resumed["ticket"],
                        "X-Flowly-Screen-Input": "epoch-u64-v1",
                    }
                    async with client.ws_connect(
                        server.make_url(STREAM_PATH), headers=headers
                    ) as upstream:
                        ws = web.WebSocketResponse(compress=False)
                        await ws.prepare(request)

                        async def forward(source, destination, envelope):
                            async for message in source:
                                if message.type != aiohttp.WSMsgType.BINARY:
                                    break
                                await destination.send_bytes(envelope + message.data)

                        tasks = [
                            asyncio.create_task(forward(ws, upstream, prefix)),
                            asyncio.create_task(forward(upstream, ws, b"")),
                        ]
                        try:
                            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            await ws.close()
                        return ws

                async def viewer(_):
                    return web.Response(
                        content_type="text/html",
                        text="""<!doctype html>
                    <body style="margin:0"><div id="screen" style="width:1440px;height:900px"></div>
                    <script type="module">
                    import RFB from '/novnc/core/rfb.js';
                    window.rfb = new RFB(document.querySelector('#screen'), `ws://${location.host}/rfb`);
                    rfb.qualityLevel=6; rfb.compressionLevel=2; rfb.showDotCursor=true;
                    rfb.addEventListener('connect',()=>window.connected=true);
                    rfb.addEventListener('disconnect',()=>window.connected=false);
                    </script>""",
                    )

                app = web.Application()
                app.router.add_get("/", viewer)
                app.router.add_get("/rfb", bridge)
                app.router.add_static("/novnc/", "/novnc")
                bridge_server = TestServer(app)
                await bridge_server.start_server()
                desktop = await playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{state['cdpPort']}"
                )
                page = desktop.contexts[0].pages[0]
                await page.set_content(
                    "<body style='background:#237bb4;margin:0;height:100vh'><input id='code'></body>"
                )
                await page.evaluate("""() => {
                    addEventListener('mousemove', e=>window.lastPointer=[e.screenX,e.screenY]);
                    addEventListener('keydown', e=>window.shift=e.shiftKey);
                    addEventListener('keyup', e=>window.shift=e.shiftKey);
                }""")
                await page.bring_to_front()
                browser = await playwright.chromium.launch(
                    executable_path="/usr/bin/chromium", headless=True, chromium_sandbox=True
                )
                errors = []
                try:
                    view = await browser.new_page(viewport={"width": 1440, "height": 900})
                    view.on("pageerror", lambda error: errors.append(str(error)))
                    for attempt in range(2):
                        await view.goto(str(bridge_server.make_url("/")))
                        await view.wait_for_function("window.connected===true")
                        await view.mouse.move(600, 500)
                        await page.wait_for_function("window.lastPointer?.[0]===600")
                        await view.wait_for_function("rfb._cursorImage.w>0 && rfb._cursorImage.h>0")
                        # The decoded Tight canvas stays uniformly blue under
                        # the local cursor; there is no delayed baked cursor.
                        await view.wait_for_function("""() => {
                            const p=document.querySelector('canvas').getContext('2d').getImageData(600,500,32,32).data;
                            for(let i=0;i<p.length;i+=4) if(Math.abs(p[i]-35)>5 || Math.abs(p[i+1]-123)>5 || Math.abs(p[i+2]-180)>5) return false;
                            return true;
                        }""")
                        for x in range(610, 710, 5):
                            await view.mouse.move(x, 500)
                        await page.wait_for_function("window.lastPointer?.[0]===705")
                        # Force renegotiation while input and continuous updates
                        # are live; the framer must keep exact message boundaries.
                        await view.evaluate("rfb.qualityLevel=9; rfb.compressionLevel=0")
                        await view.mouse.click(705, 500)
                        await view.keyboard.down("Shift")
                        await page.wait_for_function("window.shift===true")
                        if attempt == 0:
                            # A new capture drains the disconnected input side.
                            await view.goto("about:blank")
                            await view.keyboard.up("Shift")
                        else:
                            result = await rpc(
                                "display.control.release",
                                {
                                    **control_params,
                                    "epoch": human["epoch"],
                                    "operationId": "release",
                                },
                            )
                            assert not result["control"]["inputDrainRequired"]
                            await page.wait_for_function("window.shift===false")
                            assert await view.evaluate("window.connected")
                    assert not errors, errors
                finally:
                    await browser.close()
    finally:
        if bridge_server:
            await bridge_server.close()
        await server.close()
        # A failed assertion must still dispose of this test-only runtime.
        control = runtime.authority.snapshot()
        if control["control"] == "human":
            from flowly.desktop_runtime.input_service import InputServiceClient

            await asyncio.to_thread(
                InputServiceClient(runtime.socket_directory / "input.sock").request,
                "control.release",
                viewer=observed["viewerId"],
                generation=state["generation"],
                epoch=control["epoch"],
                operationId="test-cleanup",
            )
        await asyncio.to_thread(runtime.stop, state["generation"])
