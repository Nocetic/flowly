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


def clamp_scale(value: float) -> float:
    """Clamp a scale multiplier into the supported ``[SCALE_MIN, SCALE_MAX]`` range."""
    return max(SCALE_MIN, min(SCALE_MAX, float(value)))
