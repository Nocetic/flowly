"""Outbound-only screen transport; every byte still passes local display admission."""

import asyncio
import time
import json
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from loguru import logger

PROTOCOL = "screen-reverse-stream-v1"
MAX_FRAME = 256 * 1024
CONNECT_TIMEOUT = 5


def _text(value, limit=256):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) <= 32 or ord(c) == 127 for c in value)


def relay_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != "/relay"
            or (parsed.scheme == "ws" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Screen relay requires a protected relay origin")
    if parsed.port is not None and not 0 < parsed.port < 65536:
        raise ValueError("Invalid screen relay port")
    return urlunsplit((parsed.scheme, parsed.netloc, "/screen/agent", "", ""))


def gateway_endpoint(gateway) -> tuple[str, str] | None:
    """Use only the owning gateway's token-authenticated loopback/wildcard listener.

    Display routes demand the token even where other loopback routes do not.
    """
    if not _text(getattr(gateway, "_auth_token", None), 4096):
        return None
    host = {"0.0.0.0": "127.0.0.1", "::": "[::1]", "::1": "[::1]"}.get(gateway.host, gateway.host)
    if host not in {"127.0.0.1", "[::1]", "localhost"} or type(gateway.port) is not int or not 0 < gateway.port < 65536:
        return None
    return f"ws://{host}:{gateway.port}/api/display/stream", gateway._auth_token


class RelayScreenStreams:
    def __init__(self, *, relay_url: str, server_id: str, token: Callable[[], str],
                 gateway, current: Callable[[], bool], ssl_context=None):
        self._relay_url = relay_endpoint(relay_url)
        self._server_id, self._token, self._gateway = server_id, token, gateway
        self._current, self._ssl = current, ssl_context
        self._tasks: dict[str, asyncio.Task] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._closed = False

    @property
    def available(self) -> bool:
        return not self._closed and self._current() and gateway_endpoint(self._gateway) is not None

    def open(self, message: dict) -> bool:
        allowed = {"type", "protocol", "channelId", "serverId", "userId", "profileId", "hostId", "botId", "generation", "ticket", "inputProtocol"}
        if (not self.available or not isinstance(message, dict) or set(message) - allowed
                or message.get("type") != "screen.stream.open" or message.get("protocol") != PROTOCOL
                or message.get("serverId") != self._server_id or not _text(message.get("userId"))
                or not _text(message.get("channelId"), 43) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", message["channelId"])
                or not _text(message.get("profileId"), 64)
                or not all(_text(message.get(key)) for key in ("hostId", "botId", "generation"))
                or not _text(message.get("ticket"), 512) or not re.fullmatch(r"[A-Za-z0-9_-]+", message["ticket"])
                or message.get("inputProtocol") not in (None, "epoch-u64-v1")):
            return False
        channel = message["channelId"]
        if channel in self._seen or channel in self._tasks or len(self._tasks) >= 4:
            return False
        self._seen[channel] = None
        if len(self._seen) > 128:
            self._seen.popitem(last=False)
        task = asyncio.create_task(self._bridge(dict(message)), name="relay-screen-stream")
        self._tasks[channel] = task

        def completed(done):
            if self._tasks.get(channel) is done:
                self._tasks.pop(channel, None)
            if not done.cancelled():
                error = done.exception()
                if error:
                    # Neither request headers nor exception strings may disclose credentials.
                    status = getattr(error, "status", None)
                    logger.info("[Screen] relay stream failed: {}{}", type(error).__name__,
                                f" (HTTP {status})" if isinstance(status, int) else "")
        task.add_done_callback(completed)
        return True

    @asynccontextmanager
    async def _connect(self, client, url, **kwargs):
        # Socket-connect timeouts alone do not bound a stalled HTTP upgrade.
        async with asyncio.timeout(CONNECT_TIMEOUT):
            socket = await client.ws_connect(url, **kwargs)
        try:
            yield socket
        finally:
            await socket.close()

    async def _bridge(self, message: dict):
        endpoint = gateway_endpoint(self._gateway)
        if not endpoint or not self.available:
            return
        local_url, local_token = endpoint
        headers = {"Authorization": f"Bearer {local_token}", "X-Flowly-Profile": message["profileId"],
                   "X-Flowly-Host-Id": message["hostId"], "X-Flowly-Bot-Id": message["botId"],
                   "X-Flowly-Screen-Generation": message["generation"], "X-Flowly-Screen-Ticket": message["ticket"]}
        if message.get("inputProtocol"):
            headers["X-Flowly-Screen-Input"] = message["inputProtocol"]
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
            # The local gateway consumes the one-use ticket and verifies the
            # viewer/profile/generation. The relay never substitutes that check.
            async with self._connect(client, local_url, headers=headers, max_msg_size=MAX_FRAME,
                                         heartbeat=20, autoclose=True, compress=0) as local:
                headers.clear()
                if not self.available:
                    return
                remote_headers = {"Authorization": f"Bearer {self._token()}", "X-Flowly-Screen-Channel": message["channelId"]}
                async with self._connect(client, self._relay_url, headers=remote_headers,
                                             ssl=self._ssl if self._ssl is not None else True,
                                             max_msg_size=MAX_FRAME, heartbeat=20, autoclose=True, compress=0) as remote:
                    remote_headers.clear()
                    if not self.available:
                        return
                    counts = {"pixels": 0, "input": 0}
                    pixels = asyncio.create_task(self._copy(local, remote, counts, "pixels"))
                    inputs = asyncio.create_task(self._copy(remote, local, counts, "input"))
                    opened = time.monotonic()
                    try:
                        done, _ = await asyncio.wait([pixels, inputs], return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in (pixels, inputs):
                            task.cancel()
                        await asyncio.gather(pixels, inputs, return_exceptions=True)
                    first = next(iter(done))
                    error = None if first.cancelled() else first.exception()
                    # Which side ended it, and how: the gateway's own line says why its side did.
                    logger.info("[Screen] relay stream ended: {} {} | {}s | gateway close {} | relay close {} | frames {}",
                                "pixels" if first is pixels else "input",
                                "ended" if error is None else type(error).__name__,
                                round(time.monotonic() - opened, 1), local.close_code, remote.close_code,
                                json.dumps(counts, separators=(",", ":")))

    async def _copy(self, source, target, counts=None, side=""):
        async for frame in source:
            if not self.available or frame.type != aiohttp.WSMsgType.BINARY:
                return
            if len(frame.data) > MAX_FRAME:
                return
            # Never queue an unbounded stream or retry uncertain input writes.
            await asyncio.wait_for(target.send_bytes(frame.data), timeout=5)
            if counts is not None:
                counts[side] += 1

    async def close(self):
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._seen.clear()
