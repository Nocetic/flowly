"""Flowly TUI themes.

The default (flowly) is the signature black + Turkish-blue.
Power users and ricers can choose from a curated collection of
serious, cozy, warm, and community palettes (Catppuccin, Tokyo Night,
Synthwave, etc). All support live preview in the
/theme picker and pair with a matching code highlighter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class FlowlyPalette:
    name: str
    label: str
    description: str
    code_theme: str
    bg: str
    bg_alt: str             # composer / status bg
    bg_tool: str            # tool line bg block
    surface: str            # modals / panels
    surface_alt: str        # nested modal blocks
    boost: str              # emphasized blocks
    text: str
    text_muted: str
    accent: str             # primary highlight (prompt char, header)
    accent_soft: str
    user: str               # user bubble border + label
    assistant: str          # assistant bubble border + label
    system: str             # system bubble border + label
    error: str
    success: str
    warning: str
    border: str             # composer top border


FLOWLY_PALETTE = FlowlyPalette(
    name="flowly",
    label="Flowly",
    description="signature black + Turkish blue",
    code_theme="monokai",
    bg="#000000",
    bg_alt="#050505",
    bg_tool="#0a0a0a",
    surface="#050505",
    surface_alt="#0a0a0a",
    boost="#101010",
    text="#e6fbff",
    text_muted="#83b8c2",
    accent="#00a6c8",
    accent_soft="#35d5ef",
    user="#35d5ef",
    assistant="#00a6c8",
    system="#466b73",
    error="#ff5d6c",
    success="#31d0aa",
    warning="#f2c94c",
    border="#0f4c5c",
)

MONO_PALETTE = FlowlyPalette(
    name="mono",
    label="Mono",
    description="strict grayscale terminal mode",
    code_theme="bw",
    bg="#000000",
    bg_alt="#070707",
    bg_tool="#0b0b0b",
    surface="#080808",
    surface_alt="#101010",
    boost="#161616",
    text="#f2f2f2",
    text_muted="#9a9a9a",
    accent="#d7d7d7",
    accent_soft="#ffffff",
    user="#e5e5e5",
    assistant="#c7c7c7",
    system="#737373",
    error="#ff6b6b",
    success="#d4d4d4",
    warning="#f5f5f5",
    border="#4a4a4a",
)

AMBER_PALETTE = FlowlyPalette(
    name="amber",
    label="Amber",
    description="warm low-blue-light palette",
    code_theme="vim",
    bg="#050403",
    bg_alt="#0c0905",
    bg_tool="#100c06",
    surface="#0c0905",
    surface_alt="#151008",
    boost="#1d1609",
    text="#fff1cf",
    text_muted="#b99655",
    accent="#f59e0b",
    accent_soft="#fbbf24",
    user="#fbbf24",
    assistant="#f59e0b",
    system="#8a6a31",
    error="#fb7185",
    success="#84cc16",
    warning="#facc15",
    border="#7c4a03",
)

HACKER_PALETTE = FlowlyPalette(
    name="hacker",
    label="Hacker",
    description="neon green black-terminal mode",
    code_theme="native",
    bg="#000000",
    bg_alt="#020802",
    bg_tool="#031003",
    surface="#020802",
    surface_alt="#061406",
    boost="#081f08",
    text="#d7ffd2",
    text_muted="#7fbd7a",
    accent="#39ff14",
    accent_soft="#93ff7d",
    user="#7dff68",
    assistant="#39ff14",
    system="#4c8f45",
    error="#ff3864",
    success="#00ff87",
    warning="#ccff00",
    border="#147a20",
)

MOONFLY_PALETTE = FlowlyPalette(
    name="moonfly",
    label="Moonfly",
    description="near-black vim palette with soft periwinkle",
    code_theme="monokai",
    bg="#080808",
    bg_alt="#101113",
    bg_tool="#15171a",
    surface="#101113",
    surface_alt="#181a1d",
    boost="#23262a",
    text="#bdbdbd",
    text_muted="#949494",
    accent="#80a0ff",
    accent_soft="#79dac8",
    user="#79dac8",
    assistant="#80a0ff",
    system="#74787d",
    error="#ff5454",
    success="#8cc85f",
    warning="#e3c78a",
    border="#323437",
)

# --- viral & aesthetic community themes (added for variety + shareability) ---

CATPPUCCIN_PALETTE = FlowlyPalette(
    name="catppuccin",
    label="Catppuccin",
    description="cozy pastel dark — community favorite",
    code_theme="catppuccin-mocha",
    bg="#1e1e2e",
    bg_alt="#181825",
    bg_tool="#1e1e2e",
    surface="#1e1e2e",
    surface_alt="#11111b",
    boost="#313244",
    text="#cdd6f4",
    text_muted="#a6adc8",
    accent="#cba6f7",      # mauve
    accent_soft="#89b4fa", # sky
    user="#89b4fa",
    assistant="#cba6f7",
    system="#6c7086",
    error="#f38ba8",
    success="#a6e3a1",
    warning="#f9e2af",
    border="#45475a",
)

TOKYO_PALETTE = FlowlyPalette(
    name="tokyo-night",
    label="Tokyo Night",
    description="electric blue-purple modern night",
    code_theme="tokyo-night",
    bg="#1a1b26",
    bg_alt="#16161e",
    bg_tool="#1f2335",
    surface="#1a1b26",
    surface_alt="#1f2335",
    boost="#292e42",
    text="#c0caf5",
    text_muted="#565f89",
    accent="#7aa2f7",
    accent_soft="#bb9af7",
    user="#bb9af7",
    assistant="#7aa2f7",
    system="#565f89",
    error="#f7768e",
    success="#9ece6a",
    warning="#e0af68",
    border="#414868",
)

SYNTHWAVE_PALETTE = FlowlyPalette(
    name="synthwave",
    label="Synthwave",
    description="80s outrun neon — pink, cyan, vapor",
    code_theme="synthwave",
    bg="#0f0e17",
    bg_alt="#120d1f",
    bg_tool="#1a1530",
    surface="#120d1f",
    surface_alt="#1f1a33",
    boost="#2a2142",
    text="#f0e7ff",
    text_muted="#8a7f9e",
    accent="#ff2a6d",  # hot pink
    accent_soft="#05d9e8",  # cyan
    user="#05d9e8",
    assistant="#ff2a6d",
    system="#6b5b8c",
    error="#ff4d6d",
    success="#00ff9f",
    warning="#f7c948",
    border="#4a3f6b",
)

THEMES: dict[str, FlowlyPalette] = {
    p.name: p
    for p in (
        FLOWLY_PALETTE,
        MOONFLY_PALETTE,
        MONO_PALETTE,
        AMBER_PALETTE,
        HACKER_PALETTE,
        CATPPUCCIN_PALETTE,
        TOKYO_PALETTE,
        SYNTHWAVE_PALETTE,
    )
}

THEME_ALIASES: dict[str, str] = {
    "default": "flowly",
    "turkish-blue": "flowly",
    "turkish_blue": "flowly",
    "black": "flowly",
    "gray": "mono",
    "grey": "mono",
    "matrix": "hacker",
    "moon": "moonfly",
    "moon-fly": "moonfly",
    "moonfly-default": "moonfly",
    # Retired duplicate-ish themes remain accepted for old configs and muscle memory.
    "midnight": "moonfly",
    "future": "synthwave",
    "cyber": "synthwave",
    "futuristic": "synthwave",
    "retro": "synthwave",
    "cute": "catppuccin",
    "gruvbox": "amber",
    "gruv": "amber",
    "gruvbox-dark": "amber",
    "rose-pine": "catppuccin",
    "rose": "catppuccin",
    "rosepine": "catppuccin",
    "rose-pine-moon": "catppuccin",
    "rp": "catppuccin",
    # new viral aliases
    "cat": "catppuccin",
    "catppuccin-mocha": "catppuccin",
    "mocha": "catppuccin",
    "tokyo": "tokyo-night",
    "tokyonight": "tokyo-night",
    "synth": "synthwave",
    "outrun": "synthwave",
    "vapor": "synthwave",
    "vaporwave": "synthwave",
    "80s": "synthwave",
    "neon": "synthwave",
}

_active_theme_name = "flowly"


def normalize_theme_name(name: str | None) -> str:
    key = (name or "").strip().lower().replace(" ", "-")
    return THEME_ALIASES.get(key, key)


def list_themes() -> Iterable[FlowlyPalette]:
    return THEMES.values()


def get_theme(name: str | None) -> FlowlyPalette | None:
    key = normalize_theme_name(name)
    return THEMES.get(key)


def set_active_theme(name: str | None) -> FlowlyPalette:
    global _active_theme_name
    palette = get_theme(name) or FLOWLY_PALETTE
    _active_theme_name = palette.name
    return palette


def resolve_theme_name(explicit: str | None = None, state: dict | None = None) -> str:
    for raw in (
        explicit,
        os.environ.get("FLOWLY_TUI_THEME"),
        (state or {}).get("theme"),
    ):
        if not raw:
            continue
        palette = get_theme(str(raw))
        if palette is not None:
            return palette.name
    return FLOWLY_PALETTE.name


def get_palette() -> FlowlyPalette:
    return THEMES.get(_active_theme_name, FLOWLY_PALETTE)


def get_code_theme() -> str:
    return get_palette().code_theme


def css_for(palette: FlowlyPalette | None = None) -> str:
    """Generate global CSS for a Flowly TUI palette."""
    palette = palette or get_palette()
    return f"""
    Screen {{ background: {palette.bg}; }}
    Header {{ background: {palette.bg_alt}; color: {palette.accent}; }}
    HeaderTitle {{ color: {palette.text}; text-style: bold; }}

    TranscriptPane {{
        background: {palette.bg};
        scrollbar-background: {palette.bg};
        scrollbar-background-hover: {palette.bg_alt};
        scrollbar-color: {palette.border};
        scrollbar-color-hover: {palette.accent};
    }}

    Bubble {{
        background: transparent;
        color: {palette.text};
    }}
    Bubble.user      {{ border: none; background: {palette.boost}; }}
    Bubble.assistant {{ border: round {palette.assistant}; background: transparent; }}
    Bubble.system    {{ border: round {palette.system}; background: transparent; }}
    Bubble.slash     {{ border: round {palette.system}; background: transparent; }}
    Bubble.error     {{ border: round {palette.error}; background: transparent; }}
    Bubble > .bubble-body {{ background: transparent; color: {palette.text}; }}

    ToolLine          {{ background: {palette.bg_tool}; }}
    ToolLine.running  {{ color: {palette.warning}; border-left: thick {palette.warning}; }}
    ToolLine.ok       {{ color: {palette.success}; border-left: thick {palette.success}; }}
    ToolLine.fail     {{ color: {palette.error};   border-left: thick {palette.error}; }}

    Composer {{
        background: {palette.bg};
    }}
    Composer > #composer-input-row {{
        background: {palette.bg};
    }}
    Composer > #composer-input-row > #composer-prompt {{
        color: {palette.accent}; background: {palette.bg};
    }}
    Composer > #composer-input-row > _Editor {{
        background: {palette.bg}; color: {palette.text};
    }}
    Composer > #composer-hint {{
        background: {palette.bg}; color: {palette.text_muted};
    }}
    Composer.approval-open > #composer-input-row,
    Composer.secret-open > #composer-input-row,
    Composer.setup-open > #composer-input-row,
    Composer.review-open > #composer-input-row,
    Composer.approval-open > #composer-hint,
    Composer.secret-open > #composer-hint,
    Composer.setup-open > #composer-hint,
    Composer.review-open > #composer-hint {{
        display: none;
    }}
    Composer > #composer-attachments {{
        background: {palette.bg}; color: {palette.accent};
    }}
    Composer > #composer-approval {{
        background: {palette.bg_alt};
        color: {palette.text};
        border-left: solid {palette.accent};
    }}
    Composer > #composer-approval > #approval-title {{
        color: {palette.warning};
    }}
    Composer > #composer-approval > #approval-command {{
        color: {palette.text};
    }}
    Composer > #composer-approval > #approval-meta,
    Composer > #composer-approval > #approval-hint {{
        color: {palette.text_muted};
    }}
    Composer > #composer-approval > .approval-option {{
        color: {palette.text};
        background: {palette.bg_alt};
    }}
    Composer > #composer-approval > .approval-option.selected {{
        color: {palette.bg};
        background: {palette.accent};
        text-style: bold;
    }}
    Composer > #composer-secret {{
        background: {palette.bg};
    }}
    Composer > #composer-secret > #secret-title,
    Composer > #composer-secret > #secret-prefix {{
        color: {palette.accent};
    }}
    Composer > #composer-secret > #secret-label {{
        color: {palette.text};
    }}
    Composer > #composer-secret > #secret-input-row > #secret-value {{
        background: {palette.bg_alt};
        color: {palette.text};
    }}
    Composer > #composer-secret > #secret-error {{
        color: {palette.error};
    }}
    Composer > #composer-secret > #secret-hint {{
        color: {palette.text_muted};
    }}
    Composer > #composer-setup {{
        background: {palette.bg};
    }}
    Composer > #composer-setup > #setup-title,
    Composer > #composer-setup > #setup-prefix {{
        color: {palette.accent};
    }}
    Composer > #composer-setup > #setup-subtitle,
    Composer > #composer-setup > #setup-progress,
    Composer > #composer-setup > #setup-hint {{
        color: {palette.text_muted};
    }}
    Composer > #composer-setup > #setup-label {{
        color: {palette.text};
    }}
    Composer > #composer-setup > .setup-field {{
        color: {palette.text_muted};
    }}
    Composer > #composer-setup > .setup-field.complete {{
        color: {palette.system};
    }}
    Composer > #composer-setup > .setup-field.selected {{
        color: {palette.text};
        background: {palette.bg_alt};
        text-style: bold;
    }}
    Composer > #composer-setup > #setup-input-row > #setup-value {{
        background: {palette.bg_alt};
        color: {palette.text};
    }}
    Composer > #composer-setup > .setup-choice {{
        color: {palette.text_muted};
    }}
    Composer > #composer-setup > .setup-choice.selected {{
        color: {palette.accent};
        background: {palette.bg};
        text-style: bold;
    }}
    Composer > #composer-setup > #setup-error {{
        color: {palette.error};
    }}
    Composer:disabled > #composer-input-row > #composer-prompt,
    Composer:disabled > #composer-input-row > _Editor,
    Composer:disabled > #composer-hint {{
        color: {palette.system};
    }}
    Composer > .queued-row {{ color: {palette.warning}; background: {palette.bg}; }}
    Composer > .composer-rule {{ color: {palette.border}; background: {palette.bg}; }}
    Composer > OptionList {{ background: {palette.bg_alt}; color: {palette.text}; }}

    StatusBar {{ background: {palette.bg}; color: {palette.text}; }}
    _Sep {{ color: {palette.border}; }}
    _ProviderLabel {{ color: {palette.accent}; }}
    _ModelLabel {{ color: {palette.accent_soft}; }}
    _HeaderTokenBar {{ background: {palette.bg_alt}; }}
    _SessionClock, _CwdLabel, _CostBadge, _BgBadge {{ color: {palette.text_muted}; }}

    SubagentPane {{
        background: {palette.bg_alt};
        border-left: solid {palette.border};
    }}
    SubagentPane > .pane-title {{ color: {palette.accent}; }}
    SubagentRow.running {{ color: {palette.warning}; }}
    SubagentRow.ok      {{ color: {palette.success}; }}
    SubagentRow.fail    {{ color: {palette.error}; }}

    /* Every modal renders as a composer-adjacent bottom sheet. Targeting the
       ModalScreen BASE class (Textual type selectors match base classes) means
       a newly-added modal is inline automatically — no hand-maintained list to
       fall out of sync (which is how /usage first shipped centered). */
    ModalScreen {{
        align: center bottom;
        /* No dimming overlay — modals read as composer-adjacent inline panels,
           not full-screen popups. (Textual's ModalScreen defaults to a 60%
           scrim; transparent removes it.) */
        background: transparent;
    }}

    ModalScreen > Vertical {{
        background: {palette.surface};
        border: thick {palette.accent};
        margin-bottom: 5;
    }}

    ApprovalModal .title,
    ActivityModal .title,
    ApprovalsModal .title,
    ArtifactsModal .title,
    AssistantPicker .title,
    BrowserModal .title,
    ConfirmModal .title,
    HelpModal Markdown,
    IntegrationSetupModal .title,
    IntegrationsModal .title,
    LoginModal .title,
    MCPModal .title,
    MCPSecretModal .title,
    ModelPicker .title,
    PluginsModal .title,
    PolicyModal .title,
    ProviderPicker .title,
    SessionPicker .title,
    SubagentModelsModal .title,
    ThemePicker .title,
    _SpecialistModelPicker .title {{
        color: {palette.accent};
    }}

    ConfirmModal .body {{
        color: {palette.text};
    }}

    ApprovalModal .cmd,
    IntegrationSetupModal .account-block,
    LoginModal .code-box,
    ApprovalsModal ListItem {{
        background: {palette.boost};
        color: {palette.text};
    }}

    AssistantPicker .hint,
    AssistantPicker .footnote,
    BrowserModal .description,
    BrowserModal #status-line,
    ConfirmModal .hint,
    MCPModal .hint,
    MCPModal .footer,
    MCPSecretModal .hint,
    ModelPicker .hint,
    ModelPicker .footer,
    PluginsModal .hint,
    PluginsModal .footer,
    PolicyModal .hint,
    ProviderPicker .hint,
    ProviderPicker .footer,
    SessionPicker .hint,
    SubagentModelsModal .hint,
    SubagentModelsModal .footer,
    _SpecialistModelPicker .hint,
    _SpecialistModelPicker .footer,
    IntegrationSetupModal .field-row > Label,
    IntegrationSetupModal .field-help,
    IntegrationSetupModal #status-line,
    IntegrationsModal .hint,
    IntegrationsModal .footer,
    LoginModal .hint,
    LoginModal .status,
    ActivityModal .meta,
    ActivityModal .hint,
    ArtifactsModal .hint,
    ApprovalsModal .hint,
    ApprovalsModal .session,
    ThemePicker .hint {{
        color: {palette.text_muted};
    }}

    AssistantPicker OptionList,
    ArtifactsModal ListView,
    IntegrationSetupModal VerticalScroll,
    IntegrationsModal OptionList,
    MCPModal OptionList,
    ModelPicker OptionList,
    PluginsModal OptionList,
    ProviderPicker OptionList,
    SessionPicker OptionList,
    SubagentModelsModal OptionList,
    ThemePicker OptionList,
    _SpecialistModelPicker OptionList,
    HelpModal VerticalScroll,
    HelpModal Markdown {{
        background: {palette.surface};
    }}

    ActivityModal DataTable {{
        background: transparent;
    }}

    ArtifactsModal VerticalScroll {{
        background: {palette.bg_alt};
    }}

    ArtifactsModal ListView {{
        border-right: solid {palette.border};
    }}

    HelpHint {{
        background: {palette.surface};
        border: round {palette.accent};
    }}
    """
