"""Authenticated POST to the local gateway's ``/api/provider/reload``.

Every TUI surface that hot-applies a provider/model change (model picker,
provider picker, integration setup, xAI login) used to hard-code
``http://127.0.0.1:18790`` with NO auth header. That silently broke the moment
the gateway had a static token configured — which ``flowly service install
--host 0.0.0.0`` does automatically — because the auth middleware then gates
every REST route and the bare POST gets a 401: the model "switched" in config
but the running agent kept the old one until a restart.

This helper is the single place that knows how to reach the gateway's REST
plane: config-derived host/port (``0.0.0.0`` normalised to loopback) and the
configured token as ``X-Flowly-Token``.
"""
from __future__ import annotations

import httpx


def _gateway_origin_and_headers() -> tuple[str, dict[str, str]]:
    host, port, token = "127.0.0.1", 18790, ""
    try:
        from flowly.config.loader import load_config
        gw = load_config().gateway
        host = (gw.host or "127.0.0.1").strip() or "127.0.0.1"
        port = int(gw.port or 18790)
        token = (gw.token or "").strip()
    except Exception:  # noqa: BLE001
        pass
    # The gateway may BIND 0.0.0.0 (all interfaces) but we reach it locally.
    if host in ("0.0.0.0", "::", "::0"):
        host = "127.0.0.1"
    headers = {"X-Flowly-Token": token} if token else {}
    return f"http://{host}:{port}", headers


async def post_provider_reload(timeout: float = 8.0) -> httpx.Response:
    """POST /api/provider/reload with auth. Returns the raw response so each
    caller keeps its own 200/422/other footer wording; connection errors
    propagate (callers map them to their "gateway offline" strings)."""
    origin, headers = _gateway_origin_and_headers()
    async with httpx.AsyncClient(timeout=timeout) as c:
        return await c.post(f"{origin}/api/provider/reload", headers=headers)
