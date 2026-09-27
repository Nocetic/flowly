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
import subprocess
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.browser_tab import BrowserTabTool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.supervisor import application_environment
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
    if os.environ.get("FLOWLY_SCREEN_LINUX_TEST") != "1":
        raise RuntimeError("Requires the isolated Linux screen runner")
    if os.getuid() == 0:
        if os.environ.get("FLOWLY_SCREEN_ROOT_TEST") != "1" or not Path("/.dockerenv").is_file():
            raise RuntimeError("Root fixture requires explicit disposable-container opt-in")
        from flowly.desktop_runtime.runtime_account_payload import PROVISION_SOURCE

        subprocess.run(["/usr/bin/python3", "-I", "-c", PROVISION_SOURCE + "\nprovision_runtime_account()\n"],
                       check=True, timeout=35, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    home = Path(os.environ["FLOWLY_HOME"])
    if home != Path.home() / ".flowly":
        raise RuntimeError("Use the disposable container user's default profile home")
    home.mkdir(parents=True, exist_ok=True)
    runtime = ScreenRuntime(home / "desktop")
    provider = HostBrowserProvider(runtime)
    relay_url = os.environ.get("FLOWLY_SCREEN_FIXTURE_RELAY_URL")
    gateway = GatewayServer(host="0.0.0.0", port=8788 if relay_url else 8787, auth_token=TOKEN, require_loopback_auth=True)
    gateway._host_screen_browser = provider
    gateway._host_screen_identity = {"hostId": get_or_create_profile_host_id(), "profileId": "default", "screenId": "browser"}
    actions = []
    dispatch = provider._dispatch

    async def track_dispatch(action, params):
        actions.append(action)
        return await dispatch(action, params)

    class LocalModel(LLMProvider):
        calls = 0
        auxiliary: list[str] = []

        def get_default_model(self):
            return "test/local-screen-model"

        async def chat(self, *args, **kwargs):
            if not kwargs.get("tools"):
                # Tool-less product side calls (e.g. the session auto-title
                # after a real bus turn) are not steps of the screen task.
                self.auxiliary.append(str(kwargs.get("purpose") or "unknown"))
                return LLMResponse(content="Screen fixture")
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
    # One bus: in relay mode the original task arrives as a real relay
    # chat.send through WebChannel ingress, admission and the agent loop.
    bus = MessageBus()
    loop = AgentLoop(bus=bus, provider=model, workspace=home / "workspace", main_config=Config(),
                     max_iterations=4, soft_warn_at_iteration=0)
    loop.tools.register(BrowserTabTool(gateway))
    gateway._maintenance_backlog = lambda: bus.inbound_size

    async def on_chat(*args):
        result = await loop._run_llm_tool_loop(
            [{"role": "system", "content": "Complete the browser task."}, {"role": "user", "content": "Read the login result."}],
            action_turn=False, session_key=args[0], outbound_run_id=args[2])
        return result[0], {"aborted": loop.is_run_aborted(args[2])}

    gateway.on_chat_message = on_chat
    app = gateway._create_app()
    page = None

    def desktop_inspection():
        state = runtime.status()
        if not state or state["phase"] != "running":
            return None
        privileges = {}
        if os.geteuid() == 0:
            from flowly.desktop_runtime.application_layout import root_application_layout

            layout = root_application_layout(runtime.directory)
            environment = layout.environment()
            privileges = {"user": layout.account.uid, "group": layout.account.gid, "extra_groups": []}
        else:
            environment = application_environment(runtime.directory, runtime.rfb_socket_for(state).parent)
        environment["DISPLAY"] = state["display"]

        def query(*args):
            return subprocess.run(["xdotool", *args], env=environment, **privileges,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2)

        panel = query("search", "--onlyvisible", "--class", "^xfce4-panel$")
        if panel.returncode:
            return None
        geometry = query("getwindowgeometry", "--shell", panel.stdout.splitlines()[-1])
        bounds = dict(line.split("=", 1) for line in geometry.stdout.splitlines())
        active = query("getactivewindow").stdout.strip()
        browsers = query("search", "--onlyvisible", "--class", "^chromium$").stdout.splitlines()
        terminals = query("search", "--onlyvisible", "--class", "^xfce4-terminal$").stdout.splitlines()
        focus = query("getwindowfocus").stdout.strip()
        marker = Path(environment["HOME"]) / "desktop-terminal-probe"
        return {"panel": {key.lower(): int(bounds[key]) for key in ("X", "Y", "WIDTH", "HEIGHT")},
                "browserActive": active in browsers,
                "activeWindow": active, "browserWindows": browsers,
                "pointer": query("getmouselocation", "--shell").stdout,
                "focusWindow": focus,
                "terminalWindows": terminals,
                # Keyboard input is only meaningful once the window manager has
                # moved focus to the new Terminal, not merely mapped it.
                "terminalFocused": active in terminals,
                "terminalVisible": bool(terminals),
                "settingsVisible": query("search", "--onlyvisible", "--class", "^xfce4-settings-manager$").returncode == 0,
                "terminalCommandRan": marker.is_file() and marker.read_bytes() == b"desktop-ready\n"}

    async def inspection(request):
        # Test-only inspection never injects mouse/keyboard input or mutates the
        # browser. Keep values/cookies out of the response and test logs.
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()
        return web.json_response({"calls": model.calls, "auxiliaryCalls": list(model.auxiliary), "actions": list(actions),
            "desktop": await asyncio.to_thread(desktop_inspection),
            "page": await page.evaluate("""() => {
                const point = selector => { const r=document.querySelector(selector).getBoundingClientRect();
                  return {x:screenX+(outerWidth-innerWidth)/2+r.x+r.width/2,
                    y:screenY+outerHeight-innerHeight+r.y+r.height/2}; };
                return {code:point('#code'), login:point('#login'), result:point('#result'),
                  codeMatches:document.querySelector('#code').value==='flowly-screen-fixture',
                  codeFocused:document.activeElement===document.querySelector('#code'),
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
    relay_channel = None
    relay_task = None
    agent_tasks = []
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
            if relay_url:
                # The authenticated production bridge talks only to this private
                # loopback listener; public fixture inspection remains TLS.
                await web.TCPSite(runner, "127.0.0.1", 8788).start()
                from flowly.channels.web import WebChannel
                from flowly.config.schema import WebChannelConfig
                relay_channel = WebChannel(WebChannelConfig(enabled=True, relay_url=relay_url,
                    server_id="screen-fixture", auth_token=TOKEN,
                    jwt_secret=os.environ["MOLTBOT_PROXY_JWT_SECRET"]), bus)
                relay_channel.set_screen_gateway(gateway)

                async def deliver_outbound():
                    # The production ChannelManager dispatcher, reduced to the
                    # one adapter this fixture runs.
                    while True:
                        message = await bus.consume_outbound()
                        if message.channel == "web":
                            await relay_channel.send(message)

                agent_tasks += [asyncio.create_task(loop.run()), asyncio.create_task(deliver_outbound())]
                relay_ready = asyncio.Event()
                relay_channel._on_ready = relay_ready.set
                relay_task = asyncio.create_task(relay_channel.start())
                await asyncio.wait_for(relay_ready.wait(), timeout=20)
            print("READY desktop screen fixture", flush=True)
            try:
                await stopped.wait()
            finally:
                setup_task.cancel()
                await asyncio.gather(setup_task, return_exceptions=True)
    finally:
        for task in agent_tasks:
            task.cancel()
        await asyncio.gather(*agent_tasks, return_exceptions=True)
        if relay_channel is not None:
            await relay_channel.stop()
        if relay_task is not None:
            relay_task.cancel()
            await asyncio.gather(relay_task, return_exceptions=True)
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
