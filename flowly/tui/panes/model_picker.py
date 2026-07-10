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
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from flowly.integrations.model_catalog import Model, fetch_models
from flowly.tui.panes.inline_picker import (
    clamp_index,
    fuzzy_filter,
    is_plain_character,
    picker_width_for_columns,
    visible_window,
)

VISIBLE_ROWS = 12


class ModelPickerPanel(Vertical):
    """Dismisses with:
      {'action': 'switched', 'model': '<id>'}
      None  (cancel / no provider)
    """

    can_focus = True

    DEFAULT_CSS = """
    ModelPickerPanel {
        width: auto;
        min-width: 40;
        max-width: 90;
        height: auto;
        max-height: 22;
        padding: 0 1;
        border: double $primary;
        background: $surface;
    }
    ModelPickerPanel .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    ModelPickerPanel .hint {
        color: $text-muted;
        height: 1;
    }
    ModelPickerPanel .filter-line,
    ModelPickerPanel .scroll-line {
        color: $text-muted;
        height: 1;
    }
    ModelPickerPanel .filter-line.active {
        color: $primary;
    }
    ModelPickerPanel .warning-line {
        height: 1;
        color: $warning;
    }
    ModelPickerPanel .picker-row {
        height: 1;
        color: $text-muted;
    }
    ModelPickerPanel .picker-row.selected {
        background: $primary;
        color: $surface;
        text-style: bold;
    }
    ModelPickerPanel .footer {
        color: $text-muted;
        height: 1;
    }
    """

    BINDINGS = []

    class Dismissed(Message):
        def __init__(self, result: dict[str, Any] | None) -> None:
            super().__init__()
            self.result = result

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
        self._filter = ""
        self._empty_message = "loading models..."
        self._selected_idx = 0

    def compose(self) -> ComposeResult:
        yield Static("Select model", classes="title")
        yield Static(f"{self._provider_label} · current: [b]{self._current_model or '?'}[/b]",
                     classes="hint")
        yield Static("", id="model-filter-line", classes="filter-line")
        yield Static("", id="model-warning-line", classes="warning-line")
        yield Static("", id="model-scroll-top", classes="scroll-line")
        for i in range(VISIBLE_ROWS):
            yield Static("", id=f"model-row-{i}", classes="picker-row")
        yield Static("", id="model-scroll-bottom", classes="scroll-line")
        yield Static("↑/↓ select · Enter switch · Esc clear/back · q close",
                     id="model-footer", classes="footer")

    async def on_mount(self) -> None:
        self._sync_panel_width()
        self.focus()
        await self._load()

    def on_resize(self, _event: events.Resize) -> None:
        self._sync_panel_width()

    def _sync_panel_width(self) -> None:
        try:
            if self._is_composer_inline():
                self.styles.width = "100%"
                self.styles.max_width = "100%"
                return
            self.styles.width = picker_width_for_columns(self.app.size.width)
        except Exception:
            pass

    def _is_composer_inline(self) -> bool:
        return any(
            bool(getattr(node, "has_class", lambda _name: False)("picker-inline-open"))
            for node in self.ancestors
        )

    def _finish(self, result: dict[str, Any] | None) -> None:
        self.post_message(self.Dismissed(result))

    async def _load(self) -> None:
        self._set_footer(f"fetching catalog for {self._provider_label}…")
        self._empty_message = f"loading catalog for {self._provider_label}..."
        self._render_list()
        try:
            self._all = await fetch_models(self._provider_key)
        except Exception as exc:
            self._all = []
            self._empty_message = "catalog fetch failed"
            self._set_footer(f"[red]fetch failed: {exc}[/red]")
            self._render_list()
            return
        if not self._all:
            doc = f" · {self._docs_url}" if self._docs_url else ""
            self._empty_message = f"no catalog available for {self._provider_label}"
            self._set_footer(
                f"[yellow]no catalog available for {self._provider_label}{doc}[/yellow]"
            )
            self._render_list()
            return
        self._set_footer(f"{len(self._all)} models loaded")
        self._filtered = list(self._all)
        self._selected_idx = self._current_model_index()
        self._render_list()

    # ── render + filter ──────────────────────────────────────────

    def _render_list(self, preferred_model: str | None = None) -> None:
        self._filtered = fuzzy_filter(self._all, self._filter, self._model_search_text)
        preferred_idx = self._index_for_model(preferred_model)
        if preferred_idx is not None:
            self._selected_idx = preferred_idx
        else:
            self._selected_idx = clamp_index(self._selected_idx, len(self._filtered))
        start, end = visible_window(self._selected_idx, len(self._filtered), VISIBLE_ROWS)
        visible = self._filtered[start:end]
        try:
            line = self.query_one("#model-filter-line", Static)
            if self._filter:
                line.add_class("active")
                line.update(f"filter: {self._filter}▎ · {len(self._filtered)}/{len(self._all)}")
            else:
                line.remove_class("active")
                line.update("type to filter · ↑/↓ select")
        except Exception:
            pass
        try:
            warning = " "
            selected = self._selected_model()
            if selected is not None and "locked" in selected.tags:
                warning = f"warning: {selected.id} is not in your plan"
            self.query_one("#model-warning-line", Static).update(warning)
            self.query_one("#model-scroll-top", Static).update(
                f" ↑ {start} more" if start > 0 else " "
            )
            self.query_one("#model-scroll-bottom", Static).update(
                f" ↓ {len(self._filtered) - end} more" if end < len(self._filtered) else " "
            )
        except Exception:
            pass
        for row in range(VISIBLE_ROWS):
            widget = self.query_one(f"#model-row-{row}", Static)
            idx = start + row
            if row >= len(visible):
                widget.remove_class("selected")
                widget.update(
                    "no models match filter"
                    if row == 0 and self._filter and not self._filtered
                    else self._empty_message if row == 0 and not self._filtered else " "
                )
                continue
            model = visible[row]
            selected = idx == self._selected_idx
            if selected:
                widget.add_class("selected")
            else:
                widget.remove_class("selected")
            marker = "▸" if selected else "*" if model.id == self._current_model else " "
            widget.update(f"{marker} {idx + 1}. {self._row_text(model)}")

    def _current_model_index(self) -> int:
        if self._current_model:
            for i, m in enumerate(self._filtered):
                if m.id == self._current_model:
                    return i
        return 0

    def _selected_model(self) -> Model | None:
        if not self._filtered:
            return None
        self._selected_idx = clamp_index(self._selected_idx, len(self._filtered))
        return self._filtered[self._selected_idx]

    def _selected_model_id(self) -> str | None:
        model = self._selected_model()
        return model.id if model is not None else None

    @staticmethod
    def _model_search_text(model: Model) -> str:
        return " ".join([model.id, model.name, model.description, *model.tags])

    def _index_for_model(self, model_id: str | None) -> int | None:
        if not model_id:
            return None
        for idx, model in enumerate(self._filtered):
            if model.id == model_id:
                return idx
        return None

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

    # ── selection ────────────────────────────────────────────────

    @work
    async def _select(self, model_id: str) -> None:
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
        self._finish({"action": "switched", "model": model_id})

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
            self.query_one("#model-footer", Static).update(text)
        except Exception:
            pass

    def action_cancel(self) -> None:
        self._finish(None)

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""
        handled = True
        is_ctrl_u = key == "ctrl+u" or (getattr(event, "ctrl", False) and char == "u")
        if key == "escape":
            if self._filter:
                preferred_model = self._selected_model_id() or self._current_model
                self._filter = ""
                self._render_list(preferred_model)
            else:
                self.action_cancel()
        elif key == "q" and not self._filter:
            self.action_cancel()
        elif key == "up":
            if self._filtered:
                self._selected_idx = max(0, self._selected_idx - 1)
                self._render_list()
        elif key == "down":
            if self._filtered:
                self._selected_idx = min(len(self._filtered) - 1, self._selected_idx + 1)
                self._render_list()
        elif key == "home":
            if self._filtered:
                self._selected_idx = 0
                self._render_list()
        elif key == "end":
            if self._filtered:
                self._selected_idx = len(self._filtered) - 1
                self._render_list()
        elif key == "pageup":
            if self._filtered:
                self._selected_idx = max(0, self._selected_idx - VISIBLE_ROWS)
                self._render_list()
        elif key == "pagedown":
            if self._filtered:
                self._selected_idx = min(len(self._filtered) - 1, self._selected_idx + VISIBLE_ROWS)
                self._render_list()
        elif key in ("enter", "return"):
            model = self._selected_model()
            if model is not None:
                self._select(model.id)
        elif key in ("backspace", "delete"):
            preferred_model = self._selected_model_id()
            self._filter = self._filter[:-1]
            self._render_list(preferred_model)
        elif is_ctrl_u:
            preferred_model = self._selected_model_id()
            self._filter = ""
            self._render_list(preferred_model)
        elif is_plain_character(event, char):
            self._filter += char
            self._selected_idx = 0
            self._render_list()
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()


# ── helper: persist the model choice (mirrors set_active_provider) ─


def _set_default_model(model_id: str) -> None:
    """Write ``agents.defaults.model`` atomically. Mirrors
    :func:`flowly.integrations.active_provider.set_active_provider` so the
    write path stays consistent (camelCase on disk, atomic temp+rename).
    """
    from flowly.config.loader import get_config_path
    from flowly.integrations.config_io import (
        _atomic_write_json,
        _load_raw,
        _set_path,
    )
    raw = _load_raw()
    _set_path(raw, "agents.defaults.model", model_id, merge=False)
    _atomic_write_json(get_config_path(), raw)


class ModelPicker(ModalScreen[dict[str, Any] | None]):
    """Modal wrapper kept for setup flows; chat mounts ModelPickerPanel inline."""

    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    """

    def __init__(
        self,
        provider_key: str,
        provider_label: str,
        current_model: str = "",
        docs_url: str = "",
    ) -> None:
        super().__init__()
        self._panel = ModelPickerPanel(
            provider_key=provider_key,
            provider_label=provider_label,
            current_model=current_model,
            docs_url=docs_url,
        )

    def compose(self) -> ComposeResult:
        yield self._panel

    @on(ModelPickerPanel.Dismissed)
    def _on_dismissed(self, event: ModelPickerPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)
