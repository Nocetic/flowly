"""Device registry + relay-forwarded push notifications.

A gateway can't send APNs/FCM directly — the app's push credentials live only on
the relay. So a device registers anonymously with the relay (getting an opaque
``pushId`` + ``pushSecret``), hands those to its trusted gateway over the
``push.register`` RPC, and the gateway triggers pushes by POSTing to the relay's
``/api/push/send`` (Bearer ``pushSecret``). The raw device token never reaches
the gateway. Persisted at ``~/.flowly/push_subs.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.profile import get_flowly_home

# HTTPS base for the relay's push endpoints. Used even by gateway-only bots that
# never chat over the relay — the host is constant, not per-account.
_DEFAULT_RELAY_BASE = "https://relay.useflowlyapp.com"


def _store_path() -> Path:
    return get_flowly_home() / "push_subs.json"


class PushRegistry:
    """Device push registrations, persisted as JSON. One entry per ``pushId``."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _store_path()
        self._subs: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._subs = data.get("subs", []) if isinstance(data, dict) else []
        except Exception as e:  # pragma: no cover — never block on a bad file
            logger.warning(f"[push] load failed: {e}")
            self._subs = []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(f".tmp.{secrets.token_hex(4)}")
            tmp.write_text(json.dumps({"subs": self._subs}, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(self._path))
        except Exception as e:  # pragma: no cover
            logger.warning(f"[push] save failed: {e}")

    def register(self, *, push_id: str, push_secret: str, gateway_id: str = "",
                 platform: str = "ios", kind: str = "gateway") -> None:
        if not push_id or not push_secret:
            return
        # Dedup by pushId — a device re-registering replaces its old entry.
        self._subs = [s for s in self._subs if s.get("pushId") != push_id]
        self._subs.append({
            "pushId": push_id,
            "pushSecret": push_secret,
            "gatewayId": gateway_id or "",
            # How this device reaches THIS bot — "gateway" (gatewayId is a local
            # gateway uuid) or "relay" (gatewayId holds the relay serverId).
            # Decides whether a push's deep-link data carries gatewayId vs
            # serverId, which the app resolves differently.
            "kind": kind or "gateway",
            "platform": platform or "ios",
            "createdAt": int(time.time() * 1000),
        })
        self._save()
        logger.info(f"[push] registered device {push_id[:8]} ({platform})")

    def unregister(self, push_id: str) -> None:
        before = len(self._subs)
        self._subs = [s for s in self._subs if s.get("pushId") != push_id]
        if len(self._subs) != before:
            self._save()

    def list(self) -> list[dict[str, Any]]:
        return list(self._subs)


_registry: PushRegistry | None = None


def get_push_registry() -> PushRegistry:
    global _registry
    if _registry is None:
        _registry = PushRegistry()
    return _registry


def _relay_base() -> str:
    """The relay's HTTPS origin — from config ``web.relay_url`` if set (chat
    relay), else the constant. Gateway-only bots have an empty relay_url but can
    still reach the constant host for push."""
    try:
        from urllib.parse import urlparse
        from flowly.config.loader import load_config
        ru = (load_config().web.relay_url or "").strip()
        if ru:
            base = ru.replace("wss://", "https://").replace("ws://", "http://")
            p = urlparse(base)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return _DEFAULT_RELAY_BASE


def _send_one(base: str, sub: dict[str, Any], title: str, body: str,
              data: dict[str, str]) -> int:
    """Blocking POST to /api/push/send for one device. Returns the HTTP status
    (0 on a network error). Runs in a thread via ``notify_devices``."""
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "pushId": sub["pushId"],
        "title": title,
        "body": body,
        "data": data,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/push/send",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {sub.get('pushSecret', '')}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — fixed https host
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


async def notify_devices(title: str, body: str, *, conversation_id: str = "",
                         data: dict[str, str] | None = None) -> None:
    """Push ``title``/``body`` to every registered device via the relay.

    Best-effort and fire-and-forget friendly: a dead registration (401/404) is
    dropped; network errors are logged and ignored. ``conversation_id`` +
    ``gatewayId`` ride along as data for deep-linking the tap.
    """
    reg = get_push_registry()
    subs = reg.list()
    if not subs:
        return
    base = _relay_base()
    dead: list[str] = []
    for sub in subs:
        d: dict[str, str] = dict(data or {})
        ident = str(sub.get("gatewayId") or "")
        if ident:
            # Relay registrations stash the relay serverId in gatewayId; surface
            # it under the key the app uses to resolve that transport.
            key = "serverId" if sub.get("kind") == "relay" else "gatewayId"
            d.setdefault(key, ident)
        if conversation_id:
            d.setdefault("conversationId", conversation_id)
        try:
            status = await asyncio.to_thread(_send_one, base, sub, title, body, d)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[push] notify {str(sub.get('pushId',''))[:8]} error: {e}")
            continue
        if status in (401, 404):
            dead.append(sub["pushId"])
        elif status and status >= 400:
            logger.debug(f"[push] notify {sub['pushId'][:8]} → HTTP {status}")
    for pid in dead:
        reg.unregister(pid)
        logger.info(f"[push] dropped dead registration {pid[:8]}")
