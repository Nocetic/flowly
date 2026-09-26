"""Connection-owned display RPC proxy into the authenticated local gateway.

Only live browser sessions announced by the relay may open local sockets.
Requests are never retried; replies use a private, single-recipient envelope.
"""

import asyncio
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field

import aiohttp

from flowly.desktop_runtime.relay_stream import gateway_endpoint
from flowly.gateway.display import METHODS

MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 256 * 1024
CONNECT_TIMEOUT = 5
REQUEST_TIMEOUT = 60
EVENTS = frozenset({"display.status", "display.control", "display.heartbeat", "display.runtime.failed", "display.subscription.closed"})


def identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and all(32 < ord(c) < 127 for c in value)


@dataclass
class Client:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    pending: dict = field(default_factory=dict)
    seen: OrderedDict = field(default_factory=OrderedDict)
    task: asyncio.Task | None = None
    closing: bool = False


def _relay_capability(value):
    if (isinstance(value, dict) and value.get("protocolVersion") == 1
            and value.get("supported") is True and isinstance(value.get("transports"), list)):
        return {**value, "transports": [*value["transports"], *([] if "relay" in value["transports"] else ["relay"])]}
    return value


class RelayScreenRpc:
    def __init__(self, *, gateway, current, send):
        self.gateway, self.current, self.send = gateway, current, send
        self.clients: dict[str, Client] = {}
        self.closed = False
        self.enabled = False

    def connected(self, session):
        if not self.closed and identifier(session) and session not in self.clients and len(self.clients) < 64:
            self.clients[session] = Client()

    async def emit(self, session, message, client=None):
        if (not self.closed and self.current() and session in self.clients
                and (client is None or self.clients[session] is client)):
            await asyncio.wait_for(self.send({"type": "screen.rpc", "sessionId": session,
                                              "message": message}), 5)

    async def error(self, session, rpc_id, code, client=None):
        await self.emit(session, {"type": "rpc", "id": rpc_id, "error": {
            "code": code, "message": "The screen request could not be completed."}}, client)

    async def submit(self, session, message):
        client = self.clients.get(session)
        rpc_id = message.get("id")
        if not self.enabled or self.closed or not self.current() or client is None:
            return  # No trusted recipient; never infer one from request params.
        if not identifier(rpc_id):
            return
        if (not isinstance(message.get("method"), str) or message["method"] not in METHODS
                or not isinstance(message.get("params", {}), dict)):
            await self.error(session, rpc_id, "INVALID_PARAMS")
            return
        try:
            raw = json.dumps({"type": "rpc", "id": rpc_id, "method": message["method"],
                              "params": message.get("params", {})}, allow_nan=False)
        except (TypeError, ValueError):
            await self.error(session, rpc_id, "INVALID_PARAMS")
            return
        if len(raw.encode()) > MAX_REQUEST:
            await self.error(session, rpc_id, "INVALID_PARAMS")
            return
        if client.closing:
            await self.error(session, rpc_id, "CONTROL_UNAVAILABLE")
            return
        if rpc_id in client.seen or rpc_id in client.pending:
            await self.error(session, rpc_id, "OPERATION_CONFLICT")
            return
        if (len(client.pending) >= 8 or (client.task is None
                and sum(c.task is not None for c in self.clients.values()) >= 16)):
            await self.error(session, rpc_id, "SCREEN_BUSY")
            return
        client.seen[rpc_id] = None
        if len(client.seen) > 128:
            client.seen.popitem(last=False)
        client.pending[rpc_id] = asyncio.get_running_loop().time()
        client.queue.put_nowait(raw)
        if client.task is None:
            client.task = asyncio.create_task(self._run(session, client), name="relay-screen-rpc")

    async def _run(self, session, client):
        workers = []
        try:
            endpoint = gateway_endpoint(self.gateway)
            if endpoint is None:
                return
            url, token = endpoint
            base = url.removesuffix("/api/display/stream")
            async with aiohttp.ClientSession(trust_env=False) as http:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    async with http.post(base.replace("ws://", "http://", 1) + "/api/auth/ws-ticket",
                                         headers={"Authorization": f"Bearer {token}"}, allow_redirects=False) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(4096):
                            body.extend(chunk)
                            if len(body) > 4096:
                                raise ValueError("Invalid local admission")
                        ticket = json.loads(body).get("ticket")
                        if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", ticket):
                            raise ValueError("Invalid local admission")
                    ws = await http.ws_connect(base + "/ws?ticket=" + ticket,
                                               heartbeat=20, compress=0, max_msg_size=MAX_RESPONSE)
                async with ws:
                    workers = [asyncio.create_task(self._write(client, ws)),
                               asyncio.create_task(self._read(session, client, ws)),
                               asyncio.create_task(self._deadline(client))]
                    await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        except (Exception, asyncio.CancelledError):
            # Credential-bearing exceptions are deliberately not serialized.
            pass
        finally:
            client.closing = True
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            pending = tuple(client.pending)
            client.pending.clear()
            while not client.queue.empty():
                client.queue.get_nowait()  # Includes ephemeral installation credentials.
            try:
                for rpc_id in pending:
                    await self.error(session, rpc_id, "CONTROL_UNAVAILABLE", client)
            except Exception:
                pass
            finally:
                client.task = None
                client.closing = False

    async def _write(self, client, ws):
        while not self.closed and self.current():
            raw = await client.queue.get()
            if self.closed or not self.current():
                return
            await asyncio.wait_for(ws.send_str(raw), 5)
            del raw

    async def _read(self, session, client, ws):
        async for frame in ws:
            if not self.current() or frame.type != aiohttp.WSMsgType.TEXT:
                return
            message = json.loads(frame.data)
            if not isinstance(message, dict):
                return
            if message.get("type") == "rpc" and identifier(message.get("id")) and message["id"] in client.pending:
                client.pending.pop(message["id"])
                if isinstance(message.get("result"), dict):
                    result = _relay_capability(message["result"])
                    if isinstance(result.get("status"), dict):
                        result = {**result, "status": _relay_capability(result["status"])}
                    message = {**message, "result": result}
                await self.emit(session, message, client)
            elif message.get("type") == "event" and message.get("event") in EVENTS:
                message = {**message, "data": _relay_capability(message.get("data"))}
                await self.emit(session, message, client)
            # No chat, credentials from other clients, or generic gateway events.

    async def _deadline(self, client):
        while not self.closed and self.current():
            await asyncio.sleep(0.25)
            now = asyncio.get_running_loop().time()
            if any(now - started >= REQUEST_TIMEOUT for started in client.pending.values()):
                return

    async def disconnected(self, session):
        client = self.clients.pop(session, None)
        if client and client.task:
            client.task.cancel()
            await asyncio.gather(client.task, return_exceptions=True)

    async def close(self):
        self.closed = True
        tasks = [c.task for c in self.clients.values() if c.task is not None]
        self.clients.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
