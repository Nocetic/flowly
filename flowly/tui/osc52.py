"""OSC 52 terminal clipboard write — works in tmux/SSH/iTerm/kitty/etc."""

from __future__ import annotations

import base64
import sys


def copy_to_clipboard(text: str) -> None:
    """Emit the OSC 52 escape sequence that puts ``text`` into the terminal's
    system clipboard. No-op if stdout is not a TTY.
    """
    if not sys.stdout.isatty():
        return
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # OSC 52 ; c ; <base64-text> BEL — 'c' = system clipboard
    sys.stdout.write(f"\x1b]52;c;{payload}\x07")
    sys.stdout.flush()
