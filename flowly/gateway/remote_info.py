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
    changed: {host, port, token, pin, lan_ip, public_ip, host_changed,
    token_changed} (``pin`` is "" when the gateway does not encrypt).
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

    from flowly.gateway.tls import pairing_pin

    return {
        "host": host,
        "port": int(cfg.gateway.port or 18790),
        "token": token,
        "pin": pairing_pin(cfg.gateway.tls, host),
        "lan_ip": detect_lan_ip(),
        "public_ip": detect_public_ip(),
        "host_changed": host_changed,
        "token_changed": token_changed,
    }


def connect_block(info: dict) -> tuple[list[str], bool]:
    """What an app pairs with, as Rich markup lines, and whether they end in a
    scannable code.

    ``info``: {host, port, token, pin, lan_ip, public_ip} (as
    ``enable_remote_access`` returns it). The token is in the lines, the
    link and the code: show them on a local terminal only.
    """
    from rich.markup import escape

    from flowly.gateway.remote_qr import connect_uri, remote_qr_markup
    from flowly.gateway.tls import short_fingerprint

    host = (info.get("host") or "").strip()
    port = int(info["port"])
    token = info["token"]
    pin = info.get("pin") or ""
    lan = info.get("lan_ip") or ""
    public = info.get("public_ip") or ""
    bind_all = host in ("", "0.0.0.0", "::")

    lines: list[str] = []
    if bind_all:
        # LAN first: a phone on the same Wi-Fi is the common case at home. On a
        # server the two are the same address.
        if lan:
            lines.append(f"  Address : [b]{lan}[/b]  [dim](same network)[/dim]")
        if public and public != lan:
            lines.append(
                f"  Address : [b]{public}[/b]  [dim](over the internet; at home this needs a router port-forward)[/dim]"
            )
        if not lan and not public:
            lines.append("  Address : [b]<this machine's IP>[/b]")
    else:
        lines.append(f"  Address : [b]{escape(host)}[/b]")
    lines += [f"  Port    : [b]{port}[/b]", f"  Token   : [b]{escape(token)}[/b]"]
    if pin:
        lines.append(
            f"  Key     : [b]{short_fingerprint(pin)}[/b]  [dim](connections are encrypted with this gateway's "
            "own key; the link and the code carry it, and the app shows the same)[/dim]"
        )
    else:
        lines.append(
            "  Encryption: [b]off[/b]  [dim](plain ws://: only on a network you trust, or behind an HTTPS proxy)[/dim]"
        )

    primary = (lan or public) if bind_all else host
    qr = remote_qr_markup(primary, port, token, pin=pin) if primary else None
    if primary:
        lines += [
            "",
            "  [b]Desktop[/b]: paste this link into a gateway's Address field (Settings → Gateways):",
            f"  {escape(connect_uri(primary, port, token, pin=pin))}",
        ]
    if qr:
        lines += ["", f"  [b]Phone[/b]: scan with the Flowly app [dim]({primary}:{port})[/dim]", "", qr]
    return lines, qr is not None


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
