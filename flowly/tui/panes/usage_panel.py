"""Inline /usage panel — token & cost for the active provider, plus Flowly
account credits when signed in.

Unlike a ModalScreen (a separate full-screen overlay), this mounts INTO the
composer and renders in place of the input row — a true inline panel, like the
approval / setup prompts. Esc closes it and returns to the input.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

_PINK = "#ff6ea6"   # section headers
_CYAN = "#35d5ef"
_MUTE = "#83b8c2"


def _human(n: float) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _bar(used: int, budget: int, width: int = 24) -> str:
    if not budget:
        return "░" * width
    ratio = max(0.0, min(1.0, used / budget))
    filled = int(ratio * width)
    if used > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def _dur(secs: float) -> str:
    s = int(secs)
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def _fmt_credits(n: object) -> str:
    try:
        v = float(n)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    if v % 1:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{int(v):,}"


def _credit_lines(credits: dict | None) -> list[str]:
    """Render the signed-in account's live credit balance — the same
    ``/api/billing/credits`` payload Desktop shows. Any missing field is skipped
    rather than rendered as a wrong zero."""
    info = credits or {}
    c = info.get("credits") if isinstance(info.get("credits"), dict) else None
    if not c:
        return ["  [dim]Account balance unavailable right now "
                "(offline or not on a credit plan).[/]"]
    lines: list[str] = []
    remaining, total = c.get("remaining"), c.get("total")
    pct = c.get("percentUsed")
    if remaining is not None and total is not None:
        tail = f"  [dim]({pct:.0f}% used)[/]" if isinstance(pct, (int, float)) else ""
        lines.append(
            f"  Credits:  [b]{_fmt_credits(remaining)}[/] / "
            f"{_fmt_credits(total)} left{tail}"
        )
    elif c.get("available") is not None:
        lines.append(f"  Credits:  [b]{_fmt_credits(c['available'])}[/] available")
    bonus = c.get("bonus")
    bonus_rem = bonus.get("remaining") if isinstance(bonus, dict) else None
    if bonus_rem:
        lines.append(f"  Bonus:    {_fmt_credits(bonus_rem)}")
    plan = info.get("plan")
    meta: list[str] = []
    if isinstance(plan, dict) and plan.get("id"):
        meta.append(f"plan {plan['id']}")
    if c.get("periodEnd"):
        meta.append(f"renews {str(c['periodEnd'])[:10]}")
    if meta:
        lines.append(f"  [dim]{' · '.join(meta)}[/]")
    return lines or ["  [dim]Account balance unavailable right now.[/]"]


def build_usage_body(
    *,
    totals: dict[str, float],
    model: str,
    provider: str,
    ctx_used: int,
    ctx_budget: int,
    elapsed: float,
    account_email: str | None,
    credits: dict | None = None,
) -> str:
    """Rich-markup body for the /usage panel."""
    t = totals
    L: list[str] = []

    # ── Session ────────────────────────────────────────────────────
    L.append(f"[b {_PINK}]Session[/]")
    if t.get("cost_known"):
        cost = f"[b]${t['cost_usd']:.4f}[/]"
    else:
        cost = "[dim]n/a — no catalog price for this model[/]"
    L.append(f"  Total cost:   {cost}")
    L.append(f"  Duration:     {_dur(elapsed)}")
    L.append(f"  Turns:        {int(t['turns'])}")
    L.append(f"  Tokens:       {_human(t['input'])} input · {_human(t['output'])} output")
    L.append(
        f"                {_human(t['cache_read'])} cache read · "
        f"{_human(t['cache_write'])} cache write"
    )
    L.append("")

    # ── Context window (current turn) ──────────────────────────────
    L.append(f"[b {_PINK}]Context window[/]  [dim](current turn)[/]")
    if ctx_budget:
        pct = ctx_used / ctx_budget * 100
        L.append(
            f"  [{_CYAN}]{_bar(ctx_used, ctx_budget)}[/]  [b]{pct:.0f}%[/]  "
            f"[dim]{_human(ctx_used)} / {_human(ctx_budget)} tokens[/]"
        )
    else:
        L.append("  [dim]context window unknown for this model[/]")
    L.append(f"  [dim]◈ {provider or '?'} · {model or '?'}[/]")
    L.append("")

    # ── Flowly account ─────────────────────────────────────────────
    L.append(f"[b {_PINK}]Flowly account[/]")
    if account_email:
        L.append(f"  Signed in as [b]{account_email}[/]")
        L.extend(_credit_lines(credits))
    else:
        L.append("  [dim]Not signed in — /login to link a Flowly account.[/]")
    L.append(f"  [{_MUTE}]The tokens & cost above are your own, tallied locally.[/]")
    L.append("")
    L.append(
        "[dim]Cost is an estimate from the OpenRouter catalog price for the "
        "active model (cached tokens billed at full rate).[/]"
    )
    return "\n".join(L)


class UsagePanel(Vertical):
    """Inline /usage panel above the composer input (not a modal overlay)."""

    can_focus = True

    class Dismissed(Message):
        """Esc/q pressed — the app should close the panel."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="usage-scroll"):
            yield Static("", id="usage-body", markup=True)
        yield Static("[dim]Esc to close · ↑/↓ scroll[/]", id="usage-hint", markup=True)

    def set_data(self, **kwargs: object) -> None:
        body = build_usage_body(**kwargs)  # type: ignore[arg-type]
        self.query_one("#usage-body", Static).update(Text.from_markup(body))
        try:
            self.query_one("#usage-scroll", VerticalScroll).scroll_home(animate=False)
        except Exception:
            pass
        self.focus()

    def clear(self) -> None:
        try:
            self.query_one("#usage-body", Static).update("")
        except Exception:
            pass

    def on_key(self, event: events.Key) -> None:
        if event.key in ("escape", "q"):
            event.stop()
            event.prevent_default()
            self.post_message(self.Dismissed())
            return
        scroll = None
        try:
            scroll = self.query_one("#usage-scroll", VerticalScroll)
        except Exception:
            return
        if event.key in ("up", "k"):
            scroll.scroll_up(animate=False)
        elif event.key in ("down", "j"):
            scroll.scroll_down(animate=False)
        elif event.key == "pageup":
            scroll.scroll_page_up(animate=False)
        elif event.key == "pagedown":
            scroll.scroll_page_down(animate=False)
        else:
            return
        event.stop()
        event.prevent_default()
