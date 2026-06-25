"""Remote-access enable/disable/status — shared by the TUI ``/remote`` command.

"Remote access" = the gateway binding a non-loopback host so the desktop app
can connect directly over IP+port+token (Settings → Connections). The CLI
flags (``flowly gateway --host`` / ``flowly service install --host``) already
do this; these helpers give interactive surfaces the same behaviour with one
call: flip ``gateway.host``, make sure a token exists (a non-loopback bind
without auth would expose the bot), and report what the user must type into
the desktop.

Note for callers: the printed token is a SECRET — only surface it on a local
terminal (TUI transcript), never through a chat channel.
"""
from __future__ import annotations

import urllib.request


def detect_public_ip() -> str:
    """Best-effort public IP (VPS sits behind cloud NAT, so the socket can't
    tell us). Short timeout; '' on any failure — callers print a hint instead."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 — fixed https hosts
                ip = resp.read().decode("utf-8", errors="replace").strip()
            if ip and len(ip) <= 64:
                return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


def detect_lan_ip() -> str:
    """Best-effort LAN IP of this machine (e.g. 192.168.x.x).

    This is the address a phone/desktop on the SAME network uses — the right
    one for the common "iOS app on the same Wi-Fi" case, where the public IP
    (router WAN) isn't reachable without port-forwarding. The UDP-connect trick
    reads the local socket address the OS would route through; no packets are
    actually sent. '' on failure.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip if ip and not ip.startswith("127.") else ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        s.close()


def remote_access_status() -> dict:
    """Current persisted state: {enabled, host, port, has_token, token}."""
    from flowly.config.loader import load_config
    from flowly.gateway.auth import is_loopback_host

    cfg = load_config()
    host = (cfg.gateway.host or "127.0.0.1").strip() or "127.0.0.1"
    token = (cfg.gateway.token or "").strip()
    return {
        "enabled": not is_loopback_host(host),
        "host": host,
        "port": int(cfg.gateway.port or 18790),
        "has_token": bool(token),
        "token": token,
    }


def enable_remote_access() -> dict:
    """Persist ``gateway.host=0.0.0.0`` (when currently loopback) and make sure
    a remote-access token exists (generated + persisted when missing — a
    non-loopback bind must never run open).

    Returns the connection info the user types into the desktop plus what
    changed: {host, port, token, public_ip, host_changed, token_changed}.
    A restart of the gateway/service is required when anything changed —
    a live process can't rebind.
    """
    from flowly.config.loader import load_config, save_config
    from flowly.gateway.auth import generate_gateway_token, is_loopback_host

    cfg = load_config()
    host = (cfg.gateway.host or "127.0.0.1").strip() or "127.0.0.1"
    token = (cfg.gateway.token or "").strip()

    host_changed = False
    if is_loopback_host(host):
        cfg.gateway.host = "0.0.0.0"
        host = "0.0.0.0"
        host_changed = True

    token_changed = False
    if not token:
        token = generate_gateway_token()
        cfg.gateway.token = token
        token_changed = True

    if host_changed or token_changed:
        save_config(cfg)

    return {
        "host": host,
        "port": int(cfg.gateway.port or 18790),
        "token": token,
        "lan_ip": detect_lan_ip(),
        "public_ip": detect_public_ip(),
        "host_changed": host_changed,
        "token_changed": token_changed,
    }


def disable_remote_access() -> dict:
    """Persist ``gateway.host=127.0.0.1`` (local-only). The token is KEPT so
    re-enabling later doesn't invalidate already-configured desktops.
    Returns {changed}."""
    from flowly.config.loader import load_config, save_config
    from flowly.gateway.auth import is_loopback_host

    cfg = load_config()
    host = (cfg.gateway.host or "127.0.0.1").strip() or "127.0.0.1"
    if is_loopback_host(host):
        return {"changed": False}
    cfg.gateway.host = "127.0.0.1"
    save_config(cfg)
    return {"changed": True}
