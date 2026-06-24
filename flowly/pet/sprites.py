"""Spritesheet analysis: map rows onto animation states, trim blank frames.

A Petdex spritesheet is a grid of ``FRAME_WIDTH x FRAME_HEIGHT`` cells: one row
per animation state, columns are that animation's frames. Rows are padded to a
fixed column count, so we trim **trailing** blank (fully-transparent) frames to
recover the real frame count per state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowly.pet.constants import FRAME_HEIGHT, FRAME_WIDTH


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
    """Map an ordered list of state names onto spritesheet rows.

    Returns ``(row_by_state, frames_by_state)``. States beyond the available rows
    are skipped. A row with zero non-blank frames is still mapped with
    ``frames_by_state[state] == 0`` so the caller can decide how to fall back.
    """
    rgba = image.convert("RGBA")
    rows = max(0, rgba.height // frame_h)
    row_by_state: dict[str, int] = {}
    frames_by_state: dict[str, int] = {}
    for idx, state in enumerate(states):
        if idx >= rows:
            break
        row_by_state[state] = idx
        frames_by_state[state] = count_frames_in_row(rgba, idx, frame_w=frame_w, frame_h=frame_h)
    return row_by_state, frames_by_state
