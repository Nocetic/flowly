"""Usage modal (/usage) — session token & cost breakdown.

Shows the user THEIR OWN usage — cumulative tokens (input/output/cache), an
estimated cost from the model-catalog price, session duration, and the current
context-window occupancy — for whatever provider is active, local or remote.
Cost/credits from a Flowly account live in the Desktop/app; here we always show
the local, provider-agnostic numbers plus the sign-in state.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

_PINK = "#ff6ea6"   # section headers (echoes the reference layout)
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


class UsageModal(ModalScreen[None]):
    # Positioning, border, and background come from the shared ModalScreen
    # runtime CSS (theme.py) so /usage is a themed bottom sheet like every other
    # modal. Here we only size the sheet.
    DEFAULT_CSS = """
    UsageModal > Vertical {
        width: 90%;
        max-width: 84;
        height: auto;
        max-height: 90%;
    }
    UsageModal VerticalScroll {
        padding: 1 2;
        height: auto;
    }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
        ("q", "dismiss(None)", "Close"),
    ]

    def __init__(
        self,
        *,
        totals: dict[str, float],
        model: str,
        provider: str,
        ctx_used: int,
        ctx_budget: int,
        elapsed: float,
        account_email: str | None,
        credits: dict | None = None,
    ) -> None:
        super().__init__()
        self._totals = totals
        self._model = model
        self._provider = provider
        self._ctx_used = ctx_used
        self._ctx_budget = ctx_budget
        self._elapsed = elapsed
        self._account_email = account_email
        self._credits = credits

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll():
                yield Static(Text.from_markup(self._body()))

    @staticmethod
    def _fmt_credits(n: object) -> str:
        try:
            v = float(n)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "?"
        if v % 1:
            return f"{v:,.2f}".rstrip("0").rstrip(".")
        return f"{int(v):,}"

    def _credit_lines(self) -> list[str]:
        """Render the signed-in account's live credit balance — the same
        ``/api/billing/credits`` payload Desktop shows. Defensive: any missing
        field is skipped rather than rendered as a wrong zero."""
        info = self._credits or {}
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
                f"  Credits:  [b]{self._fmt_credits(remaining)}[/] / "
                f"{self._fmt_credits(total)} left{tail}"
            )
        elif c.get("available") is not None:
            lines.append(f"  Credits:  [b]{self._fmt_credits(c['available'])}[/] available")
        bonus = c.get("bonus")
        bonus_rem = bonus.get("remaining") if isinstance(bonus, dict) else None
        if bonus_rem:
            lines.append(f"  Bonus:    {self._fmt_credits(bonus_rem)}")
        plan = info.get("plan")
        meta: list[str] = []
        if isinstance(plan, dict) and plan.get("id"):
            meta.append(f"plan {plan['id']}")
        if c.get("periodEnd"):
            meta.append(f"renews {str(c['periodEnd'])[:10]}")
        if meta:
            lines.append(f"  [dim]{' · '.join(meta)}[/]")
        return lines or ["  [dim]Account balance unavailable right now.[/]"]

    def _body(self) -> str:
        t = self._totals
        L: list[str] = [f"[b {_CYAN}]Usage[/]", ""]

        # ── Session ────────────────────────────────────────────────
        L.append(f"[b {_PINK}]Session[/]")
        if t.get("cost_known"):
            cost = f"[b]${t['cost_usd']:.4f}[/]"
        else:
            cost = "[dim]n/a — no catalog price for this model[/]"
        L.append(f"  Total cost:   {cost}")
        L.append(f"  Duration:     {_dur(self._elapsed)}")
        L.append(f"  Turns:        {int(t['turns'])}")
        L.append(
            f"  Tokens:       {_human(t['input'])} input · {_human(t['output'])} output"
        )
        L.append(
            f"                {_human(t['cache_read'])} cache read · "
            f"{_human(t['cache_write'])} cache write"
        )
        L.append("")

        # ── Context window (current turn) ──────────────────────────
        L.append(f"[b {_PINK}]Context window[/]  [dim](current turn)[/]")
        if self._ctx_budget:
            pct = self._ctx_used / self._ctx_budget * 100
            L.append(
                f"  [{_CYAN}]{_bar(self._ctx_used, self._ctx_budget)}[/]  "
                f"[b]{pct:.0f}%[/]  "
                f"[dim]{_human(self._ctx_used)} / {_human(self._ctx_budget)} tokens[/]"
            )
        else:
            L.append("  [dim]context window unknown for this model[/]")
        L.append(f"  [dim]◈ {self._provider or '?'} · {self._model or '?'}[/]")
        L.append("")

        # ── Flowly account ─────────────────────────────────────────
        L.append(f"[b {_PINK}]Flowly account[/]")
        if self._account_email:
            L.append(f"  Signed in as [b]{self._account_email}[/]")
            L.extend(self._credit_lines())
        else:
            L.append("  [dim]Not signed in — /login to link a Flowly account.[/]")
        L.append(
            f"  [{_MUTE}]The tokens & cost above are your own, "
            f"tallied locally on this machine.[/]"
        )
        L.append("")

        L.append(
            "[dim]Cost is an estimate from the OpenRouter catalog price for the "
            "active model (cached tokens billed at full rate). Esc to close.[/]"
        )
        return "\n".join(L)
