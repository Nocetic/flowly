"""Inert, conversation-owned Google connection proposals. OAuth starts in the UI."""

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from flowly.integrations.gmail_connection import GmailConnection, GmailConnectionError
from flowly.integrations.google_permissions import (
    CONNECTION_SERVICES,
    normalize_services,
    service_permissions,
)
from flowly.profile import get_flowly_home

TTL = 900


@dataclass
class Request:
    id: str
    session_key: str
    reason: str
    services: list[str]
    future: asyncio.Future
    service: str | None = None
    created_at: float = field(default_factory=time.time)
    phase: str = "proposed"
    setup_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    observer: asyncio.Task | None = None

    def public(self):
        return {"id": self.id, "sessionKey": self.session_key, "reason": self.reason,
                "services": self.services, "service": self.service, "phase": self.phase, "setupId": self.setup_id,
                "createdAt": self.created_at * 1000, "expiresAt": (self.created_at + TTL) * 1000}


class GoogleChatRequests:
    def __init__(self):
        self.requests: dict[str, Request] = {}

    def _get(self, request_id, session_key):
        req = self.requests.get(request_id)
        if not req or req.session_key != session_key:
            raise GmailConnectionError("SETUP_NOT_FOUND")
        if req.future.done() or time.time() >= req.created_at + TTL:
            raise GmailConnectionError("EXPIRED")
        return req

    def pending(self, session_key):
        return {"requests": [req.public() for req in self.requests.values()
                             if req.session_key == session_key and not req.future.done() and time.time() < req.created_at + TTL]}

    def _finish(self, req, result):
        req.phase = result.get("status", "failed")
        if not req.future.done():
            req.future.set_result({**result, "requestId": req.id,
                                   "note": "Use only granted access. Do not bypass declined setup with commands or credentials."})

    async def request(self, session_key, reason, services, service=None):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise GmailConnectionError("INVALID_PARAMS")
        if not isinstance(session_key, str) or not 1 <= len(session_key) <= 512 or ":" not in session_key:
            raise GmailConnectionError("INVALID_PARAMS")
        try:
            selected = normalize_services(services, require_gmail=service is None)
            if service is not None and (service not in CONNECTION_SERVICES or service not in selected or set(selected) - set(service_permissions(service))):
                raise ValueError("INVALID_SERVICE")
        except ValueError:
            raise GmailConnectionError("INVALID_SERVICES") from None
        self.requests = {key: req for key, req in self.requests.items() if not req.future.done()}
        if len(self.requests) >= 32 or self.pending(session_key)["requests"]:
            raise GmailConnectionError("SETUP_IN_PROGRESS")
        req = Request(secrets.token_hex(16), session_key, reason.strip(), selected, asyncio.get_running_loop().create_future(), service=service)
        self.requests[req.id] = req
        try:
            async with asyncio.timeout(TTL):
                return await asyncio.shield(req.future)
        except TimeoutError:
            self._finish(req, {"status": "expired", "connected": False})
            return req.future.result()
        finally:
            # A stopped model turn must not revoke an owner-started OAuth flow.
            if not req.future.done():
                self._finish(req, {"status": "cancelled", "connected": False})
            if req.observer:
                req.observer.cancel()
                await asyncio.gather(req.observer, return_exceptions=True)

    async def begin(self, request_id, session_key, **kwargs):
        req = self._get(request_id, session_key)
        async with req.lock:
            self._get(request_id, session_key)
            chosen = kwargs.pop("service", None)
            if chosen != req.service:
                raise GmailConnectionError("INVALID_SERVICE")
            service = GmailConnection(service=req.service)
            setup = await asyncio.to_thread(service.begin, **kwargs)
            req.setup_id = setup["requestId"]
            req.phase = "started"
            if req.observer is None:
                req.observer = asyncio.create_task(self._observe(req, service))
            return setup

    async def _observe(self, req, service):
        while not req.future.done():
            try:
                async with req.lock:
                    if req.future.done():
                        return
                    result = await asyncio.to_thread(service.setup_status, req.setup_id)
                    if result.get("connected") or result.get("status") in {"expired", "cancelled", "failed", "revoked", "reauthorize"}:
                        self._finish(req, {key: value for key, value in result.items()
                                          if key in {"status", "connected", "email", "services", "requestedServices"}})
                        return
            except GmailConnectionError as error:
                if error.code in {"SETUP_NOT_FOUND", "CONNECTION_CHANGED"}:
                    self._finish(req, {"status": "cancelled", "connected": False})
                    return
            except (OSError, ValueError):
                pass
            await asyncio.sleep(3)

    async def cancel(self, request_id, session_key):
        req = self._get(request_id, session_key)
        async with req.lock:
            if req.future.done():
                return req.future.result()
            if req.setup_id:
                service = GmailConnection(service=req.service)
                try:
                    await asyncio.to_thread(service.cancel, req.setup_id)
                except GmailConnectionError as error:
                    if error.code != "SETUP_NOT_FOUND":
                        raise
                    status = await asyncio.to_thread(service.status)
                    if status.get("connected") and status.get("connectionId") == req.setup_id:
                        self._finish(req, status)
                        return req.future.result()
            self._finish(req, {"status": "cancelled", "connected": False})
            return req.future.result()


_managers: dict[str, GoogleChatRequests] = {}


def google_chat_requests():
    # Profile runtimes are isolated; this also keeps embedded/test profiles separate.
    return _managers.setdefault(str(get_flowly_home()), GoogleChatRequests())
