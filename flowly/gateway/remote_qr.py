"""Remote-access QR — encode the gateway connection block so a phone can scan
it instead of typing host / port / token by hand.

Companion to :func:`flowly.gateway.remote_info.enable_remote_access`: it carries
exactly the same four values (host, port, token, TLS) as a ``flowly://remote``
URI, rendered as a QR the user points the Flowly app's camera at. Manual entry
stays the documented fallback, so the QR is never load-bearing — if anything
here returns ``None`` (e.g. ``segno`` missing, or no host to encode) the caller
just shows the typed block.

Rendering: half-block glyphs wrapped per line in a ``black on white`` Rich style.
That paints a real dark-on-light code regardless of the terminal's theme, and
the *same* markup string renders in both the CLI (``console.print``) and the
TUI transcript bubble. The quiet zone (a border of light modules) gives the
code its own white frame, so it still scans inside a dark bubble.

SECRET: the URI embeds the token. Like the printed block, only ever surface
this on a local terminal (CLI / TUI) — never through a chat channel.
"""
from __future__ import annotations

from urllib.parse import urlencode

# Each text cell stacks two vertical QR modules (top, bottom). Under a
# ``black on white`` style the glyph paints black and the cell background white,
# so the half a glyph covers maps straight onto which module is dark.
_GLYPH = {
    (1, 1): "█",  # █ full block  — both dark
    (1, 0): "▀",  # ▀ upper half  — top dark, bottom light
    (0, 1): "▄",  # ▄ lower half  — top light, bottom dark
    (0, 0): " ",       #   space       — both light
}


def connect_uri(host: str, port: int, token: str, *, tls: bool = False) -> str:
    """Build the ``flowly://remote`` URI the app parses into its add-server form.

    Carries the same fields the user would type by hand; the app fills host /
    port / token / TLS from the query string.
    """
    query = urlencode(
        {
            "host": host,
            "port": int(port),
            "token": token,
            "tls": "1" if tls else "0",
        }
    )
    return f"flowly://remote?{query}"


def qr_markup(data: str, *, border: int = 4) -> str | None:
    """Render ``data`` as Rich-markup half-block QR, or ``None`` when QR support
    is unavailable (``segno`` not importable) — callers fall back to the typed
    block rather than crashing the ``/remote`` flow.
    """
    try:
        import segno
    except Exception:  # noqa: BLE001 — degrade to manual-only, never break /remote
        return None

    qr = segno.make(data, error="m")
    rows = [[1 if m else 0 for m in row] for row in qr.matrix_iter(border=border)]
    if not rows:
        return None
    width = len(rows[0])
    blank = [0] * width

    lines: list[str] = []
    for i in range(0, len(rows), 2):
        top = rows[i]
        bottom = rows[i + 1] if i + 1 < len(rows) else blank
        cells = "".join(_GLYPH[(top[c], bottom[c])] for c in range(width))
        # One self-contained style span per line — no cross-newline markup state.
        lines.append(f"[black on white]{cells}[/]")
    return "\n".join(lines)


def remote_qr_markup(
    host: str,
    port: int,
    token: str,
    *,
    tls: bool = False,
    border: int = 4,
) -> str | None:
    """Connection values → scannable QR markup, or ``None`` when there's nothing
    safe to encode (missing host/token) or QR support is unavailable."""
    if not host or not token:
        return None
    return qr_markup(connect_uri(host, port, token, tls=tls), border=border)
