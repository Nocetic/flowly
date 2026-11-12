"""One port, TLS for other machines, plain HTTP for this one.

A gateway reached from other machines speaks TLS (flowly/gateway/tls.py),
but a dozen callers on the gateway's own machine reach it by plain HTTP on
the same port: the profile hosts, MCP tools, ``flowly cron run``, the TUI,
the screen stream, health checks. Moving them all, or the apps to a new
port, would break installs that work today. So this site looks at each
connection's first byte before taking it: a TLS ClientHello (``0x16``) is
answered with TLS; anything else is plain HTTP, taken only from loopback,
or from anywhere while ``allow_plain_remote`` (apps that predate pinning,
until the owner turns it off).

A connection that sends nothing within ``first_byte_timeout`` is closed.
The peek is ``MSG_PEEK``: the byte stays for the TLS or HTTP handshake.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from typing import Any

from aiohttp import web
from loguru import logger

TLS_HANDSHAKE = 0x16


def is_loopback_peer(address: Any) -> bool:
    try:
        ip = ipaddress.ip_address(str(address[0]).split("%", 1)[0])
    except (ValueError, TypeError, IndexError):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


class _Listening:
    """What aiohttp and the gateway read of a started site: its sockets, and close()."""

    def __init__(self, sock: socket.socket, task: asyncio.Task) -> None:
        self.sockets = [sock]
        self._task = task

    def close(self) -> None:
        self._task.cancel()
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass


class SniffingSite(web.BaseSite):
    """A site on ``host:port`` that answers TLS and plain HTTP on the one port."""

    __slots__ = ("_host", "_port", "_allow_plain_remote", "_first_byte_timeout", "_pending")

    def __init__(
        self,
        runner: web.BaseRunner,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        allow_plain_remote: bool,
        first_byte_timeout: float = 10.0,
        backlog: int = 128,
    ) -> None:
        super().__init__(runner, ssl_context=ssl_context, backlog=backlog)
        self._host = host
        self._port = port
        self._allow_plain_remote = allow_plain_remote
        self._first_byte_timeout = first_byte_timeout
        self._pending: set[asyncio.Task] = set()

    @property
    def name(self) -> str:
        return f"https+http://{self._host}:{self._port}"

    def _bind(self) -> socket.socket:
        infos = socket.getaddrinfo(self._host or None, self._port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        family, kind, proto, _canon, address = infos[0]
        sock = socket.socket(family, kind, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and self._host in ("::", ""):
                # One socket for IPv4 and IPv6, as a bind to "::" is everywhere else.
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(address)
            sock.listen(self._backlog)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        return sock

    async def start(self) -> None:
        await super().start()
        sock = self._bind()
        task = asyncio.create_task(self._accept(sock))
        self._server = _Listening(sock, task)  # type: ignore[assignment]

    async def stop(self) -> None:
        for task in list(self._pending):
            task.cancel()
        await super().stop()

    async def _accept(self, sock: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                connection, address = await loop.sock_accept(sock)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                if sock.fileno() < 0:
                    return
                logger.debug("[Gateway] accept failed: {}", exc)
                await asyncio.sleep(0.05)
                continue
            task = asyncio.create_task(self._take(connection, address))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _first_byte(self, connection: socket.socket) -> bytes:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        fd = connection.fileno()
        loop.add_reader(fd, lambda: ready.done() or ready.set_result(None))
        try:
            await asyncio.wait_for(ready, self._first_byte_timeout)
        finally:
            loop.remove_reader(fd)
        return connection.recv(1, socket.MSG_PEEK)

    async def _take(self, connection: socket.socket, address: Any) -> None:
        connection.setblocking(False)
        try:
            first = await self._first_byte(connection)
        except (asyncio.TimeoutError, OSError):
            connection.close()
            return
        except asyncio.CancelledError:
            connection.close()
            raise
        if not first:
            connection.close()
            return
        tls = first[0] == TLS_HANDSHAKE
        if not tls and not self._allow_plain_remote and not is_loopback_peer(address):
            # Another machine without encryption: refused before a byte of HTTP is read.
            connection.close()
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.connect_accepted_socket(
                self._runner.server,  # type: ignore[arg-type]
                connection,
                ssl=self._ssl_context if tls else None,
                ssl_handshake_timeout=self._first_byte_timeout if tls else None,
            )
        except (OSError, ssl.SSLError, asyncio.TimeoutError, ConnectionError) as exc:
            logger.debug("[Gateway] connection from {} not taken: {}", address, exc)
            connection.close()
