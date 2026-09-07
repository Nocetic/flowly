"""Owner-managed MCP access to the existing profile and live tool runtime."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

from flowly.mcp.external_access import ExternalAccessError, ExternalAccessStore
from flowly.mcp.server.tool_runtime import MAX_ARGUMENT_BYTES, RuntimeToolBridge, ToolBridgeError


class ExternalMCPService:
    def __init__(self, path: Path, bridge: RuntimeToolBridge, *, conversation_server=None):
        self.store = ExternalAccessStore(path)
        self.bridge = bridge
        self._conversation_server = conversation_server
        self._closed = False
        self.local_endpoint: str | None = None
        self._calls: dict[asyncio.Task, str] = {}

    @property
    def conversation_server(self):
        if self._conversation_server is None:
            from functools import partial

            from flowly.mcp.server.readplane import SessionReader, channels_list
            from flowly.mcp.server.serve import create_server
            from flowly.mcp.server.writeplane import _request

            reader = SessionReader(self.bridge.owner.sessions)
            self._conversation_server = create_server(
                allow_writes=True, reader=reader,
                channel_reader=partial(channels_list, config_path=self.store.path.parent / "config.json", reader=reader),
                control_request=partial(_request, runtime_home=self.store.path.parent),
                journal_path=self.store.path.parent / "mcp" / "conversation-events.sqlite",
            )
        return self._conversation_server

    def sessions(self) -> list[dict]:
        return [{"key": row["key"], "title": row.get("title") or row["key"]}
                for row in self.bridge.owner.sessions.list_sessions() if isinstance(row.get("key"), str)]

    def _session_exists(self, key: str) -> bool:
        return any(row["key"] == key for row in self.sessions())

    async def catalog(self, session_key: str | None) -> dict:
        if self._closed:
            raise ExternalAccessError("MCP access runtime is stopped")
        sessions = self.sessions()
        if session_key is not None and not self._session_exists(session_key):
            raise ExternalAccessError("Select an existing conversation in this profile")
        definitions = [tool.model_dump(by_alias=True, exclude_none=True)
                       for tool in await self.conversation_server.list_tools()]
        base_names = {row["name"] for row in definitions}
        if session_key:
            definitions.extend(row for row in self.bridge.available_tools(session_key) if row["name"] not in base_names)
        return {"sessions": sessions, "tools": definitions}

    async def owner(self, action: str, params: dict) -> dict:
        if self._closed:
            raise ExternalAccessError("MCP access runtime is stopped")
        if not isinstance(params, dict):
            raise ExternalAccessError("MCP access settings must be an object")
        if action == "catalog":
            return await self.catalog(params.get("sessionKey"))
        if action == "list":
            rows = self.store.list()
            existing = {row["key"] for row in self.sessions()}
            for row in rows:
                if row["status"] == "active" and row["sessionKey"] not in existing:
                    row["status"] = "unavailable"
            return {"credentials": rows, "endpointPath": "/mcp", "localEndpoint": self.local_endpoint}
        if action == "create":
            key = params.get("sessionKey")
            if not isinstance(key, str) or not self._session_exists(key):
                raise ExternalAccessError("Select an existing conversation in this profile")
            manager = self.bridge.owner.sessions
            def identity():
                return next((row.get("created_at") for row in manager.list_sessions() if row["key"] == key), None)

            generation = identity()
            catalog = await self.catalog(key)
            # Do not hold a filesystem lease across async tool discovery. Recheck
            # the incarnation under the same cross-process lease as session deletion.
            with manager._session_write_lock(key):
                if generation is None or identity() != generation:
                    raise ExternalAccessError("The owning conversation changed; review permissions again")
                return self.store.create(params, available={row["name"] for row in catalog["tools"]})
        if action == "revoke":
            row = self.store.revoke(params.get("id"))
            for task, key_id in list(self._calls.items()):
                if key_id == row["id"]:
                    task.cancel()
            return row
        raise ExternalAccessError("Unknown MCP access method")

    def authorize(self, token: str, name: str | None = None) -> dict:
        if self._closed:
            raise ExternalAccessError("MCP access runtime is stopped")
        row = self.store.authorize(token, name)
        if not self._session_exists(row["sessionKey"]):
            raise ExternalAccessError("The owning conversation no longer exists")
        return row

    async def list_tools(self, token: str) -> list[dict]:
        row = self.authorize(token)
        catalog = await self.catalog(row["sessionKey"])
        # Recheck after discovery, including a key revoked while awaiting it.
        row = self.authorize(token)
        return [item for item in catalog["tools"] if item["name"] in row["tools"]]

    async def call(self, token: str, name: str, arguments: dict) -> dict:
        row = self.authorize(token, name)
        if not isinstance(arguments, dict):
            raise ExternalAccessError("Tool arguments must be an object")
        try:
            if len(json.dumps(arguments, allow_nan=False).encode()) > MAX_ARGUMENT_BYTES:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ExternalAccessError("Tool arguments exceed the finite JSON size limit") from None
        if len(self._calls) >= 32 or sum(key == row["id"] for key in self._calls.values()) >= 4:
            raise ExternalAccessError("MCP access call capacity reached; wait for current calls")
        task = asyncio.create_task(self._invoke(token, name, arguments, row))
        self._calls[task] = row["id"]
        # SDK cancellation scopes may repeatedly interrupt the caller's async
        # finally block after an HTTP disconnect. Release capacity when the
        # actual invocation finishes, independent of that caller's cleanup.
        task.add_done_callback(lambda finished: self._calls.pop(finished, None))

        async def watch_authority():
            while not task.done():
                await asyncio.sleep(0.1)
                self.authorize(token, name)

        watcher = asyncio.create_task(watch_authority())
        try:
            async with asyncio.timeout(610):
                done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    await watcher
                result = await task
                self.authorize(token, name)
                return result
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # An owner revocation cancels the child invocation, not the HTTP
            # request. Return an MCP error instead of abandoning its reply.
            raise ExternalAccessError("MCP call cancelled because its access was withdrawn") from None
        except TimeoutError:
            raise ExternalAccessError("MCP call exceeded its time limit") from None
        finally:
            task.cancel()
            watcher.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)
            self._calls.pop(task, None)

    async def _invoke(self, token: str, name: str, arguments: dict, row: dict) -> dict:
        base_names = {tool.name for tool in await self.conversation_server.list_tools()}
        self.authorize(token, name)
        if name in base_names:
            result = await self.conversation_server.call_tool(name, arguments)
            return result.model_dump(by_alias=True, exclude_none=True)

        def permitted():
            try:
                self.authorize(token, name)
                return True
            except ExternalAccessError:
                return False

        grant = self.bridge.create_grant(
            row["sessionKey"], names=[name], allow_writes=True,
            ttl=max(1, min(600, row["expiresAt"] - time.time())), authority_check=permitted,
        )
        try:
            return await self.bridge.call(grant["token"], secrets.token_hex(16), name, arguments)
        finally:
            try:
                tasks = self.bridge.revoke(grant["token"])
                await asyncio.gather(*tasks, return_exceptions=True)
            except ToolBridgeError:
                pass

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._calls)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
