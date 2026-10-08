"""Opt-in, disposable Linux capture/encode comparison. Not a production service.

Both viewers use the SAME input WebSocket and acknowledged ScreenInputBroker.
This isolates raw-X11 -> WebRTC vs Xvnc -> noVNC display latency; it does NOT
benchmark the authenticated gateway, Desktop bridge, SSH, relay or WAN.
"""

import asyncio
import json
import os
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import aiohttp
import mss
import numpy as np
from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from playwright.async_api import async_playwright

from flowly.desktop_runtime.input_broker import ScreenInputBroker
from flowly.desktop_runtime.launcher import ScreenRuntime

HERE = Path(__file__).resolve().parent


class DesktopTrack(VideoStreamTrack):
    def __init__(self, display, fps):
        super().__init__()
        self.display = display
        self.fps = fps
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.capture = None
        self.started = None
        self.samples = []

    def grab(self):
        if self.capture is None:
            self.capture = mss.mss(display=self.display, with_cursor=False)
        return np.asarray(self.capture.grab(self.capture.monitors[0]))[:, :, :3].copy()

    async def recv(self):
        now = time.monotonic()
        if self.started is None:
            self.started = now
        await asyncio.sleep(max(0, getattr(self, "next_frame", now) - now))
        start = time.monotonic()
        pixels = await asyncio.get_running_loop().run_in_executor(self.worker, self.grab)
        self.samples.append((time.monotonic() - start) * 1000)
        frame = VideoFrame.from_ndarray(pixels, format="bgr24")
        frame.pts = round((time.monotonic() - self.started) * 90000)
        frame.time_base = Fraction(1, 90000)
        self.next_frame = start + 1 / self.fps
        return frame

    async def close(self):
        self.stop()
        if self.capture:
            await asyncio.get_running_loop().run_in_executor(self.worker, self.capture.close)
        self.worker.shutdown(wait=True)


def cpu_snapshot():
    result = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            result[path.parent.name] = (
                int(fields[19]),
                (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
            )
        except (OSError, ValueError, IndexError):
            continue
    return result


async def main():
    if os.environ.get("FLOWLY_SCREEN_LATENCY_TEST") != "1" or os.name != "posix":
        raise SystemExit("Run only in the disposable Linux latency runner.")
    fps = int(os.environ.get("FLOWLY_SCREEN_BENCH_FPS", "30"))
    if fps not in (30, 60):
        raise SystemExit("Benchmark FPS must be 30 or 60.")
    Path("/tmp/flowly-benchmark-fps").write_text(str(fps))
    results, peers, tracks, tasks = [], [], [], set()
    with tempfile.TemporaryDirectory(prefix="latency-", dir="/tmp") as directory:
        runtime = ScreenRuntime(Path(directory) / "desktop")
        state = await asyncio.to_thread(runtime.start)
        authority = runtime.authority
        human = authority.acquire(
            "latency-fixture", state["generation"], authority.snapshot()["epoch"]
        )
        broker = ScreenInputBroker(
            authority,
            "latency-fixture",
            state["generation"],
            human["epoch"],
            runtime.rfb_socket_for(state),
        )
        input_times = []
        # An isolated runtime's X cookie; never copy the user's agent environment.
        os.environ["XAUTHORITY"] = str(runtime.rfb_socket_for(state).parent / "Xauthority")
        # Fail closed if the test image did not actually apply the override.
        xvnc_commands = [
            Path(f"/proc/{child['pid']}/cmdline").read_bytes().split(b"\0")
            for child in state["children"]
        ]
        xvnc_commands = [args for args in xvnc_commands if b"-FrameRate" in args]
        assert len(xvnc_commands) == 1
        assert xvnc_commands[0][xvnc_commands[0].index(b"-FrameRate") + 1] == str(fps).encode()

        async def viewer(request):
            return web.FileResponse(HERE / "viewer.html")

        async def input_socket(request):
            ws = web.WebSocketResponse(max_msg_size=256)
            await ws.prepare(request)
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                value = json.loads(message.data)
                x, y = value["x"], value["y"]
                if not (type(x) is int and type(y) is int and 0 <= x < 1440 and 0 <= y < 900):
                    break
                start = time.monotonic()
                await asyncio.to_thread(broker.send, struct.pack("!BBHH", 5, 0, x, y))
                input_times.append((time.monotonic() - start) * 1000)
            return ws

        async def rfb_socket(request):
            ws = web.WebSocketResponse(compress=False, max_msg_size=256 * 1024)
            await ws.prepare(request)
            reader, writer = await asyncio.open_unix_connection(runtime.rfb_socket_for(state))

            async def outgoing():
                while data := await reader.read(64 * 1024):
                    await ws.send_bytes(data)

            async def incoming():
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.BINARY:
                        break
                    writer.write(message.data)
                    await writer.drain()

            pair = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
            tasks.update(pair)
            try:
                await asyncio.wait(pair, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in pair:
                    task.cancel()
                await asyncio.gather(*pair, return_exceptions=True)
                tasks.difference_update(pair)
                writer.close()
                await writer.wait_closed()
            return ws

        async def offer(request):
            data = await request.json()
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            peers.append(pc)
            track = DesktopTrack(state["display"], fps)
            tracks.append(track)
            await pc.setRemoteDescription(RTCSessionDescription(**data))
            pc.addTrack(track)
            await pc.setLocalDescription(await pc.createAnswer())
            return web.json_response(
                {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
            )

        app = web.Application()
        app.router.add_get("/", viewer)
        app.router.add_get("/input", input_socket)
        app.router.add_get("/rfb", rfb_socket)
        app.router.add_post("/offer", offer)
        app.router.add_static("/novnc/", Path("/novnc"), follow_symlinks=False)
        runner = web.AppRunner(app, shutdown_timeout=2)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with async_playwright() as playwright:
                desktop = await playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{state['cdpPort']}"
                )
                page = desktop.contexts[0].pages[0]
                await page.set_content("""<body style='background:#237bb4;margin:0'><canvas id='marker' width='256' height='64' style='position:absolute;left:16px;top:16px'></canvas><script>
                const c=document.querySelector('canvas').getContext('2d');
                addEventListener('mousemove',e=>{for(let i=0;i<16;i++){c.fillStyle=(e.screenX&(1<<(15-i)))?'white':'black';c.fillRect(i*16,0,16,64);}});
                </script></body>""")
                await page.bring_to_front()
                marker = await page.evaluate(
                    "({x:screenX+16,y:screenY+outerHeight-innerHeight+16})"
                )
                viewer_browser = await playwright.chromium.launch(
                    executable_path="/usr/bin/chromium", headless=True, chromium_sandbox=True
                )
                try:
                    for mode in ("vnc", "webrtc", "webrtc", "vnc"):
                        view = await viewer_browser.new_page(
                            viewport={"width": 1440, "height": 1000}
                        )
                        view.on(
                            "pageerror", lambda err: print("VIEWER_ERROR", str(err), flush=True)
                        )
                        await view.route(
                            "**/*",
                            lambda route: (
                                route.continue_()
                                if route.request.url.startswith(f"http://127.0.0.1:{port}/")
                                else route.abort()
                            ),
                        )
                        print("START", mode, flush=True)
                        await view.goto(f"http://127.0.0.1:{port}/?mode={mode}")
                        await view.wait_for_function("window.ready === true", timeout=30000)
                        await asyncio.to_thread(broker.send, struct.pack("!BBHH", 5, 0, 300, 500))
                        await asyncio.sleep(1)
                        start_index = len(input_times)
                        cpu_before = cpu_snapshot()
                        wall_start = time.monotonic()
                        result = await view.evaluate("(marker)=>window.run(marker)", marker)
                        wall = time.monotonic() - wall_start
                        cpu_after = cpu_snapshot()
                        # Container-scoped process CPU, 100% = one logical core.
                        result["cpuCorePercent"] = (
                            100
                            * sum(
                                max(0, seconds - cpu_before.get(pid, (started, 0))[1])
                                for pid, (started, seconds) in cpu_after.items()
                                if pid not in cpu_before or cpu_before[pid][0] == started
                            )
                            / wall
                        )
                        samples = sorted(input_times[start_index:])
                        result["inputAckP95Ms"] = samples[
                            min(len(samples) - 1, int(len(samples) * 0.95))
                        ]
                        results.append(result)
                        print("RESULT", json.dumps(result), flush=True)
                        await view.evaluate("window.stop()")
                        await view.close()
                        for peer in peers:
                            await peer.close()
                        peers.clear()
                        for track in tracks:
                            await track.close()
                        tracks.clear()
                finally:
                    await viewer_browser.close()
        finally:
            for peer in peers:
                await peer.close()
            for track in tracks:
                await track.close()
            await runner.cleanup()
            await asyncio.to_thread(broker.finish, release=True)
            await asyncio.to_thread(runtime.stop, state["generation"])
        print(
            "SUMMARY",
            json.dumps(
                {
                    "scope": "isolated-loopback-capture-comparison",
                    "width": 1440,
                    "height": 900,
                    "fps": fps,
                    "results": results,
                }
            ),
            flush=True,
        )
        if any(result["observed"] == 0 or result["lastVisibleId"] != 579 for result in results):
            raise SystemExit("No visible marker samples: benchmark invalid.")


if __name__ == "__main__":
    asyncio.run(main())
