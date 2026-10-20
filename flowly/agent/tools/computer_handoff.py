"""Computer handoff — a step the person does on the agent's screen (browser_tab ``handoff``).

Signing in, a two-factor or verification code, a CAPTCHA, approving a
payment: the agent hands the screen to the person and ends its reply. The
request travels like a clarify question (``kind="computer"``), so every
surface that shows the agent's questions shows it in the conversation as a
card that opens the screen. Nothing waits inside the agent's turn: a watcher
of the screen's own control record runs on, and once the person has taken
control and handed it back (from whichever client), the conversation gets a
new turn of its own in which the agent looks at the page again and goes on.
Nobody in a bound: the agent is told, once, and the card closes.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.clarify.manager import get_clarify_manager
from flowly.clarify.types import ClarifyRequest

# Long enough to find a phone for a code, short enough not to hold the screen.
TIMEOUT_SECONDS = 15 * 60
# Answers the screen's record gives; nothing a person can type.
_DONE = "\x00computer-done"
_GONE = "\x00computer-gone"
_POLL_SECONDS = 0.5

# What the agent is told to say in its conversation when the step is over.
CONTINUE_NOTICE = (
    "[Screen] The person did this on your screen and handed it back: {task}\n"
    "Look at the page again (read_page, or a screenshot) and continue the task from there. "
    "Do not ask them to do it again."
)
NOT_DONE_NOTICE = (
    "[Screen] Nobody did this on your screen in time: {task}\n"
    "Tell the person in one short sentence that the step was not done and the task is paused; "
    "do not retry it and do not ask for passwords or codes in chat."
)

Continuation = Callable[[str, str, str, str], Awaitable[None]]

# One watcher per conversation: a new handoff replaces the one before it.
_watchers: dict[str, asyncio.Task] = {}


def _result(task: str, outcome: str, note: str, **extra: Any) -> str:
    import json

    return json.dumps({"task": task, "outcome": outcome, **extra, "note": note})


def _snapshot(authority) -> dict | None:
    try:
        return authority.snapshot()
    except Exception:
        return None


async def _watch_control(authority, generation: str, manager, request_id: str, start: dict) -> None:
    """Resolve the request once the person took the screen and gave it back.

    Read from the screen's control record (any client's release counts). The
    epoch moves on every takeover and release: from agent control, a takeover
    and a release are two steps; from the person already holding it, one.
    ``start`` is the record as it was when the card was made, so a hand-back
    right after it is not missed.
    """
    needed = start["epoch"] + (1 if start["control"] != "agent" else 2)
    while True:
        await asyncio.sleep(_POLL_SECONDS)
        state = await asyncio.to_thread(_snapshot, authority)
        if not state or not state["running"] or state["generation"] != generation:
            manager.resolve(request_id, _GONE)
            return
        if state["control"] == "agent" and state["epoch"] >= needed:
            manager.resolve(request_id, _DONE)
            return


async def _attend(pending: ClarifyRequest, authority, generation: str, start: dict, task: str,
                  channel: str, chat_id: str, continuation: Continuation | None) -> None:
    """Wait for the card's outcome and start the conversation's next turn."""
    manager = get_clarify_manager()
    watcher = asyncio.create_task(_watch_control(authority, generation, manager, pending.id, start))
    try:
        answer = await manager.request_and_wait(pending)
    except Exception as exc:
        logger.error("[Computer] handoff request failed ({})", type(exc).__name__)
        answer = None
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        if _watchers.get(pending.session_key or "") is asyncio.current_task():
            _watchers.pop(pending.session_key or "", None)
    if answer == _GONE:
        logger.info("[Computer] the screen stopped before the person did: {}", task)
        return
    if continuation is None:
        return
    notice = (CONTINUE_NOTICE if answer == _DONE else NOT_DONE_NOTICE).format(task=task)
    try:
        await continuation(pending.session_key or "", channel, chat_id, notice)
    except Exception as exc:
        logger.error("[Computer] could not continue the task ({})", type(exc).__name__)


async def computer_handoff(task: Any, channel: str = "", chat_id: str = "",
                           continuation: Continuation | None = None) -> str:
    """browser_tab ``handoff``: hand the screen to the person for ``task``.

    An action of the browser tool rather than a tool of its own: every tool's
    schema rides every turn, and this adds one word and one argument. The
    conversation is the screen run's own, never the model's to name.
    """
    import json

    task = " ".join(str(task or "").split())[:300]
    if not task:
        return json.dumps({"error": "task is required for handoff", "error_code": "INVALID_PARAMS"})

    from flowly.agent.loop import SCREEN_VIEWER_SESSION_PREFIXES
    from flowly.desktop_runtime.authority import ScreenError
    from flowly.desktop_runtime.host_screen_run import current_screen_run, unavailable

    run = current_screen_run()
    if run is None or not run.uses_screen():
        return _result(task, "unavailable", "There is no screen in this task. Say what is needed instead.")
    session_key = run.session_key or ""
    if not session_key.startswith(SCREEN_VIEWER_SESSION_PREFIXES):
        return _result(task, "unavailable",
                       "This conversation is not in Flowly Desktop, the only app where the person can take "
                       "control of your screen. Tell them they can finish it from Flowly Desktop's Computer "
                       "panel, then end your reply without waiting or retrying.")
    try:
        identity = await asyncio.to_thread(run.gateway._host_screen_checkpoint_identity)
        _, provider = run.gateway._get_host_screen_browser()
        state = await asyncio.to_thread(provider.runtime.status)
    except (ScreenError, OSError, KeyError, AttributeError) as exc:
        code = exc.code if isinstance(exc, ScreenError) else "SCREEN_NOT_RUNNING"
        return json.dumps({"task": task, **unavailable(ScreenError(code))})
    if not state or state.get("phase") != "running":
        return json.dumps({"task": task, **unavailable(ScreenError("SCREEN_NOT_RUNNING"))})
    start = await asyncio.to_thread(_snapshot, provider.runtime.authority)
    if not start or not start["running"] or start["generation"] != state["generation"]:
        return json.dumps({"task": task, **unavailable(ScreenError("SCREEN_NOT_RUNNING"))})

    now = time.time()
    pending = ClarifyRequest(
        id=secrets.token_hex(8), question=task, choices=None, session_key=session_key,
        created_at=now, expires_at=now + TIMEOUT_SECONDS, kind="computer", profile_id=identity["profileId"],
    )
    previous = _watchers.get(session_key)
    if previous is not None and not previous.done():
        previous.cancel()
    _watchers[session_key] = asyncio.create_task(_attend(
        pending, provider.runtime.authority, state["generation"], start, task, channel, chat_id, continuation))
    return _result(task, "handed_off",
                   "The person has the card. End your reply now with one short sentence: what they should "
                   "do on the screen, and that you will continue once they hand it back. Do not wait, poll "
                   "the screen, or retry; the task continues in a new turn when they are done.")


def _reset_for_tests() -> None:
    for task in _watchers.values():
        task.cancel()
    _watchers.clear()
