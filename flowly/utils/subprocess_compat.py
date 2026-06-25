"""Cross-platform subprocess spawn kwargs (POSIX ⇄ Windows).

POSIX uses ``start_new_session=True`` (``setsid``) to put a child in its own
session/process-group so it survives the parent and is isolated from terminal
signals (Ctrl+C). On Windows ``start_new_session`` is silently ignored, so we
translate the intent to the right ``creationflags``.

Two helpers, split by purpose — the distinction matters and is easy to get
wrong:

* :func:`hide_window_kwargs` — only suppresses a console-window flash on
  Windows (``CREATE_NO_WINDOW``). It does **not** detach and does **not** sever
  stdio, so ``stdout``/``stderr`` capture keeps working. **This is the only
  variant safe for captured and/or asyncio subprocesses.**

* :func:`detach_kwargs` — fully detaches a fire-and-forget child that we do not
  capture (e.g. opening a URL in the system browser). On Windows it adds
  ``DETACHED_PROCESS`` (which *severs* stdio — fine when stdio is ``DEVNULL``,
  fatal if you wanted capture).

Deliberately **never** ``CREATE_NEW_PROCESS_GROUP``: on Windows + Python 3.11 it
interacts with asyncio's ProactorEventLoop such that subprocess creation cancels
the running loop task, surfacing as ``KeyboardInterrupt`` and tearing the
process down mid-turn. ``DETACHED_PROCESS`` already gives a detached child its
own group, so the flag buys nothing and only risks that footgun.

On POSIX both helpers return exactly the kwargs the code used before this module
existed, so macOS/Linux behavior is byte-identical. Windows-only constants are
referenced only inside the ``os.name == "nt"`` branch (and via ``getattr(...,
0)``), so importing/using this on POSIX can never ``AttributeError``.
"""

from __future__ import annotations

import os
import subprocess

__all__ = ["hide_window_kwargs", "detach_kwargs"]


def hide_window_kwargs() -> dict:
    """Spawn kwargs that hide a console flash on Windows without detaching.

    Safe for captured stdio and asyncio subprocesses. ``{}`` on POSIX.
    """
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def detach_kwargs() -> dict:
    """Spawn kwargs that fully detach a fire-and-forget (uncaptured) child.

    POSIX: ``start_new_session=True`` (unchanged from before). Windows:
    ``CREATE_NO_WINDOW | DETACHED_PROCESS``. Do **not** use for children whose
    stdout/stderr you capture — ``DETACHED_PROCESS`` severs stdio.
    """
    if os.name != "nt":
        return {"start_new_session": True}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    }
