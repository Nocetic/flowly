"""Constants for the floating-pet feature.

The frame geometry and animation-state names mirror the Petdex sprite/manifest
format so downloaded spritesheets line up row-for-row. The scale clamp is ours.
"""

from __future__ import annotations

# Petdex spritesheet frame geometry, in pixels.
FRAME_WIDTH = 192
FRAME_HEIGHT = 208

# Animation timing/sizing defaults.
DEFAULT_LOOP_MS = 1100
DEFAULT_SCALE = 0.33
SCALE_MIN = 0.1
SCALE_MAX = 3.0

# Recognised animation states. Which rows a given spritesheet actually provides
# is data-driven (see sprites.py); this is the vocabulary we map onto.
PET_STATES: tuple[str, ...] = (
    "idle",
    "wave",
    "run",
    "failed",
    "review",
    "jump",
    "waiting",
)
DEFAULT_STATE = "idle"

# ── canonical row taxonomies ─────────────────────────────────────────────────
# A Petdex spritesheet is NOT one-row-per-state in our PET_STATES order — it
# follows a fixed physical row layout that we map our state vocabulary onto.
# Two layouts exist in the wild, distinguished by row count.

# Current Petdex atlas: 1536x1872 = 8 columns x 9 rows of 192x208 cells.
# Row order, top -> bottom (running-right/-left are directional variants we
# don't drive; our "run" maps to the canonical "running" row).
PETDEX_ROW_ORDER: tuple[str, ...] = (
    "idle",           # 0
    "running-right",  # 1
    "running-left",   # 2
    "waving",         # 3
    "jumping",        # 4
    "failed",         # 5
    "waiting",        # 6
    "running",        # 7
    "review",         # 8
)

# Older 8-row atlases. "waiting" is absent here and falls back to idle.
LEGACY_ROW_ORDER: tuple[str, ...] = (
    "idle",
    "wave",
    "run",
    "failed",
    "review",
    "jump",
    "extra1",
    "extra2",
)

# Our stable state names -> accepted row-name aliases, descending preference.
# Keeps our internal vocabulary (wave/jump/run) stable across both layouts.
STATE_ALIASES: dict[str, tuple[str, ...]] = {
    "idle": ("idle",),
    "wave": ("wave", "waving"),
    "run": ("run", "running"),
    "failed": ("failed",),
    "review": ("review",),
    "jump": ("jump", "jumping"),
    "waiting": ("waiting",),
}


def row_order_for(row_count: int) -> tuple[str, ...]:
    """Pick the row taxonomy for a sheet with *row_count* physical rows."""
    try:
        n = int(row_count or 0)
    except (TypeError, ValueError):
        n = 0
    return PETDEX_ROW_ORDER if n >= len(PETDEX_ROW_ORDER) else LEGACY_ROW_ORDER


def state_row_index(state: str, row_count: int) -> int:
    """Physical spritesheet row for *state* on a *row_count*-row sheet.

    Resolves through ``STATE_ALIASES`` against the layout chosen by row count;
    falls back to the idle row (0) when the state isn't present in that layout.
    """
    order = row_order_for(row_count)
    for alias in STATE_ALIASES.get(state, (state,)):
        try:
            return order.index(alias)
        except ValueError:
            continue
    return 0


def clamp_scale(value: float) -> float:
    """Clamp a scale multiplier into the supported ``[SCALE_MIN, SCALE_MAX]`` range."""
    return max(SCALE_MIN, min(SCALE_MAX, float(value)))
