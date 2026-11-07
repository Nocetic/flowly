"""Signing in to a subscription provider from an app (``provider.login.*``).

The ChatGPT subscription (``openai_codex``) and xAI's Grok sign-in
(``xai_oauth``) sign in with a device code: Core asks the provider for a code,
the app shows it and opens the verification page, the person approves on any
device, and Core waits for the tokens in the background. Nothing has to reach the machine the agent runs on, so it works
the same on a Mac and on a cloud computer.

One sign-in per provider at a time: starting again cancels the one waiting.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

#: Providers that sign in with a device code.
DEVICE_LOGIN_PROVIDERS = frozenset({"openai_codex", "xai_oauth"})
#: How long a code is good for; OpenAI's own limit is about fifteen minutes.
LOGIN_TIMEOUT_SECONDS = 900
#: A finished sign-in is still answered for this long, then forgotten.
_KEEP_FINISHED_SECONDS = 600


class ProviderLoginError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Login:
    id: str
    key: str
    user_code: str = ""
    verification_url: str = ""
    expires_at: float = 0.0
    state: str = "pending"  # pending | done | failed | expired | cancelled
    error: str | None = None
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {"loginId": self.id, "key": self.key, "state": self.state}
        if self.state == "pending":
            body.update(userCode=self.user_code, verificationUrl=self.verification_url,
                        expiresIn=max(0, int(self.expires_at - time.time())))
        if self.error:
            body["error"] = self.error
        return body


_logins: dict[str, _Login] = {}


def _forget_finished() -> None:
    now = time.time()
    for login_id, login in list(_logins.items()):
        if login.finished_at is not None and now - login.finished_at > _KEEP_FINISHED_SECONDS:
            _logins.pop(login_id, None)


def _finish(login: _Login, state: str, error: str | None = None) -> None:
    if login.state != "pending":
        return
    login.state, login.error, login.finished_at = state, error, time.time()


async def start(key: str, *, on_signed_in: Callable[[], Awaitable[Any]] | None = None) -> dict[str, Any]:
    """Begin signing in to ``key``; answers once the code is known.

    ``on_signed_in`` runs on this event loop after the tokens are saved (the
    caller flushes the catalogue and reloads the provider).
    """
    if key not in DEVICE_LOGIN_PROVIDERS:
        raise ProviderLoginError("LOGIN_UNSUPPORTED", f"{key} does not sign in from the app")
    _forget_finished()
    for other in _logins.values():
        if other.key == key and other.state == "pending":
            other.cancel.set()
            _finish(other, "cancelled")

    login = _Login(id=secrets.token_urlsafe(12), key=key)
    loop = asyncio.get_running_loop()
    code_ready: asyncio.Future[None] = loop.create_future()

    def on_user_code(user_code: str, verification_url: str) -> None:
        login.user_code, login.verification_url = user_code, verification_url
        login.expires_at = time.time() + LOGIN_TIMEOUT_SECONDS
        loop.call_soon_threadsafe(lambda: code_ready.done() or code_ready.set_result(None))

    def run() -> None:
        try:
            _sign_in(key, on_user_code=on_user_code, cancelled=login.cancel.is_set)
        except Exception as exc:
            reason = str(exc)
            if login.cancel.is_set():
                _finish(login, "cancelled")
            elif "timed out" in reason:
                _finish(login, "expired")
            else:
                _finish(login, "failed", type(exc).__name__)
            logger.info("[ProviderLogin] {} sign-in ended: {}", key, login.state)
            loop.call_soon_threadsafe(lambda: code_ready.done() or code_ready.set_exception(
                ProviderLoginError("LOGIN_FAILED", "The sign-in could not start.")))
            return
        _finish(login, "done")
        logger.info("[ProviderLogin] {} signed in", key)
        if on_signed_in is not None:
            asyncio.run_coroutine_threadsafe(on_signed_in(), loop)

    _logins[login.id] = login
    threading.Thread(target=run, name=f"provider-login-{key}", daemon=True).start()
    try:
        await asyncio.wait_for(code_ready, timeout=30)
    except asyncio.TimeoutError:
        login.cancel.set()
        _finish(login, "failed", "Timeout")
        raise ProviderLoginError("LOGIN_FAILED", "The sign-in could not start.") from None
    return login.wire()


def _sign_in(key: str, *, on_user_code: Callable[[str, str], None], cancelled: Callable[[], bool]) -> None:
    """Run ``key``'s device sign-in to the end (blocking): its tokens are saved where every agent reads them."""
    if key == "xai_oauth":
        from flowly.auth import xai_oauth

        xai_oauth.login_with_device_code(
            client_id=xai_oauth.require_client_id(), on_user_code=on_user_code,
            timeout_seconds=LOGIN_TIMEOUT_SECONDS, cancelled=cancelled,
        )
        return
    from flowly.auth import openai_codex

    openai_codex.login_with_device_code(
        client_id=openai_codex.require_client_id(), on_user_code=on_user_code,
        timeout_seconds=LOGIN_TIMEOUT_SECONDS, cancelled=cancelled,
    )


def status(login_id: str) -> dict[str, Any]:
    login = _logins.get(login_id)
    if login is None:
        raise ProviderLoginError("LOGIN_NOT_FOUND", "This sign-in is no longer waiting.")
    if login.state == "pending" and time.time() > login.expires_at > 0:
        login.cancel.set()
        _finish(login, "expired")
    return login.wire()


def cancel(login_id: str) -> dict[str, Any]:
    login = _logins.get(login_id)
    if login is None:
        raise ProviderLoginError("LOGIN_NOT_FOUND", "This sign-in is no longer waiting.")
    login.cancel.set()
    _finish(login, "cancelled")
    return login.wire()
