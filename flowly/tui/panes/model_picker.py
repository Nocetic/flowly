"""ModelPicker — searchable list of LLM models for the current provider.

Opened by ``/model`` (no arg) or by future keybinding. Fetches the
provider's catalog (cached for the TUI session), shows it in a filterable
list, and on Enter writes ``agents.defaults.model`` to ``config.json``
then triggers the gateway's hot-reload so the next chat uses the new
model immediately.

For providers without a model-list fetcher we surface a hint pointing at
their dashboard (``docs_url`` from the integration card).
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from flowly.integrations.model_catalog import Model, fetch_models


_PREFIX = "MODEL:"


class ModelPicker(ModalScreen[dict[str, Any] | None]):
    """Dismisses with:
      {'action': 'switched', 'model': '<id>'}
      None  (cancel / no provider)
    """

    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    ModelPicker > Vertical {
        width: 75%;
        max-width: 90;
        height: 80%;
        max-height: 32;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    ModelPicker .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    ModelPicker .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    ModelPicker Input {
        height: 3;
        margin-bottom: 1;
    }
    ModelPicker OptionList {
        height: 1fr;
        border: none;
        background: $surface;
    }
    ModelPicker .footer {
        color: $text-muted;
        text-style: italic;
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
    ]

    # Focus the search field by default — picker is search-first.
    AUTO_FOCUS = "Input"

    def __init__(
        self,
        provider_key: str,
        provider_label: str,
        current_model: str = "",
        docs_url: str = "",
    ) -> None:
        super().__init__()
        self._provider_key = provider_key
        self._provider_label = provider_label
        self._current_model = current_model
        self._docs_url = docs_url
        self._all: list[Model] = []
        self._filtered: list[Model] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Models — {self._provider_label}", classes="title")
            yield Label(
                f"current: [b]{self._current_model or '?'}[/b]  ·  "
                "type to filter · ↑/↓ navigate · Enter select · Esc close",
                classes="hint",
            )
            yield Input(placeholder="filter by id / vendor / tag…",
                        id="model-filter")
            yield OptionList(id="model-list")
            yield Label("", id="model-footer", classes="footer")

    async def on_mount(self) -> None:
        await self._load()

    async def _load(self) -> None:
        self._set_footer(f"fetching catalog for {self._provider_label}…")
        try:
            self._all = await fetch_models(self._provider_key)
        except Exception as exc:
            self._all = []
            self._set_footer(f"[red]fetch failed: {exc}[/red]")
        if not self._all:
            doc = f" · {self._docs_url}" if self._docs_url else ""
            self._set_footer(
                f"[yellow]no catalog available for {self._provider_label}{doc}[/yellow]"
            )
            return
        self._set_footer(f"{len(self._all)} models loaded")
        self._filtered = list(self._all)
        self._render_list()

    # ── render + filter ──────────────────────────────────────────

    def _render_list(self) -> None:
        ol = self.query_one("#model-list", OptionList)
        ol.clear_options()
        for m in self._filtered:
            ol.add_option(Option(self._row_text(m), id=f"{_PREFIX}{m.id}"))
        # Highlight the current model if it's in the filtered view, else
        # land on the top row so Enter does something predictable.
        if self._current_model:
            for i, m in enumerate(self._filtered):
                if m.id == self._current_model:
                    ol.highlighted = i
                    return
        if self._filtered:
            ol.highlighted = 0

    def _row_text(self, m: Model) -> str:
        locked = "locked" in m.tags
        # Lock-out is visual + a [red] hint so the user sees at-a-glance
        # which models will be rejected at the proxy. Letting them pick
        # one anyway is fine — they just learn the limit immediately
        # instead of after sending a message.
        label = (
            f"[strike dim]{m.id}[/]" if locked else f"[b]{m.id}[/b]"
        )
        bits = [label]
        if m.id == self._current_model:
            bits.append("[yellow]★ current[/yellow]")
        if locked:
            bits.append("[red]🔒 not in your plan[/red]")
        if "free" in m.tags:
            bits.append("[green]free[/green]")
        if "vision" in m.tags:
            bits.append("[cyan]vision[/cyan]")
        if m.context_window:
            bits.append(f"[dim]{m.context_window // 1000}k ctx[/dim]")
        if m.pricing_in is not None and m.pricing_out is not None:
            bits.append(
                f"[dim]${m.pricing_in:.2f}/${m.pricing_out:.2f} per 1M[/dim]"
            )
        return "  ".join(bits)

    @on(Input.Changed, "#model-filter")
    def _on_filter(self, event: Input.Changed) -> None:
        q = (event.value or "").strip().lower()
        if not q:
            self._filtered = list(self._all)
        else:
            self._filtered = [
                m for m in self._all
                if q in m.id.lower()
                or q in m.name.lower()
                or any(q in t for t in m.tags)
            ]
        self._render_list()
        self._set_footer(
            f"{len(self._filtered)} / {len(self._all)} models match '{q}'"
            if q
            else f"{len(self._all)} models loaded"
        )

    # ── selection ────────────────────────────────────────────────

    @on(Input.Submitted, "#model-filter")
    def _submit_filter(self, event: Input.Submitted) -> None:
        # Enter on the filter box picks the first match.
        ol = self.query_one("#model-list", OptionList)
        if ol.highlighted is None and ol.options:
            ol.highlighted = 0
        if ol.highlighted is not None:
            try:
                opt = ol.get_option_at_index(ol.highlighted)
                self._select(str(opt.id or ""))
            except Exception:
                pass

    @on(OptionList.OptionSelected, "#model-list")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        self._select(str(event.option.id or ""))

    @work
    async def _select(self, opt_id: str) -> None:
        if not opt_id.startswith(_PREFIX):
            return
        model_id = opt_id[len(_PREFIX):]
        # Refuse locked models up front — the proxy would reject them
        # with "not in your plan" otherwise, and the user would just
        # see a cryptic error mid-stream. Spelling it out here keeps
        # the failure local and actionable.
        picked = next((m for m in self._all if m.id == model_id), None)
        if picked is not None and "locked" in picked.tags:
            self._set_footer(
                f"[red]🔒 {model_id} isn't in your Flowly plan — "
                f"pick another or upgrade at useflowlyapp.com/account[/red]"
            )
            return
        try:
            await asyncio.to_thread(_set_default_model, model_id)
        except Exception as exc:
            self._set_footer(f"[red]save failed: {exc}[/red]")
            return
        tail = await self._reload_gateway()
        self._set_footer(f"✓ default model → [b]{model_id}[/b] · {tail}")
        await asyncio.sleep(0.8)
        self.dismiss({"action": "switched", "model": model_id})

    async def _reload_gateway(self) -> str:
        """Tell the running gateway to swap its LLM client + model. Also
        pushes the new model into the StatusBar so the chip in the
        bottom bar reflects the switch immediately (otherwise the user
        had to restart the TUI to see the new label)."""
        from flowly.tui.gateway_reload import post_provider_reload
        try:
            r = await post_provider_reload(timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                new_model = str(data.get("model") or "")
                if new_model:
                    self._push_model_to_status(new_model)
                return (
                    f"gateway reloaded → {data.get('source') or data.get('key') or '?'}"
                )
            return f"[yellow]reload HTTP {r.status_code}[/yellow]"
        except Exception:
            return "[dim]gateway offline — restart to apply[/dim]"

    def _push_model_to_status(self, model: str) -> None:
        """Walk up to the running app and update the StatusBar's model
        reactive. Best-effort — silently no-ops if status is gone or the
        modal is detached."""
        try:
            from flowly.tui.panes.status import StatusBar
            self.app.query_one(StatusBar).model = model
        except Exception:
            pass

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#model-footer", Label).update(text)
        except Exception:
            pass


# ── helper: persist the model choice (mirrors set_active_provider) ─


def _set_default_model(model_id: str) -> None:
    """Write ``agents.defaults.model`` atomically. Mirrors
    :func:`flowly.integrations.active_provider.set_active_provider` so the
    write path stays consistent (camelCase on disk, atomic temp+rename).
    """
    from flowly.config.loader import get_config_path
    from flowly.integrations.config_io import (
        _atomic_write_json, _load_raw, _set_path,
    )
    raw = _load_raw()
    _set_path(raw, "agents.defaults.model", model_id, merge=False)
    _atomic_write_json(get_config_path(), raw)
