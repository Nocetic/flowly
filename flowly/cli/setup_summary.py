"""Setup summary — a one-glance recap of what Flowly has configured.

Shown at the end of any setup flow (and on demand from the setup home). It is
deliberately a *pure, read-only snapshot*:

  * it reads the in-memory ``Config`` object the caller already loaded — never
    re-reads disk, never mutates anything;
  * channel/tool state is derived from the same ``IntegrationCard`` registry the
    setup modals use, so the summary can't drift out of sync as cards are added
    (a future FAL/media card shows up here for free);
  * gateway state can be injected (so tests don't shell out), otherwise it's
    probed locally via the service helpers.

No network, no Textual, no gateway round-trip — safe to call from any context.
The rendering is Flowly's own: a compact turquoise-accented panel that stays in
the terminal scrollback after setup exits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from flowly.config.schema import Config

# Flowly wordmark gradient — reused so the summary reads as part of the brand.
_TURQUOISE = "#19d3e6"
_TEAL = "#00a6c8"
_DEEP = "#0b7c97"
_DIM = "dim #5a7785"

# Sentinel so callers can inject ``provider=None`` (a real value) distinctly
# from "not provided, resolve it live".
_UNSET = object()


@dataclass(frozen=True)
class ItemStatus:
    """One channel/tool row: is it set up, and a short human detail."""

    label: str
    configured: bool
    detail: str = ""


@dataclass
class SetupSummary:
    """Structured snapshot of the current setup. Pure data; render separately."""

    provider_key: str | None
    provider_source: str
    model: str
    gateway_installed: bool
    gateway_running: bool
    channels: list[ItemStatus] = field(default_factory=list)
    tools: list[ItemStatus] = field(default_factory=list)
    media: list[ItemStatus] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def provider_ready(self) -> bool:
        return self.provider_key is not None

    @property
    def configured_channels(self) -> list[ItemStatus]:
        return [c for c in self.channels if c.configured]

    @property
    def configured_tools(self) -> list[ItemStatus]:
        return [t for t in self.tools if t.configured]

    @property
    def configured_media(self) -> list[ItemStatus]:
        return [m for m in self.media if m.configured]


# --------------------------------------------------------------------------
# Collection — pure, from the Config object + the card registry
# --------------------------------------------------------------------------

def _descend(config: "Config", dotted_path: str):
    """Walk ``a.b.c`` over the Config object via getattr; None if any hop misses."""
    node = config
    for part in dotted_path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node


def _field_is_set(section, field_key: str) -> bool:
    """True when a field holds a *user-provided* value, not its schema default.

    Comparing against the Pydantic schema default is what makes this robust:
    some config fields ship a non-empty default (e.g. WhatsApp ``bridge_url`` =
    ``ws://localhost:3001``), so "truthy" alone would falsely read as configured.
    """
    val = getattr(section, field_key, None)
    if not _truthy(val):
        return False
    fields = getattr(type(section), "model_fields", {})
    info = fields.get(field_key)
    default = getattr(info, "default", None) if info is not None else None
    return val != default


def _card_status(config: "Config", card) -> ItemStatus:
    """Derive a channel/tool card's configured state.

    ``configured`` = the card is enabled, OR at least one *required* field holds
    a user-provided value (distinct from its schema default). A card that has a
    credential set but its ``enabled`` toggle off is reported configured, with a
    "set · disabled" detail so the user knows it won't actually run.
    """
    section = _descend(config, card.config_path)
    if section is None:
        return ItemStatus(card.label, configured=False)

    enabled_attr = getattr(section, "enabled", None)
    required_set = any(
        _field_is_set(section, f.key) for f in card.fields if getattr(f, "required", False)
    )
    configured = enabled_attr is True or required_set

    detail = ""
    if configured and enabled_attr is False:
        detail = "set · disabled"
    return ItemStatus(card.label, configured=configured, detail=detail)


def _truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return bool(value)


def collect_summary(
    config: "Config",
    *,
    provider=_UNSET,
    gateway_installed: bool | None = None,
    gateway_running: bool | None = None,
) -> SetupSummary:
    """Build a :class:`SetupSummary` from ``config``.

    ``provider`` may be injected (an ``ActiveProvider`` or ``None``); when left
    unset it's resolved live. Note the live resolver also consults the on-disk
    Flowly account, so injecting it is what keeps unit tests hermetic.
    ``gateway_installed`` / ``gateway_running`` may likewise be injected; when
    left as ``None`` they're probed locally.
    """
    from flowly.integrations.registry import list_cards

    if provider is _UNSET:
        from flowly.integrations.active_provider import resolve_active_provider

        active = resolve_active_provider(config)
    else:
        active = provider
    provider_key = active.key if active else None
    provider_source = active.source if active else ""
    model = getattr(getattr(getattr(config, "agents", None), "defaults", None), "model", "") or ""

    if gateway_installed is None or gateway_running is None:
        inst, run = _probe_gateway(config)
        gateway_installed = inst if gateway_installed is None else gateway_installed
        gateway_running = run if gateway_running is None else gateway_running

    channels = [_card_status(config, c) for c in list_cards("channel")]
    tools = [_card_status(config, c) for c in list_cards("tool")]
    media = [_card_status(config, c) for c in list_cards("media")]

    return SetupSummary(
        provider_key=provider_key,
        provider_source=provider_source,
        model=model,
        gateway_installed=gateway_installed,
        gateway_running=gateway_running,
        channels=channels,
        tools=tools,
        media=media,
        missing=_missing_optional(config),
    )


def _missing_optional(config: "Config") -> list[str]:
    """Optional, commonly-wanted things the user hasn't set up yet (nudges)."""
    missing: list[str] = []
    tools = getattr(config, "tools", None)
    browser = getattr(getattr(tools, "browser_tab", None), "enabled", None)
    if browser is not True:
        missing.append("Browser extension")
    if not getattr(config, "mcp_servers", None):
        missing.append("MCP servers")
    return missing


def _probe_gateway(config: "Config") -> tuple[bool, bool]:
    """(installed, running) — local, read-only. Never raises."""
    try:
        from flowly.cli.service_cmd import (
            DEFAULT_SERVICE_LABEL,
            _port_listener_pids,
            _service_paths,
        )

        mac, linux, win = _service_paths(DEFAULT_SERVICE_LABEL)
        installed = any(p is not None and p.exists() for p in (mac, linux, win))
        port = int(getattr(getattr(config, "gateway", None), "port", 0) or 18790)
        running = len(_port_listener_pids(port)) > 0
        return installed, running
    except Exception:
        return False, False


# --------------------------------------------------------------------------
# Rendering — Flowly's own panel
# --------------------------------------------------------------------------

def _row(label: str, value: Text) -> Text:
    line = Text()
    line.append(f"  {label:<13}", style=_DIM)
    line.append_text(value)
    return line


def _glyph(ok: bool) -> Text:
    return Text("✓ ", style=f"bold {_TURQUOISE}") if ok else Text("· ", style=_DIM)


def _item_line(items: list[ItemStatus]) -> Text:
    """A compact ' Name ✓ · Other –' inline list for channels/tools."""
    out = Text()
    shown = [i for i in items if i.configured]
    if not shown:
        out.append("— none yet", style=_DIM)
        return out
    for idx, it in enumerate(shown):
        if idx:
            out.append(" · ", style=_DIM)
        out.append(it.label, style=_TEAL)
        out.append(" ✓", style=f"bold {_TURQUOISE}")
        if it.detail:
            out.append(f" ({it.detail})", style=_DIM)
    return out


def render_summary(summary: SetupSummary, console: Console | None = None) -> None:
    """Print the summary panel to the terminal (stays in scrollback)."""
    console = console or Console()
    rows: list[Text] = []

    # Provider
    if summary.provider_ready:
        val = Text()
        val.append("✓ ", style=f"bold {_TURQUOISE}")
        val.append(summary.provider_key, style="bold")
        if summary.model:
            val.append(f"  ({summary.model})", style=_DIM)
    else:
        val = Text("○ not set — pick one to start", style="yellow")
    rows.append(_row("Provider", val))

    # Gateway
    if summary.gateway_running:
        gw = Text("● running", style=f"bold {_TURQUOISE}")
    elif summary.gateway_installed:
        gw = Text("◐ installed · not running", style="yellow")
    else:
        gw = Text("○ not installed", style=_DIM)
    rows.append(_row("Gateway", gw))

    rows.append(_row("Channels", _item_line(summary.channels)))
    rows.append(_row("Integrations", _item_line(summary.tools)))
    rows.append(_row("Media", _item_line(summary.media)))

    if summary.missing:
        rows.append(_row("Missing", Text(" · ".join(summary.missing), style=_DIM)))

    # Next commands — Flowly's own short list, gated on state.
    nxt = Text("\n")
    nxt.append("  Next\n", style=f"bold {_DEEP}")
    for cmd, desc in _next_commands(summary):
        nxt.append(f"    {cmd:<32}", style=_TURQUOISE)
        nxt.append(f"{desc}\n", style=_DIM)

    body = Group(*rows, nxt)
    title = Text("Flowly · setup", style=f"bold {_TEAL}")
    console.print(Panel(body, title=title, title_align="left", border_style=_DEEP, padding=(1, 2)))


def _next_commands(summary: SetupSummary) -> list[tuple[str, str]]:
    """The handful of next steps that actually make sense given current state."""
    cmds: list[tuple[str, str]] = []
    if not summary.provider_ready:
        cmds.append(("flowly setup", "choose an account or API key"))
        return cmds
    if not summary.gateway_installed:
        cmds.append(("flowly service install --start", "run the gateway in the background"))
    cmds.append(("flowly", "start chatting"))
    cmds.append(("flowly memory import-prompt", "bring memories from ChatGPT/Gemini"))
    if not summary.configured_tools:
        cmds.append(("flowly setup tools", "add integrations"))
    if "MCP servers" in summary.missing:
        cmds.append(("flowly mcp picker", "browse MCP servers"))
    return cmds
