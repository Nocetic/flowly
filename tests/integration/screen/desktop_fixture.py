"""Disposable Linux host for the real Desktop screen integration smoke.

Run only inside the isolated screen test container. The test model never calls
an external service. Human login must arrive through the public RFB input path;
CDP is used only to navigate during setup and to inspect the resulting page.
"""

from __future__ import annotations

import asyncio
import os
import signal
import ssl
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.browser_tab import BrowserTabTool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.gateway.server import GatewayServer
from flowly.profile import get_or_create_profile_host_id
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

TOKEN = "disposable-screen-desktop-test"
HTML = """<!doctype html><html><head><title>Screen login fixture</title></head>
<body style="margin:60px;background:#fafafa;color:#123;font:24px sans-serif">
<h1>Flowly screen integration</h1>
<label for="code">Test code</label><input id="code" style="font:24px sans-serif" autocomplete="off">
<button id="login" style="font:24px sans-serif" onclick="
if(document.querySelector('#code').value==='flowly-screen-fixture') {
document.cookie='session=desktop-login; path=/';
document.querySelector('#result').textContent='Signed in';
document.querySelector('#result').style.backgroundColor='#16803c';
}">Sign in</button><p id="result">Waiting for human login</p></body></html>"""


async def main():
    if os.environ.get("FLOWLY_SCREEN_LINUX_TEST") != "1" or os.getuid() == 0:
        raise RuntimeError("Requires the isolated unprivileged Linux screen runner")
    home = Path(os.environ["FLOWLY_HOME"])
    if home != Path.home() / ".flowly":
        raise RuntimeError("Use the disposable container user's default profile home")
    home.mkdir(parents=True, exist_ok=True)
    runtime = ScreenRuntime(home / "desktop")
    provider = HostBrowserProvider(runtime)
    gateway = GatewayServer(host="0.0.0.0", port=8787, auth_token=TOKEN, require_loopback_auth=True)
    gateway._host_screen_browser = provider
    gateway._host_screen_identity = {"hostId": get_or_create_profile_host_id(), "profileId": "default", "screenId": "browser"}
    actions = []
    dispatch = provider._dispatch

    async def track_dispatch(action, params):
        actions.append(action)
        return await dispatch(action, params)

    class LocalModel(LLMProvider):
        calls = 0

        def get_default_model(self):
            return "test/local-screen-model"

        async def chat(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                # Hold only the deterministic model response. The production
                # checkpoint must handle the subsequent wait and continuation.
                async with asyncio.timeout(90):
                    while (await asyncio.to_thread(runtime.authority.snapshot))["control"] != "human":
                        await asyncio.sleep(0.02)
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="stale-click", name="browser_tab",
                    arguments={"action": "click", "selector": "#login"})])
            if self.calls == 2:
                assert "fresh browser observation" in str(kwargs["messages"])
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="observe-login", name="browser_tab",
                    arguments={"action": "get_page_text"})])
            assert "Signed in" in str(kwargs["messages"])
            return LLMResponse(content="Signed in in the original Desktop task")

    model = LocalModel(api_key="test")
    loop = AgentLoop(bus=MessageBus(), provider=model, workspace=home / "workspace", main_config=Config(),
                     max_iterations=4, soft_warn_at_iteration=0)
    loop.tools.register(BrowserTabTool(gateway))

    async def on_chat(*args):
        result = await loop._run_llm_tool_loop(
            [{"role": "system", "content": "Complete the browser task."}, {"role": "user", "content": "Read the login result."}],
            action_turn=False, session_key=args[0], outbound_run_id=args[2])
        return result[0], {"aborted": loop.is_run_aborted(args[2])}

    gateway.on_chat_message = on_chat
    app = gateway._create_app()
    page = None

    async def inspection(request):
        # Test-only inspection never injects mouse/keyboard input or mutates the
        # browser. Keep values/cookies out of the response and test logs.
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()
        return web.json_response({"calls": model.calls, "actions": list(actions),
            "page": await page.evaluate("""() => {
                const point = selector => { const r=document.querySelector(selector).getBoundingClientRect();
                  return {x:screenX+(outerWidth-innerWidth)/2+r.x+r.width/2,
                    y:screenY+outerHeight-innerHeight+r.y+r.height/2}; };
                return {code:point('#code'), login:point('#login'), result:point('#result'),
                  codeMatches:document.querySelector('#code').value==='flowly-screen-fixture',
                  signedIn:document.querySelector('#result').textContent==='Signed in',
                  cookieMatches:document.cookie==='session=desktop-login'};
            }""") if page is not None and not page.is_closed() else None})

    app.router.add_get("/fixture/inspection", inspection)
    # The login page is deliberately separate from the authenticated gateway.
    site_app = web.Application()
    site_app.router.add_get("/", lambda request: web.Response(text=HTML, content_type="text/html"))
    site_runner = web.AppRunner(site_app, access_log=None)
    await site_runner.setup()
    site = web.TCPSite(site_runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    runner = web.AppRunner(app, access_log=None)
    stopped = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        async with async_playwright() as playwright:
            async def prepare_page():
                nonlocal page
                # Desktop must explicitly start the runtime. Test setup then
                # navigates once; inspection itself remains strictly read-only.
                async with asyncio.timeout(120):
                    while True:
                        state = await asyncio.to_thread(runtime.status)
                        if state and state["phase"] == "running":
                            break
                        await asyncio.sleep(0.05)
                generation = state["generation"]
                assert (await provider.request("setup", "navigate", generation, "navigate", {"url": f"http://127.0.0.1:{port}/"}))["success"]
                await provider.execution.finish_run("setup", generation)
                provider._dispatch = track_dispatch
                browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['cdpPort']}")
                page = browser.contexts[0].pages[0]
            setup_task = asyncio.create_task(prepare_page())
            await runner.setup()
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain("/fixture-tls/cert.pem", "/fixture-tls/key.pem")
            await web.TCPSite(runner, "0.0.0.0", 8787, ssl_context=tls).start()
            print("READY desktop screen fixture", flush=True)
            try:
                await stopped.wait()
            finally:
                setup_task.cancel()
                await asyncio.gather(setup_task, return_exceptions=True)
    finally:
        await runner.cleanup()
        await site_runner.cleanup()
        loop.stop()
        await provider.disconnect()
        # The disposable container is removed by the runner even if an
        # interrupted test deliberately leaves human ownership fenced.
        control = runtime.authority.snapshot()
        state = runtime.status()
        if state and state["phase"] == "running" and control["control"] == "agent" and control["runId"] is None:
            await asyncio.to_thread(runtime.stop, state["generation"])


if __name__ == "__main__":
    asyncio.run(main())
