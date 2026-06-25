"""Welcome screen — Flowly wordmark + session panel + quick tips."""

from __future__ import annotations

from rich.align import Align
from rich.columns import Columns
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from flowly.tui.theme import FlowlyPalette

# Block-char "FLOWLY" wordmark — 6 rows × 50 cols.
LOGO_ART = [
    " ███████╗██╗      ██████╗ ██╗    ██╗██╗  ██╗   ██╗",
    " ██╔════╝██║     ██╔═══██╗██║    ██║██║  ╚██╗ ██╔╝",
    " █████╗  ██║     ██║   ██║██║ █╗ ██║██║   ╚████╔╝ ",
    " ██╔══╝  ██║     ██║   ██║██║███╗██║██║    ╚██╔╝  ",
    " ██║     ███████╗╚██████╔╝╚███╔███╔╝███████╗██║   ",
    " ╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝ ╚══════╝╚═╝   ",
]
# 4-color gradient mapping (row idx → palette idx)
LOGO_GRADIENT = [0, 0, 1, 1, 2, 2]

# Compact spark icon shown above tagline.
SPARK_ICON = "✦"

# Fallback "small" logo for narrow terminals (< 56 cols).
LOGO_SMALL = f"{SPARK_ICON}  FLOWLY"


def _logo_renderable(palette: FlowlyPalette, width: int) -> Group:
    """Return colored ASCII logo, or compact text if terminal is too narrow."""
    if width < 56:
        return Group(Text(LOGO_SMALL, style=f"bold {palette.accent}"))
    colors = [palette.user, palette.assistant, palette.accent, palette.text_muted]
    lines = []
    for row, art in enumerate(LOGO_ART):
        color = colors[LOGO_GRADIENT[row]]
        lines.append(Text(art, style=color))
    return Group(*lines)


def _info_panel(
    session_key: str,
    model: str,
    palette: FlowlyPalette,
    *,
    gateway_url: str = "ws://127.0.0.1:18790",
    ios_paired: bool = False,
    provider: str = "none",
    provider_source: str = "",
) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(style=palette.text_muted)
    t.add_column(style=palette.text)
    t.add_row("session", session_key)
    t.add_row("provider", provider or "none")
    if provider_source:
        t.add_row("source", f"[{palette.text_muted}]{provider_source}[/]")
    t.add_row("model", model or "gateway default")
    # Sync row — distinguishes pairing state from chat persistence so the
    # user knows where this turn's messages actually land. CLI sessions
    # never write to Firestore; iOS/desktop/Android chats do (via relay)
    # when /login is active.
    if ios_paired:
        sync_text = (
            f"[{palette.assistant}]paired[/]  iOS/desktop chats → relay (cloud)\n"
            f"[{palette.text_muted}]this CLI session → local only[/]"
        )
    else:
        sync_text = (
            f"[{palette.text_muted}]local only[/]\n"
            f"[{palette.text_muted}]/login to sync iOS/desktop chats[/]"
        )
    t.add_row("sync", sync_text)
    t.add_row("gateway", f"[{palette.text_muted}]{gateway_url}[/]")
    return Panel(
        t,
        title=f"[{palette.accent}]· workspace ·[/]",
        title_align="left",
        border_style=palette.border,
        padding=(0, 1),
    )


def _command_strip(palette: FlowlyPalette) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(style=palette.accent, no_wrap=True)
    t.add_column(style=palette.text)
    t.add_row("/", "commands")
    t.add_row("/provider", "choose LLM")
    t.add_row("/channels", "connect inboxes")
    t.add_row("/browser", "use Chrome")
    t.add_row("F1", "help")
    return Panel(
        t,
        title=f"[{palette.accent}]· controls ·[/]",
        title_align="left",
        border_style=palette.border,
        padding=(0, 1),
    )


def build_welcome(
    session_key: str,
    model: str,
    palette: FlowlyPalette,
    *,
    width: int = 100,
    gateway_url: str = "ws://127.0.0.1:18790",
    ios_paired: bool = False,
    provider: str = "none",
    provider_source: str = "",
) -> Group:
    """Compose the full welcome screen as a single Rich Group renderable."""
    logo = _logo_renderable(palette, width)
    tagline = Text(
        f"{SPARK_ICON}  from intent to action  {SPARK_ICON}",
        style=f"italic {palette.text_muted}",
        justify="center",
    )
    info = _info_panel(
        session_key, model, palette,
        gateway_url=gateway_url, ios_paired=ios_paired,
        provider=provider, provider_source=provider_source,
    )
    controls = _command_strip(palette)
    panels = Columns(
        [info, controls],
        expand=True,
        equal=True,
        padding=(0, 2),
    )

    # Footer doubles as a discoverability hint — F1 + /help are
    # otherwise easy to miss; surfacing them where the user is about
    # to start typing means they have at least one visible escape
    # hatch before they're lost in the chat surface.
    footer = Text(
        "type a message to begin  ·  F1 for help  ·  /help for commands",
        style=f"italic {palette.text_muted}",
        justify="center",
    )

    return Group(
        Align.center(logo),
        Padding(tagline, (1, 0, 0, 0)),
        Padding(Align.center(panels), (1, 0, 1, 0)),
        footer,
    )
