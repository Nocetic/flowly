"""Computer handoff — a step the person does on the agent's screen (browser_tab ``handoff``).

Signing in, a two-factor or verification code, a CAPTCHA, approving a
payment: the agent stops and asks the person to take control of its screen,
do the step and give control back; this same run then continues. The request
travels like a clarify question (``kind="computer"``), so every surface that
shows the agent's questions shows it, in the conversation, as a card that
opens the screen. It is done when the screen's own handoff record says the
person took control and handed it back (whichever client they used), and it
stops waiting after a bound.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from loguru import logger

from flowly.clarify.manager import get_clarify_manager
from flowly.clarify.types import ClarifyRequest

# Long enough to find a phone for a code, short enough not to hold the screen.
TIMEOUT_SECONDS = 15 * 60
# The answer the screen's own record gives; nothing a person can type.
_DONE = "\x00computer-done"
_POLL_SECONDS = 0.5


def _result(task: str, outcome: str, note: str, **extra: Any) -> str:
    return json.dumps({"task": task, "outcome": outcome, **extra, "note": note})


async def _watch_handoff(checkpoint, manager, request_id: str, state: dict) -> None:
    """Resolve the request once the person took the screen and gave it back.

    Reads the run's durable handoff record and never waits in a way that can
    be cancelled half-way (``checkpoint.wait`` would mark the run cancelled):
    it calls it only once the person asked the run to continue, when it only
    completes the run's fresh look at the page.
    """
    seen = False
    while True:
        status = await asyncio.to_thread(checkpoint.store.checkpoint, checkpoint.execution)
        phase = status["state"]
        if phase in {"waiting_human", "waiting_resume"}:
            seen = True
        elif phase in {"resume_requested", "refreshing"} or (seen and phase == "ready"):
            state["refreshing"] = True
            try:
                await checkpoint.wait()
                manager.resolve(request_id, _DONE)
            finally:
                state["refreshing"] = False
            return
        elif phase in {"finished", "cancelled"}:
            return
        await asyncio.sleep(_POLL_SECONDS)


async def computer_handoff(task: Any) -> str:
    """browser_tab ``handoff``: wait for the person to do ``task`` on the screen.

    An action of the browser tool rather than a tool of its own: every tool's
    schema rides every turn, and this adds one word and one argument.
    """
    task = " ".join(str(task or "").split())[:300]
    if not task:
        return json.dumps({"error": "task is required for handoff", "error_code": "INVALID_PARAMS"})

    from flowly.agent.loop import SCREEN_VIEWER_SESSION_PREFIXES
    from flowly.desktop_runtime.authority import ScreenError
    from flowly.desktop_runtime.host_screen_run import current_screen_run, unavailable

    run = current_screen_run()
    if run is None or not run.uses_screen():
        return _result(task, "unavailable", "There is no screen in this task. Say what is needed instead.")
    # The run's own conversation (set by the runtime, never by the model).
    session_key = run.session_key or ""
    if not session_key.startswith(SCREEN_VIEWER_SESSION_PREFIXES):
        return _result(task, "unavailable",
                       "This conversation is not in Flowly Desktop, the only app where the person can take "
                       "control of your screen. Tell them they can finish it from Flowly Desktop's Computer "
                       "panel, then end your reply without waiting or retrying.")
    try:
        binding = await run.bind()
    except ScreenError as exc:
        return json.dumps({"task": task, **unavailable(exc)})
    if binding is None or binding.checkpoint is None:
        return json.dumps({"task": task, **unavailable(ScreenError("SCREEN_NOT_RUNNING"))})
    try:
        identity = await asyncio.to_thread(run.gateway._host_screen_checkpoint_identity)
        profile_id = identity["profileId"]
    except (ScreenError, OSError, KeyError):
        profile_id = None

    now = time.time()
    pending = ClarifyRequest(
        id=secrets.token_hex(8), question=task, choices=None, session_key=session_key,
        created_at=now, expires_at=now + TIMEOUT_SECONDS, kind="computer", profile_id=profile_id,
    )
    manager = get_clarify_manager()
    state = {"refreshing": False}
    watcher = asyncio.create_task(_watch_handoff(binding.checkpoint, manager, pending.id, state))
    try:
        answer = await manager.request_and_wait(pending)
    except Exception as exc:
        logger.error("[Computer] handoff request failed ({})", type(exc).__name__)
        answer = None
    finally:
        if not watcher.done():
            if state["refreshing"]:
                # The person asked to continue: let the fresh look finish.
                await asyncio.shield(watcher)
            else:
                watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, ScreenError):
            pass
        except Exception as exc:
            logger.warning("[Computer] handoff watch failed ({})", type(exc).__name__)

    if answer == _DONE:
        return _result(task, "done", "The person did it on your screen and gave it back. Look at the "
                       "screen again (read_page or a screenshot) before you continue.")
    if answer is None:
        return _result(task, "not_done", "Nobody did it in time. Say so and end your reply; do not retry "
                       "the step or ask for the details in chat.")
    return _result(task, "answered", "The person answered instead of taking the screen. Follow what "
                   "they said; never ask for passwords or codes in chat.", user_response=answer)
