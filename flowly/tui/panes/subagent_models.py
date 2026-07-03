"""Subagent model settings — pick which model each specialist runs on.

Opened by ``/subagents models``. Lists the registered specialists
(researcher, writer, coder, plus any user assistants) with their current
effective model, and lets you set a per-specialist override:

  * a concrete model id from the active provider's live catalogue,
  * "Use my model" → the bot's selected model (override ``inherit``),
  * "Default" → clear the override (the specialist's own default model).

Writes go through the gateway's ``subagents.set_model`` feature RPC, so the
running bot picks the change up on its next dispatch (no restart).
"""

from __future__ import annotations

from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from flowly.integrations.model_catalog import Model, fetch_models


def _short(model_id: str) -> str:
    if not model_id:
        return ""
    return model_id.rsplit("/", 1)[-1] if "/" in model_id else model_id


_SPEC_PREFIX = "SPEC:"


class SubagentModelsModal(ModalScreen[None]):
    """Specialist → model editor. Talks to the gateway via the passed client."""

    DEFAULT_CSS = """
    SubagentModelsModal { align: center middle; }
    SubagentModelsModal > Vertical {
        width: 80%;
        max-width: 96;
        height: 80%;
        max-height: 30;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    SubagentModelsModal .title { text-style: bold; color: $primary; height: 1; }
    SubagentModelsModal .hint {
        color: $text-muted; text-style: italic; height: auto; margin-bottom: 1;
    }
    SubagentModelsModal OptionList {
        height: 1fr; border: none; background: $surface;
    }
    SubagentModelsModal .footer {
        color: $text-muted; text-style: italic; height: auto; margin-top: 1;
    }
    """

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client
        self._assistants: list[dict[str, Any]] = []
        self._bot_model: str = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Subagent models", classes="title")
            yield Label(
                "Pick the model each specialist runs on when your assistant "
                "delegates a task · ↑/↓ navigate · Enter to change · Esc close",
                classes="hint",
            )
            yield OptionList(id="spec-list")
            yield Label("", id="spec-footer", classes="footer")

    async def on_mount(self) -> None:
        await self._load()

    async def _load(self) -> None:
        self._set_footer("loading specialists…")
        try:
            data = await self._client.subagents_assistants()
        except Exception as exc:
            self._set_footer(f"[red]load failed: {exc}[/red]")
            return
        self._assistants = list(data.get("assistants") or [])
        self._bot_model = str(data.get("botModel") or "")
        if not self._assistants:
            self._set_footer("[yellow]no specialists registered[/yellow]")
            return
        self._render_list()
        self._set_footer(f"{len(self._assistants)} specialist(s)")

    def _render_list(self) -> None:
        ol = self.query_one("#spec-list", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        for a in self._assistants:
            ol.add_option(Option(self._row_text(a), id=f"{_SPEC_PREFIX}{a['name']}"))
        if self._assistants:
            ol.highlighted = keep if keep is not None and keep < len(self._assistants) else 0

    def _row_text(self, a: dict[str, Any]) -> str:
        override = a.get("override") or ""
        if override == "inherit":
            chosen = f"[cyan]my model[/cyan] ([dim]{_short(self._bot_model)}[/dim])"
        elif override:
            chosen = f"[b]{_short(override)}[/b]"
        else:
            chosen = f"[dim]default ({_short(a.get('defaultModel') or '')})[/dim]"
        builtin = " [dim]·built-in[/dim]" if a.get("builtin") else ""
        return f"[b]{a['name']}[/b]{builtin}  →  {chosen}"

    @on(OptionList.OptionSelected, "#spec-list")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        opt_id = str(event.option.id or "")
        if opt_id.startswith(_SPEC_PREFIX):
            self._edit_specialist(opt_id[len(_SPEC_PREFIX):])

    @work
    async def _edit_specialist(self, name: str) -> None:
        a = next((x for x in self._assistants if x["name"] == name), None)
        if a is None:
            return
        picker = _SpecialistModelPicker(
            specialist=name,
            default_model=str(a.get("defaultModel") or ""),
            bot_model=self._bot_model,
            override=str(a.get("override") or ""),
        )
        show_inline = getattr(self.app, "_show_inline_screen", None)
        choice = (
            await show_inline(picker)
            if callable(show_inline)
            else await self.app.push_screen_wait(picker)
        )
        if choice is None:
            return  # cancelled
        self._set_footer(f"saving {name}…")
        try:
            res = await self._client.subagents_set_model(name, choice)
        except Exception as exc:
            self._set_footer(f"[red]save failed: {exc}[/red]")
            return
        # Reflect the server's resolved state locally, then re-render.
        a["override"] = res.get("override", choice)
        a["effectiveModel"] = res.get("effectiveModel", a.get("effectiveModel"))
        self._bot_model = str(res.get("botModel") or self._bot_model)
        self._render_list()
        eff = _short(str(res.get("effectiveModel") or ""))
        self._set_footer(f"✓ {name} → [b]{eff}[/b]")

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#spec-footer", Label).update(text)
        except Exception:
            pass


_OVR_PREFIX = "OVR:"
_MODEL_PREFIX = "MODEL:"


class _SpecialistModelPicker(ModalScreen[str | None]):
    """Pick a model for one specialist.

    Dismisses with the override string to persist:
      ``"inherit"`` (use the bot model), ``""`` (clear → default), or a model id.
    ``None`` ⇒ cancelled.
    """

    DEFAULT_CSS = """
    _SpecialistModelPicker { align: center middle; }
    _SpecialistModelPicker > Vertical {
        width: 75%; max-width: 90; height: 80%; max-height: 32;
        padding: 1 2; border: thick $primary; background: $surface;
    }
    _SpecialistModelPicker .title { text-style: bold; color: $primary; height: 1; }
    _SpecialistModelPicker .hint {
        color: $text-muted; text-style: italic; height: 1; margin-bottom: 1;
    }
    _SpecialistModelPicker Input { height: 3; margin-bottom: 1; }
    _SpecialistModelPicker OptionList { height: 1fr; border: none; background: $surface; }
    _SpecialistModelPicker .footer {
        color: $text-muted; text-style: italic; height: auto; margin-top: 1;
    }
    """

    BINDINGS = [("escape", "dismiss(None)", "Close")]
    AUTO_FOCUS = "Input"

    def __init__(
        self, specialist: str, default_model: str, bot_model: str, override: str
    ) -> None:
        super().__init__()
        self._specialist = specialist
        self._default_model = default_model
        self._bot_model = bot_model
        self._override = override
        self._all: list[Model] = []
        self._filtered: list[Model] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Model — {self._specialist}", classes="title")
            yield Label(
                "type to filter · ↑/↓ navigate · Enter select · Esc close",
                classes="hint",
            )
            yield Input(placeholder="filter by id / vendor / tag…", id="spm-filter")
            yield OptionList(id="spm-list")
            yield Label("", id="spm-footer", classes="footer")

    async def on_mount(self) -> None:
        await self._load()

    async def _load(self) -> None:
        # Resolve the active provider locally and fetch its catalogue (same
        # source the /model picker uses). Synthetic options always show even
        # if the catalogue is empty/unavailable.
        from flowly.config.loader import load_config
        from flowly.integrations.active_provider import resolve_active_provider

        self._set_footer("fetching catalog…")
        try:
            active = resolve_active_provider(load_config())
            if active is not None:
                self._all = await fetch_models(active.key)
        except Exception as exc:
            self._all = []
            self._set_footer(f"[yellow]catalog unavailable: {exc}[/yellow]")
        self._filtered = list(self._all)
        self._render_list()
        if self._all:
            self._set_footer(f"{len(self._all)} models")

    def _render_list(self) -> None:
        ol = self.query_one("#spm-list", OptionList)
        ol.clear_options()
        # Two synthetic options first.
        mine_mark = " [yellow]★[/yellow]" if self._override == "inherit" else ""
        def_mark = " [yellow]★[/yellow]" if self._override == "" else ""
        ol.add_option(Option(
            f"[cyan]Use my model[/cyan]{mine_mark}  "
            f"[dim]({_short(self._bot_model) or 'bot model'})[/dim]",
            id=f"{_OVR_PREFIX}inherit",
        ))
        ol.add_option(Option(
            f"Default{def_mark}  [dim]({_short(self._default_model)})[/dim]",
            id=f"{_OVR_PREFIX}",
        ))
        for m in self._filtered:
            star = " [yellow]★[/yellow]" if m.id == self._override else ""
            ctx = f"  [dim]{m.context_window // 1000}k[/dim]" if m.context_window else ""
            ol.add_option(Option(f"[b]{m.id}[/b]{star}{ctx}", id=f"{_MODEL_PREFIX}{m.id}"))
        # Land on a sensible default row.
        ol.highlighted = 0

    @on(Input.Changed, "#spm-filter")
    def _on_filter(self, event: Input.Changed) -> None:
        q = (event.value or "").strip().lower()
        if not q:
            self._filtered = list(self._all)
        else:
            self._filtered = [
                m for m in self._all
                if q in m.id.lower() or q in m.name.lower() or any(q in t for t in m.tags)
            ]
        self._render_list()

    @on(Input.Submitted, "#spm-filter")
    def _submit_filter(self, event: Input.Submitted) -> None:
        ol = self.query_one("#spm-list", OptionList)
        if ol.highlighted is not None:
            try:
                opt = ol.get_option_at_index(ol.highlighted)
                self._select(str(opt.id or ""))
            except Exception:
                pass

    @on(OptionList.OptionSelected, "#spm-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        self._select(str(event.option.id or ""))

    def _select(self, opt_id: str) -> None:
        if opt_id.startswith(_OVR_PREFIX):
            self.dismiss(opt_id[len(_OVR_PREFIX):])  # "inherit" or ""
        elif opt_id.startswith(_MODEL_PREFIX):
            self.dismiss(opt_id[len(_MODEL_PREFIX):])

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#spm-footer", Label).update(text)
        except Exception:
            pass
