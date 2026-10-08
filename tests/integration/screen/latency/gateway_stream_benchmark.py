"""Pixel delivery latency through the real authenticated gateway stream.

A synthetic RFB peer emits a 48 KiB frame every 1/60 s carrying its send time;
the client records arrival of each frame's last byte (same process clock).
--load N keeps N blocking jobs in the default thread pool, as agent tools do.
Disposable: synthetic peer, no Xvnc, no network beyond container loopback.
"""

import argparse
import asyncio
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp.test_utils import TestServer

import flowly.profile as profiles
from flowly.desktop_runtime.authority import ScreenAuthority
from flowly.desktop_runtime.input_service import InputService
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.observers import ObserverRegistry
from flowly.gateway.display import STREAM_PATH, _resolve
from flowly.gateway.server import GatewayServer

FRAME = 48 * 1024
MAGIC = b"FLWYFRAM"
parser = argparse.ArgumentParser()
parser.add_argument("--load", type=int, default=0)
parser.add_argument("--frames", type=int, default=600)


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    home = tmp / "home"
    home.mkdir()
    profiles._DEFAULT_HOME = home
    profiles._PROFILES_ROOT = home / "profiles"
    os.environ["FLOWLY_HOME"] = str(home)
    ScreenRuntime.capabilities = staticmethod(
        lambda: {"supported": True, "reason": None, "missing": []}
    )
    gateway = GatewayServer(
        host="127.0.0.1",
        port=0,
        auth_token="bench-owner",
        require_loopback_auth=True,
        enable_profile_host=True,
    )

    async def ignore(*_a, **_k):
        return None

    gateway.on_chat_message = ignore
    server = TestServer(gateway._create_app())
    await server.start_server()
    sockets = Path(tempfile.mkdtemp(prefix="bs-", dir="/tmp"))
    service = _resolve(gateway, {"profileId": "default"})
    authority = ScreenAuthority(tmp / "authority")
    authority.start("gen")
    registry = ObserverRegistry(authority, "gen", sockets, viewers=service.viewers)
    inputs = InputService(authority, "gen", sockets, observers=registry, viewers=service.viewers)
    service._runtime = SimpleNamespace(
        socket_directory=sockets,
        authority=authority,
        assert_current_directory=lambda: None,
        rfb_socket_for=lambda state: sockets / "rfb.sock",
        status=lambda: {
            "phase": "running",
            "generation": "gen",
            "socket": "/x",
            "cdpPort": 9999,
            "observerServiceVersion": 3,
            "inputServiceVersion": 3,
        },
    )
    start_frames = asyncio.Event()

    async def peer(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        await reader.readexactly(12)
        writer.write(b"\x01\x01")
        await writer.drain()
        await reader.readexactly(1)
        writer.write(bytes(4))
        await writer.drain()
        await reader.readexactly(1)
        writer.write(bytes(24))
        await writer.drain()
        await start_frames.wait()
        filler = bytes(FRAME - 16)
        period = 1 / 60
        next_at = time.perf_counter()
        for _ in range(args.frames):
            writer.write(MAGIC + time.perf_counter_ns().to_bytes(8, "big") + filler)
            await writer.drain()
            next_at += period
            await asyncio.sleep(max(0, next_at - time.perf_counter()))
        await asyncio.sleep(1)
        writer.close()

    backend = await asyncio.start_unix_server(peer, sockets / "rfb.sock")
    stop_load = False

    async def load():
        while not stop_load:
            await asyncio.to_thread(time.sleep, 0.02)

    loaders = [asyncio.create_task(load()) for _ in range(args.load)]

    async with aiohttp.ClientSession() as client:
        async with client.post(
            server.make_url("/api/auth/ws-ticket"), headers={"Authorization": "Bearer bench-owner"}
        ) as r:
            ticket = (await r.json())["ticket"]
        socket = await client.ws_connect(server.make_url("/ws?ticket=" + ticket))

        async def rpc(method, params):
            await socket.send_json({"type": "rpc", "id": "x", "method": method, "params": params})
            return await socket.receive_json(timeout=10)

        status = (await rpc("display.status", {"profileId": "default"}))["result"]
        request = {
            "profileId": "default",
            "expectedHostId": status["hostId"],
            "expectedBotId": status["botId"],
            "generation": "gen",
        }
        observed = (await rpc("display.observe", request))["result"]
        headers = {
            "Authorization": "Bearer bench-owner",
            "X-Flowly-Profile": "default",
            "X-Flowly-Host-Id": status["hostId"],
            "X-Flowly-Bot-Id": status["botId"],
            "X-Flowly-Screen-Generation": "gen",
            "X-Flowly-Screen-Ticket": observed["ticket"],
        }
        ws = await client.ws_connect(server.make_url(STREAM_PATH), headers=headers)
        buffer = bytearray()

        async def read(n):
            while len(buffer) < n:
                m = await ws.receive(timeout=10)
                buffer.extend(m.data)
            v = bytes(buffer[:n])
            del buffer[:n]
            return v

        assert await read(12) == b"RFB 003.008\n"
        await ws.send_bytes(b"RFB 003.008\n")
        await read(2)
        await ws.send_bytes(b"\x01")
        await read(4)
        await ws.send_bytes(b"\x00")
        init = await read(24)
        await read(int.from_bytes(init[20:24], "big"))
        start_frames.set()
        delays = []
        started = time.perf_counter()
        while len(delays) < args.frames:
            frame = await read(FRAME)
            assert frame[:8] == MAGIC
            delays.append((time.perf_counter_ns() - int.from_bytes(frame[8:16], "big")) / 1e6)
        elapsed = time.perf_counter() - started
        await ws.close()
        await socket.close()
    stop_load = True
    await asyncio.gather(*loaders)
    await server.close()
    backend.close()
    await asyncio.to_thread(registry.close)
    await asyncio.to_thread(inputs.close)
    q = statistics.quantiles(delays, n=100)
    print(
        json.dumps(
            {
                "load": args.load,
                "frames": len(delays),
                "fps": round(len(delays) / elapsed, 1),
                "p50": round(q[49], 2),
                "p95": round(q[94], 2),
                "p99": round(q[98], 2),
                "max": round(max(delays), 2),
            }
        )
    )


if __name__ == "__main__":
    args = parser.parse_args()
    asyncio.run(main())
