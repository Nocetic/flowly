"""Status bar — FaceTicker pattern with isolated segment ticks.

Layout (horizontal, 1 row):
    [FaceTicker] │ [ModelLabel] │ [ElapsedClock] │ [Badges]

The context-window token bar is rendered in ContextHeader's right slot
so it sits beside Textual's normal title/subtitle session display.

Each segment is its own widget with its own (or no) timer. Only the
animated segment re-renders per tick — model/tokens stay put — which
eliminates the every-second jitter you'd get by storing all state on
a single Static.
"""

from __future__ import annotations

import random
import time

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Header, Static
from textual.widgets._header import HeaderTitle

SPINNER_TICK_MS = 100        # 10 fps glyph rotation
ELAPSED_TICK_MS = 1000       # clock segment refresh cadence

# Verb + face are picked ONCE per busy entry (random.choice on each
# turn boundary, then held stable for the duration). Rotating them on
# a fast timer made the status line feel epileptic. Now: spinner glyph
# animates, label sits still until the next turn.

BUSY_VERBS = ["thinking", "running", "processing", "weaving", "stirring"]
BUSY_FACES: dict[str, list[str]] = {
    "thinking":   ["(´･ω･`)", "(´- ω -`)", "(•́ω•̀)", "(ʘ‿ʘ)"],
    "running":    ["ᕦ(ò_óˇ)ᕤ", "ᕙ(`▽´)ᕗ", "ᕦ(•̀_•́)ᕤ", "ᕙ(⇀‸↼‶)ᕗ"],
    "processing": ["( ͡° ͜ʖ ͡°)", "( ಠ_ಠ )", "(¬‿¬)", "( ◉_◉)"],
    "weaving":    ["(づ｡◕‿‿◕｡)づ", "✧(>o<)ノ✧", "(◕‿◕✿)", "(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧"],
    "stirring":   ["(づ￣ ³￣)づ", "(˘▾˘)~", "(～￣▽￣)~", "(¯﹃¯)"],
}
BUSY_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

DEFAULT_BUDGET_TOKENS = 200_000


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _progress_bar(used: int, budget: int, width: int = 10) -> tuple[str, int | float]:
    """Render a width-cell bar and return (bar, pct).

    For huge contexts (1M tokens) a 10-cell bar would show 0 filled
    cells until usage crosses 10% — that's ~100k tokens, by which point
    the user can't tell if the indicator works. Two adjustments:

    1. Any non-zero usage gets at least 1 filled cell so the bar visibly
       responds the moment the first turn lands.
    2. Sub-percent usage shows fractional pct ("0.3%" instead of "0%")
       so the indicator still moves on big-context models.
    """
    if not budget:
        return ("░" * width, 0)
    ratio = used / budget
    pct_raw = ratio * 100
    if pct_raw >= 1:
        pct: int | float = int(round(pct_raw))
    else:
        pct = round(pct_raw, 1)
    filled = max(0, min(width, int(ratio * width)))
    if used > 0 and filled == 0:
        filled = 1  # always show *some* progress on real usage
    return ("█" * filled + "░" * (width - filled), pct)


def _model_budget(model: str) -> int:
    """Resolve the context-window budget for a model id.

    Priority order:
      1. **Live catalog** — when the OpenRouter / Flowly-proxy catalog is
         already cached (warmed at TUI startup), use the provider's own
         ``context_length`` field. Works for the entire ~262 OpenRouter
         catalog without us baking in magic numbers per family.
      2. **Hardcoded fallback** — the catalog isn't always cached (no
         network yet, BYOK provider w/o fetcher, custom model id). The
         family heuristics below stay so the bar isn't blank on cold
         start.
      3. **Default 200k** — last resort so the bar still renders.
    """
    try:
        from flowly.integrations.model_catalog import get_context_window
        live = get_context_window(model)
        if live:
            return live
    except Exception:
        pass
    m = (model or "").lower()
    if "kimi" in m:
        return 262_144
    if "gemini" in m:
        return 1_000_000
    if "gpt-4o" in m or "gpt-4-turbo" in m:
        return 128_000
    if any(x in m for x in ("claude-haiku-4", "claude-sonnet-4",
                            "claude-opus-4", "sonnet-4", "haiku-4", "opus-4")):
        return 200_000
    return DEFAULT_BUDGET_TOKENS


# ── Segment widgets ────────────────────────────────────────────────


class _FaceTicker(Static):
    """Animated spinner + kaomoji face + turn-elapsed seconds.

    Only this segment ticks at SPINNER_TICK_MS — when the rest of the
    status bar stays static, no other widget re-renders.
    """

    DEFAULT_CSS = "_FaceTicker { width: auto; height: 1; }"

    state: reactive[str] = reactive("idle", layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=True)
        self._frame = 0
        # Verb + face are chosen once per busy entry and held stable.
        # Empty strings until the first turn lands.
        self._verb = ""
        self._face = ""
        self._turn_start: float | None = None
        self._glyph_timer = None
        self._clock_timer = None

    def on_mount(self) -> None:
        self._glyph_timer = self.set_interval(SPINNER_TICK_MS / 1000, self._tick_glyph)
        self._clock_timer = self.set_interval(ELAPSED_TICK_MS / 1000, self._tick_clock)
        self._refresh()

    def watch_state(self, _old: str, new: str) -> None:
        if new == "busy" and self._turn_start is None:
            self._turn_start = time.monotonic()
            # Pick verb + face once per turn via random.choice.
            # Choosing the face from the verb's specific set keeps the
            # personality matched ("processing ( ͡° ͜ʖ ͡°)" not
            # "processing (>o<)ノ✧").
            self._verb = random.choice(BUSY_VERBS)
            faces = BUSY_FACES.get(self._verb) or [""]
            self._face = random.choice(faces)
        elif new != "busy":
            self._turn_start = None
        self._refresh()

    def _tick_glyph(self) -> None:
        if self.state in ("busy", "reconnecting"):
            self._frame = (self._frame + 1) % len(BUSY_FRAMES)
            self._refresh()

    def _tick_clock(self) -> None:
        if self._turn_start is not None:
            self._refresh()

    def _refresh(self) -> None:
        if self.state == "busy":
            spin = BUSY_FRAMES[self._frame]
            elapsed = ""
            if self._turn_start is not None:
                elapsed = f" · ⏲ {int(time.monotonic() - self._turn_start)}s"
            self.update(
                f"[#f2c94c]{spin}[/] [b]{self._verb}[/b]{elapsed}"
            )
        elif self.state == "reconnecting":
            spin = BUSY_FRAMES[self._frame]
            self.update(f"[#f2c94c]{spin}[/] [b]reconnecting[/b]")
        elif self.state == "offline":
            self.update("[#ff5d6c]○[/] offline")
        elif self.state == "error":
            self.update("[#ff5d6c]●[/] error")
        else:
            self.update("")


class _Sep(Static):
    DEFAULT_CSS = "_Sep { width: 3; height: 1; color: #0f4c5c; }"
    def __init__(self) -> None:
        super().__init__("│", markup=False)


class _ModelLabel(Static):
    DEFAULT_CSS = "_ModelLabel { width: auto; height: 1; color: #35d5ef; }"

    model: reactive[str] = reactive("", layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=False)

    def watch_model(self, _old: str, new: str) -> None:
        m = new.split("/")[-1] if new else ""
        if len(m) > 22:
            m = "…" + m[-21:]
        self.update(m)


class _ProviderLabel(Static):
    """Resolved LLM provider that will serve the next request."""

    DEFAULT_CSS = "_ProviderLabel { width: auto; height: 1; color: #00a6c8; }"

    provider: reactive[str] = reactive("", layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=True)

    def watch_provider(self, _old: str, new: str) -> None:
        name = (new or "").strip()
        if not name:
            self.update("[#ff5d6c]◈ ?[/]")
            return
        if len(name) > 18:
            name = "…" + name[-17:]
        self.update(f"[dim]◈[/] [b]{name}[/b]")


class _TokenBar(Static):
    DEFAULT_CSS = "_TokenBar { width: auto; height: 1; }"

    tokens_in:  reactive[int] = reactive(0, layout=False)
    tokens_out: reactive[int] = reactive(0, layout=False)
    model:      reactive[str] = reactive("", layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=True)

    def watch_tokens_in(self) -> None: self._refresh()
    def watch_tokens_out(self) -> None: self._refresh()
    def watch_model(self) -> None: self._refresh()

    def _refresh(self) -> None:
        # Always render the bar — previously we hid it until the first
        # token landed, which left the status line eerily empty on a
        # fresh chat. The bar now shows "0/1M ░░░░ 0%" up front so the
        # user knows how much context their current model has.
        used = self.tokens_in + self.tokens_out
        budget = _model_budget(self.model)
        if not budget:
            self.update("")
            return
        bar, pct = _progress_bar(used, budget)
        self.update(
            f"[dim]{_human(used)}/{_human(budget)}[/] "
            f"[#35d5ef]{bar}[/] [dim]{pct}%[/]"
        )


class _HeaderTokenBar(_TokenBar):
    DEFAULT_CSS = """
    _HeaderTokenBar {
        dock: right;
        width: auto;
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    """


class ContextHeader(Header):
    """Textual header with the context-window token bar in the right slot."""

    DEFAULT_CSS = """
    ContextHeader HeaderTitle {
        content-align: left middle;
        padding: 0 1;
    }
    """

    model: reactive[str] = reactive("", layout=False)
    tokens_in: reactive[int] = reactive(0, layout=False)
    tokens_out: reactive[int] = reactive(0, layout=False)

    def compose(self) -> ComposeResult:
        yield HeaderTitle()
        self._tokens = _HeaderTokenBar()
        yield self._tokens

    def watch_model(self, _old: str, new: str) -> None:
        if hasattr(self, "_tokens"):
            self._tokens.model = new

    def watch_tokens_in(self, _old: int, new: int) -> None:
        if hasattr(self, "_tokens"):
            self._tokens.tokens_in = new

    def watch_tokens_out(self, _old: int, new: int) -> None:
        if hasattr(self, "_tokens"):
            self._tokens.tokens_out = new


class _SessionClock(Static):
    """Static elapsed-since-launch counter — 1 s tick, no jitter."""

    DEFAULT_CSS = "_SessionClock { width: auto; height: 1; color: #83b8c2; }"

    def __init__(self) -> None:
        super().__init__("", markup=False)
        self._start = time.monotonic()

    def on_mount(self) -> None:
        self.set_interval(ELAPSED_TICK_MS / 1000, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        s = int(time.monotonic() - self._start)
        self.update(f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s")


class _CwdLabel(Static):
    """Working dir on the right edge."""

    DEFAULT_CSS = "_CwdLabel { width: auto; height: 1; color: #83b8c2; }"

    def __init__(self) -> None:
        import os
        from pathlib import Path
        cwd = Path(os.getcwd())
        try:
            home = Path.home()
            label = "~" + str(cwd.relative_to(home).as_posix()).rjust(0)
            if str(cwd) == str(home):
                label = "~"
            elif not str(cwd).startswith(str(home)):
                label = str(cwd)
            else:
                label = "~/" + str(cwd.relative_to(home))
        except (ValueError, OSError):
            label = str(cwd)
        if len(label) > 28:
            label = "…" + label[-27:]
        super().__init__(label, markup=False)


class _SyncBadge(Static):
    """Persistent "local-only" indicator for CLI sessions.

    CLI sessions never write to Firestore — they always stay at
    ``~/.flowly/sessions/*.jsonl``. iOS/desktop/Android chats sync via
    the relay when the user is signed in, but the TUI's own turns do
    not. The badge is always-on so a user who switches between desktop
    and CLI never has to wonder "did this turn get cloud-saved?".
    """

    DEFAULT_CSS = "_SyncBadge { width: auto; height: 1; color: #83b8c2; }"

    def __init__(self) -> None:
        super().__init__("🔒 local", markup=False)


class _CostBadge(Static):
    """Cumulative session cost in USD (rough estimate from token counts)."""

    DEFAULT_CSS = "_CostBadge { width: auto; height: 1; color: #83b8c2; }"

    cost_usd: reactive[float] = reactive(0.0, layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=False)

    def watch_cost_usd(self, _o: float, n: float) -> None:
        self.update(f"${n:.4f}" if n > 0 else "")


class _CmpBadge(Static):
    """Compaction count — colors escalate at 5+ (warn) and 10+ (error)."""

    DEFAULT_CSS = "_CmpBadge { width: auto; height: 1; }"

    cmp_count: reactive[int] = reactive(0, layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=True)

    def watch_cmp_count(self, _o: int, n: int) -> None:
        if n <= 0:
            self.update("")
            return
        if n >= 10:
            color = "#ff5d6c"
        elif n >= 5:
            color = "#f2c94c"
        else:
            color = "#83b8c2"
        self.update(f"[{color}]cmp {n}[/]")


class _BgBadge(Static):
    """Background tasks count (subagents running)."""

    DEFAULT_CSS = "_BgBadge { width: auto; height: 1; color: #83b8c2; }"

    bg_count: reactive[int] = reactive(0, layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=False)

    def watch_bg_count(self, _o: int, n: int) -> None:
        self.update(f"{n} bg" if n > 0 else "")


class _Badges(Static):
    DEFAULT_CSS = "_Badges { width: auto; height: 1; }"

    approvals: reactive[int] = reactive(0, layout=False)
    artifacts: reactive[int] = reactive(0, layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=True)

    def watch_approvals(self) -> None: self._refresh()
    def watch_artifacts(self) -> None: self._refresh()

    def _refresh(self) -> None:
        parts: list[str] = []
        if self.approvals:
            parts.append(f"[#f2c94c]⚠ {self.approvals}[/]")
        if self.artifacts:
            parts.append(f"[#35d5ef]◆ {self.artifacts}[/]")
        self.update(" ".join(parts))


class _MetaBadges(Static):
    """Optional right-side status badges rendered as one compact segment."""

    DEFAULT_CSS = "_MetaBadges { width: auto; height: 1; }"

    approvals: reactive[int] = reactive(0, layout=False)
    artifacts: reactive[int] = reactive(0, layout=False)
    cost_usd: reactive[float] = reactive(0.0, layout=False)
    cmp_count: reactive[int] = reactive(0, layout=False)
    bg_count: reactive[int] = reactive(0, layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=True)
        self._has_content = False
        self.display = False

    def watch_approvals(self) -> None: self._refresh()
    def watch_artifacts(self) -> None: self._refresh()
    def watch_cost_usd(self) -> None: self._refresh()
    def watch_cmp_count(self) -> None: self._refresh()
    def watch_bg_count(self) -> None: self._refresh()

    def _refresh(self) -> None:
        parts: list[str] = []
        if self.cmp_count:
            if self.cmp_count >= 10:
                color = "#ff5d6c"
            elif self.cmp_count >= 5:
                color = "#f2c94c"
            else:
                color = "#83b8c2"
            parts.append(f"[{color}]cmp {self.cmp_count}[/]")
        if self.bg_count:
            parts.append(f"[#83b8c2]{self.bg_count} bg[/]")
        if self.cost_usd > 0:
            parts.append(f"[#83b8c2]${self.cost_usd:.4f}[/]")
        if self.approvals:
            parts.append(f"[#f2c94c]⚠ {self.approvals}[/]")
        if self.artifacts:
            parts.append(f"[#35d5ef]◆ {self.artifacts}[/]")
        text = " ".join(parts)
        self.update(text)
        self._has_content = bool(text)
        self.display = self._has_content


# Standing permission level, leftmost in the bar. Colored by risk so the
# current stance is readable at a glance (traffic-light): green = Ask (prompts,
# safest), yellow = Auto, red = YOLO (runs unattended). The "●" glyph is
# single-width and renders on every terminal we target (already used above for
# offline/error), so no emoji-width drift across OSes.
_PERMISSION_BADGE: dict[str, tuple[str, str]] = {
    "ask":  ("#6ee7a0", "● ASK"),
    "auto": ("#f2c94c", "● AUTO"),
    "yolo": ("#ff5d6c", "● YOLO"),
}


class _PermissionBadge(Static):
    """Standing exec/codex permission level indicator (leftmost segment)."""

    DEFAULT_CSS = "_PermissionBadge { width: auto; height: 1; }"

    level: reactive[str] = reactive("", layout=False)

    def __init__(self) -> None:
        super().__init__("", markup=True)
        self.display = False  # hidden until we know the level

    def watch_level(self, _old: str, new: str) -> None:
        meta = _PERMISSION_BADGE.get(new)
        if not meta:
            self.update("")
            self.display = False
            return
        color, label = meta
        self.update(f"[{color}][b]{label}[/b][/]")
        self.display = True


# ── Composite ──────────────────────────────────────────────────────


class StatusBar(Horizontal):
    """Composite status row. Public reactives forward to child widgets."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: #000000;
        color: #e6fbff;
        padding: 0 1;
    }
    StatusBar > Static { padding: 0 1 0 0; }
    """

    state:             reactive[str] = reactive("idle")
    session:           reactive[str] = reactive("")
    permission:        reactive[str] = reactive("")
    provider:          reactive[str] = reactive("")
    model:             reactive[str] = reactive("")
    hint:              reactive[str] = reactive("")
    tokens_in:         reactive[int] = reactive(0)
    tokens_out:        reactive[int] = reactive(0)
    approvals_pending: reactive[int] = reactive(0)
    artifacts_count:   reactive[int] = reactive(0)
    cost_usd:          reactive[float] = reactive(0.0)
    cmp_count:         reactive[int] = reactive(0)
    bg_count:          reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        self._perm = _PermissionBadge()
        self._face = _FaceTicker()
        self._provider = _ProviderLabel()
        self._model = _ModelLabel()
        self._clock = _SessionClock()
        self._meta_sep = _Sep()
        self._meta = _MetaBadges()
        self._meta_sep.display = False
        self._state_sep = _Sep()
        self._state_sep.display = False
        self._sync = _SyncBadge()
        self._cwd = _CwdLabel()
        yield self._perm
        yield self._face
        yield self._state_sep
        yield self._provider
        yield _Sep()
        yield self._model
        yield _Sep()
        yield self._clock
        yield self._meta_sep
        yield self._meta
        yield _Sep()
        yield self._sync
        yield _Sep()
        yield self._cwd

    # Forward reactive writes from app code → child widgets. Only the
    # child whose data changed re-renders.
    def watch_permission(self, _o: str, n: str) -> None:
        if hasattr(self, "_perm"):
            self._perm.level = n
    def watch_state(self, _o: str, n: str) -> None:
        if hasattr(self, "_face"):
            self._face.state = n
            self._state_sep.display = n != "idle"
    def watch_provider(self, _o: str, n: str) -> None:
        if hasattr(self, "_provider"):
            self._provider.provider = n
    def watch_model(self, _o: str, n: str) -> None:
        if hasattr(self, "_model"):
            self._model.model = n
        self._sync_context_header(model=n)
    def watch_tokens_in(self, _o: int, n: int) -> None:
        self._sync_context_header(tokens_in=n)
    def watch_tokens_out(self, _o: int, n: int) -> None:
        self._sync_context_header(tokens_out=n)
    def watch_approvals_pending(self, _o: int, n: int) -> None:
        if hasattr(self, "_meta"):
            self._meta.approvals = n
            self._sync_meta_separator()
    def watch_artifacts_count(self, _o: int, n: int) -> None:
        if hasattr(self, "_meta"):
            self._meta.artifacts = n
            self._sync_meta_separator()
    def watch_cost_usd(self, _o: float, n: float) -> None:
        if hasattr(self, "_meta"):
            self._meta.cost_usd = n
            self._sync_meta_separator()
    def watch_cmp_count(self, _o: int, n: int) -> None:
        if hasattr(self, "_meta"):
            self._meta.cmp_count = n
            self._sync_meta_separator()
    def watch_bg_count(self, _o: int, n: int) -> None:
        if hasattr(self, "_meta"):
            self._meta.bg_count = n
            self._sync_meta_separator()

    def reset_context_usage(self) -> None:
        """Reset per-conversation usage without touching provider/model/session."""
        self.tokens_in = 0
        self.tokens_out = 0
        self.cmp_count = 0
        self.cost_usd = 0.0
        if hasattr(self, "_meta"):
            self._meta.cmp_count = 0
            self._meta.cost_usd = 0.0
            self._sync_meta_separator()
        self._sync_context_header(tokens_in=0, tokens_out=0)

    def _sync_meta_separator(self) -> None:
        try:
            self._meta_sep.display = bool(getattr(self._meta, "_has_content", False))
        except Exception:
            pass

    def _sync_context_header(
        self,
        *,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        try:
            header = self.app.query_one(ContextHeader)
        except Exception:
            return
        if model is not None:
            header.model = model
        if tokens_in is not None:
            header.tokens_in = tokens_in
        if tokens_out is not None:
            header.tokens_out = tokens_out
        tokens_bar = getattr(header, "_tokens", None)
        if tokens_bar is not None:
            if model is not None:
                tokens_bar.model = model
            if tokens_in is not None:
                tokens_bar.tokens_in = tokens_in
            if tokens_out is not None:
                tokens_bar.tokens_out = tokens_out
            try:
                tokens_bar._refresh()
            except Exception:
                pass
