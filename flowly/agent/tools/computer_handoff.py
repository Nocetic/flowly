"""Computer handoff — a step the person does on the agent's screen (browser_tab ``handoff``).

Signing in, a two-factor or verification code, a CAPTCHA, approving a
payment: the agent hands the screen to the person and ends its reply. The
step is task state (``desktop_runtime/screen_steps.py``): one durable record
per conversation that outlives the turn, the window, this process and a
stopped screen. Every surface that shows the agent's questions shows it as a
card that opens the screen (the clarify wire, ``kind="computer"``, id = step
id); the card is a projection of the record and ends with its state.

A watcher per waiting step, rebuilt from the store when the gateway starts,
reads the screen's control record: once the person has taken control and
handed it back, the step is done and the conversation gets a new turn of its
own in which the agent looks at the page again and goes on. The agent can take
a step back (``withdraw``) when the person gives up; a later step in the same
conversation supersedes the one waiting; a step nobody does within the
horizon ends as not done, once.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.clarify.manager import get_clarify_manager
from flowly.clarify.types import ClarifyRequest
from flowly.desktop_runtime.screen_steps import HORIZON_SECONDS, ScreenSteps, normalized_task

# Answers the screen's record gives; nothing a person can type.
_DONE = "\x00computer-done"
_WITHDRAWN = "\x00computer-withdrawn"
_SUPERSEDED = "\x00computer-superseded"
_POLL_SECONDS = 1.0

# What the agent is told to say in its conversation when the step is over.
CONTINUE_NOTICE = (
    "[Screen] The person did this on your screen and handed it back: {task}\n"
    "Look at the page again (read_page, or a screenshot) and continue the task from there. "
    "Do not ask them to do it again."
)
NOT_DONE_NOTICE = (
    "[Screen] Nobody did this on your screen: {task}\n"
    "Tell the person in one short sentence that the step was not done and the task is paused; "
    "do not retry it and do not ask for passwords or codes in chat."
)

Continuation = Callable[[str, str, str, str], Awaitable[None]]

# The watcher of each waiting step, by step id.
_watchers: dict[str, asyncio.Task] = {}


def _result(task: str, outcome: str, note: str, **extra: Any) -> str:
    import json

    # The step is in the person's language: as written, not \u escapes the model
    # pays tokens for and the person sees in the tool panel.
    return json.dumps({"task": task, "outcome": outcome, **extra, "note": note}, ensure_ascii=False)


def _store(gateway) -> tuple[ScreenSteps, Any]:
    """The step store and browser provider of this gateway's screen."""
    _, provider = gateway._get_host_screen_browser()
    return ScreenSteps(provider.runtime.directory), provider


def _snapshot(authority) -> dict | None:
    try:
        return authority.snapshot()
    except Exception:
        return None


def _target(state: dict) -> int:
    """The control epoch at which the person has taken this screen and handed
    it back: a takeover and a release each move the epoch by one; from a screen
    they already hold, the release alone."""
    return state["epoch"] + (1 if state["control"] != "agent" else 2)


async def _watch_control(step: dict, store: ScreenSteps, provider, manager) -> None:
    """Resolve the step once the person has taken the screen and given it back.

    The screen's own control record is read every second (any client's
    takeover and release count), and the decision is the epoch mark kept in the
    store, so a takeover and release between two reads are not missed and a
    restarted watcher decides from the same mark. A screen that stops keeps the
    step waiting (opening the computer starts it again) and gets a new mark.
    """
    generation, target, taken = step["watch_generation"], step["watch_target"], step["taken_generation"]
    while True:
        state = await asyncio.to_thread(_snapshot, provider.runtime.authority)
        if state and state["running"]:
            if state["generation"] != generation or target is None:
                generation, target = state["generation"], _target(state)
                await asyncio.to_thread(store.watch, step["id"], generation, target)
            if state["control"] != "agent" and taken != generation:
                taken = generation
                await asyncio.to_thread(store.mark_taken, step["id"], generation)
            elif state["control"] == "agent" and state["epoch"] >= target:
                manager.resolve(step["id"], _DONE, reason="done")
                return
        await asyncio.sleep(_POLL_SECONDS)


async def _attend(step: dict, store: ScreenSteps, provider, continuation: Continuation | None) -> None:
    """Show the card, wait for the step's end, and start the conversation's next turn."""
    manager = get_clarify_manager()
    pending = ClarifyRequest(
        id=step["id"], question=step["task"], choices=None, session_key=step["session_key"],
        created_at=step["created_at"], expires_at=step["created_at"] + HORIZON_SECONDS, kind="computer",
        profile_id=step["profile_id"],
    )
    watcher = asyncio.create_task(_watch_control(step, store, provider, manager))
    answer: str | None = None
    try:
        answer = await manager.request_and_wait(pending)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("[Computer] step {} could not be shown ({})", step["id"], type(exc).__name__)
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        if _watchers.get(step["id"]) is asyncio.current_task():
            _watchers.pop(step["id"], None)
    if answer in (_WITHDRAWN, _SUPERSEDED):
        return
    state = "done" if answer == _DONE else "not_done"
    ended = await asyncio.to_thread(store.resolve, step["id"], state)
    if ended is None or continuation is None:
        return
    notice = (CONTINUE_NOTICE if state == "done" else NOT_DONE_NOTICE).format(task=step["task"])
    try:
        await continuation(step["session_key"], step["channel"], step["chat_id"], notice)
    except Exception as exc:
        logger.error("[Computer] could not continue the task ({})", type(exc).__name__)


def _start(step: dict, store: ScreenSteps, provider, continuation: Continuation | None) -> None:
    previous = _watchers.get(step["id"])
    if previous is not None and not previous.done():
        return
    _watchers[step["id"]] = asyncio.create_task(_attend(step, store, provider, continuation))


def _end_card(step_id: str, answer: str, reason: str) -> None:
    """End a step's card with its state; the watcher, if any, goes with it."""
    if not get_clarify_manager().resolve(step_id, answer, reason=reason):
        watcher = _watchers.pop(step_id, None)
        if watcher is not None and not watcher.done():
            watcher.cancel()


def recover(gateway, continuation: Continuation | None) -> int:
    """Rebuild the watcher of every waiting step when the gateway starts.

    Harmless where there is no screen: no store, no steps, nothing to do.
    """
    try:
        store, provider = _store(gateway)
        waiting = store.all_waiting()
    except Exception as exc:
        logger.debug("[Computer] no steps to recover ({})", type(exc).__name__)
        return 0
    for step in waiting:
        _start(step, store, provider, continuation)
    if waiting:
        logger.info("[Computer] {} screen step(s) waiting from before this start", len(waiting))
    return len(waiting)


def waiting_step(gateway, session_key: str) -> dict | None:
    """The conversation's waiting step, for the turn's context; None without one."""
    if gateway is None or not session_key:
        return None
    try:
        store, _ = _store(gateway)
        return store.waiting(session_key)
    except Exception:
        return None


def recent_steps(directory, session_key: str) -> list[dict]:
    """The conversation's steps as clients see them (``chat.inflight``)."""
    try:
        if not (directory / "steps.sqlite3").is_file():
            return []  # a read never makes the store
        return ScreenSteps(directory).recent(session_key)
    except Exception:
        return []


async def computer_handoff(task: Any, channel: str = "", chat_id: str = "",
                           continuation: Continuation | None = None) -> str:
    """browser_tab ``handoff``: hand the screen to the person for ``task``.

    An action of the browser tool rather than a tool of its own: every tool's
    schema rides every turn, and this adds one word and one argument. The
    conversation is the screen run's own, never the model's to name.
    """
    import json

    task = normalized_task(task)
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
        store, provider = _store(run.gateway)
        current = await asyncio.to_thread(store.waiting, session_key)
    except (ScreenError, OSError, KeyError, AttributeError) as exc:
        code = exc.code if isinstance(exc, ScreenError) else "SCREEN_NOT_RUNNING"
        return json.dumps({"task": task, **unavailable(ScreenError(code))}, ensure_ascii=False)
    if current is not None and current["task"] == task:
        _start(current, store, provider, continuation)  # a watcher, should this process be new
        return _result(task, "already_waiting",
                       "This step is already waiting for the person; the card is in the conversation. Do not "
                       "hand it over again. Answer what they said in one short sentence and end your reply; "
                       "the task continues in a new turn when they are done.", stepId=current["id"])
    step, superseded = await asyncio.to_thread(
        store.open, {key: identity[key] for key in ("hostId", "botId", "profileId")}, session_key, channel, chat_id, task)
    if superseded is not None:
        _end_card(superseded["id"], _SUPERSEDED, "superseded")
    state = await asyncio.to_thread(_snapshot, provider.runtime.authority)
    if state and state["running"]:
        # The mark is set now, so a takeover and hand-back before the first read count.
        step = await asyncio.to_thread(store.watch, step["id"], state["generation"], _target(state)) or step
        if state["control"] != "agent":
            step = await asyncio.to_thread(store.mark_taken, step["id"], state["generation"]) or step
    _start(step, store, provider, continuation)
    return _result(task, "handed_off",
                   "The person has the card. End your reply now with one short sentence: what they should "
                   "do on the screen, and that you will continue once they hand it back. Do not wait, poll "
                   "the screen, or retry; the task continues in a new turn when they are done.", stepId=step["id"])


async def withdraw_handoff() -> str:
    """browser_tab ``withdraw``: take back the step waiting in this conversation."""
    import json

    from flowly.desktop_runtime.host_screen_run import current_screen_run

    nothing = json.dumps({"outcome": "nothing_waiting", "note": "No screen step is waiting in this conversation."})
    run = current_screen_run()
    if run is None:
        return nothing
    try:
        store, _ = _store(run.gateway)
        current = await asyncio.to_thread(store.waiting, run.session_key or "")
    except Exception:
        current = None
    if current is None:
        return nothing
    await asyncio.to_thread(store.resolve, current["id"], "withdrawn")
    _end_card(current["id"], _WITHDRAWN, "withdrawn")
    return _result(current["task"], "withdrawn",
                   "The step is withdrawn and its card says so. Tell the person in one short sentence and end "
                   "your reply; do not retry the step.", stepId=current["id"])


def _reset_for_tests() -> None:
    for task in _watchers.values():
        task.cancel()
    _watchers.clear()
