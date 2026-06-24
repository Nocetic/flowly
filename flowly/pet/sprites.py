"""Spritesheet analysis: map rows onto animation states, trim blank frames.

A Petdex spritesheet is a grid of ``FRAME_WIDTH x FRAME_HEIGHT`` cells. Rows are
NOT one-per-state in our PET_STATES order — they follow a fixed physical layout
(see ``constants.PETDEX_ROW_ORDER`` / ``LEGACY_ROW_ORDER``) selected by row
count. We resolve each requested state to its physical row via that taxonomy,
then trim **trailing** blank (fully-transparent) frames to recover the real
frame count per state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowly.pet.constants import FRAME_HEIGHT, FRAME_WIDTH, state_row_index


def load_image(path: Path | str) -> Any:
    """Open a spritesheet as an RGBA PIL image (Pillow imported lazily)."""
    from PIL import Image

    return Image.open(path).convert("RGBA")


def _frame_has_content(image: Any, col: int, row: int, fw: int, fh: int) -> bool:
    box = (col * fw, row * fh, (col + 1) * fw, (row + 1) * fh)
    # getbbox() is None when the cropped region is fully zero (transparent).
    return image.crop(box).getbbox() is not None


def count_frames_in_row(
    image: Any, row: int, *, frame_w: int = FRAME_WIDTH, frame_h: int = FRAME_HEIGHT
) -> int:
    """Non-blank frame count in *row* — trailing blank frames are trimmed, but a
    blank frame between two non-blank frames still counts (it's part of the run)."""
    cols = max(0, image.width // frame_w)
    last = 0
    for c in range(cols):
        if _frame_has_content(image, c, row, frame_w, frame_h):
            last = c + 1
    return last


def analyze(
    image: Any, states: list[str], *, frame_w: int = FRAME_WIDTH, frame_h: int = FRAME_HEIGHT
) -> tuple[dict[str, int], dict[str, int]]:
    """Resolve each requested state to its physical spritesheet row.

    Returns ``(row_by_state, frames_by_state)``. The row is chosen by the
    canonical taxonomy for the sheet's row count (not the order of *states*), so
    e.g. ``run`` resolves to the ``running`` row even when it isn't row 2. A row
    with zero non-blank frames still maps with ``frames_by_state[state] == 0``.
    """
    rgba = image.convert("RGBA")
    rows = max(0, rgba.height // frame_h)
    row_by_state: dict[str, int] = {}
    frames_by_state: dict[str, int] = {}
    if rows == 0:
        return row_by_state, frames_by_state
    for state in states:
        idx = state_row_index(state, rows)
        if idx >= rows:  # taxonomy row missing on this (short) sheet → idle row
            idx = 0
        row_by_state[state] = idx
        frames_by_state[state] = count_frames_in_row(rgba, idx, frame_w=frame_w, frame_h=frame_h)
    return row_by_state, frames_by_state
