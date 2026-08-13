"""TUI clarify: the agent asks a question and BLOCKS on the answer.

Before this existed the gateway broadcast ``agent.clarify.requested`` into a
TUI that had no handler for it — the event fell through to the "unknown event"
bucket, no options were ever drawn, and the tool sat there until its 300s
timeout. These cover the whole path: client dispatch + RPC params, the inline
tray (choices, free text, dismiss), and the close event that retires a tray
answered elsewhere or timed out.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input

from flowly.tui.client import (
    ClarifyClosed,
    ClarifyRequested,
    GatewayClient,
)
from flowly.tui.panes.composer import (
    ClarifyPrompt,
    ClarifyPromptRequest,
    Composer,
    _ClarifyOptionRow,
)

HOSTILE = "[/dim] kötü [b]içerik [unknown]tag"


def _request(choices: tuple[str, ...] = ("Play jazz", "Play rock")) -> ClarifyPromptRequest:
    return ClarifyPromptRequest(
        id="cl_1",
        question=f"Which station? {HOSTILE}",
        choices=choices,
        session_key="cli:1",
        expires_at=9e12,
    )


# ── client: event dispatch ──────────────────────────────────────────────


def _bare_client() -> GatewayClient:
    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()
    client._pending = {}
    return client


@pytest.mark.asyncio
async def test_dispatch_clarify_requested():
    client = _bare_client()
    await client._dispatch({
        "type": "event",
        "event": "agent.clarify.requested",
        "data": {
            "id": "cl_1",
            "question": "Which station?",
            "choices": ["A", "B"],
            "sessionKey": "cli:1",
            "expiresAt": 9e12,
        },
    })
    ev = client._inbox.get_nowait()
    assert isinstance(ev, ClarifyRequested)
    assert ev.clarify_id == "cl_1"
    assert ev.question == "Which station?"
    assert ev.choices == ("A", "B")
    assert ev.session_key == "cli:1"


@pytest.mark.asyncio
async def test_dispatch_clarify_requested_without_choices():
    client = _bare_client()
    await client._dispatch({
        "type": "event",
        "event": "agent.clarify.requested",
        "data": {"id": "cl_2", "question": "What next?", "choices": None},
    })
    ev = client._inbox.get_nowait()
    assert isinstance(ev, ClarifyRequested)
    assert ev.choices == ()


@pytest.mark.asyncio
async def test_dispatch_clarify_closed():
    client = _bare_client()
    await client._dispatch({
        "type": "event",
        "event": "agent.clarify.closed",
        "data": {"id": "cl_1", "reason": "timeout", "sessionKey": "cli:1"},
    })
    ev = client._inbox.get_nowait()
    assert isinstance(ev, ClarifyClosed)
    assert ev.clarify_id == "cl_1"
    assert ev.reason == "timeout"


@pytest.mark.asyncio
async def test_the_gateway_payload_is_the_one_the_tui_reads(monkeypatch, tmp_path):
    """Contract: feed the client exactly what the gateway puts on the wire.

    The two sides are in different modules, so a rename on either (``sessionKey``
    → ``session_key``, ``agent.clarify.requested`` → anything else) silently
    lands the event in the "unknown event" bucket — which is the whole reason
    this tray never appeared.
    """
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    from unittest.mock import AsyncMock, MagicMock

    from flowly.gateway.server import GatewayServer

    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=AsyncMock())
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    server._ws_clients["c1"] = ws

    from flowly.clarify.types import ClarifyRequest as _Pending

    await server.broadcast_clarify_request(
        _Pending(
            id="cl_1", question="Which?", choices=["A", "B"],
            session_key="cli:1", created_at=0.0, expires_at=9e12,
        )
    )
    client = _bare_client()
    await client._dispatch(ws.send_json.await_args.args[0])
    ev = client._inbox.get_nowait()
    assert isinstance(ev, ClarifyRequested)
    assert (ev.clarify_id, ev.choices, ev.session_key) == ("cl_1", ("A", "B"), "cli:1")

    await server.broadcast_clarify_closed("cl_1", "answered", "cli:1")
    await client._dispatch(ws.send_json.await_args.args[0])
    ev = client._inbox.get_nowait()
    assert isinstance(ev, ClarifyClosed)
    assert (ev.clarify_id, ev.reason) == ("cl_1", "answered")


# ── client: RPC params ──────────────────────────────────────────────────


def _client_capturing(reply: dict[str, Any]):
    client = GatewayClient.__new__(GatewayClient)
    sent: dict[str, Any] = {}

    async def fake_rpc(method: str, params: dict[str, Any]) -> str:
        sent["method"] = method
        sent["params"] = params
        return "rid-1"

    async def fake_await_reply(rid: str, timeout: float = 5.0) -> dict[str, Any]:
        return reply

    client._rpc = fake_rpc  # type: ignore[method-assign]
    client._await_reply = fake_await_reply  # type: ignore[method-assign]
    return client, sent


@pytest.mark.asyncio
async def test_clarify_resolve_params():
    client, sent = _client_capturing({"ok": True})
    ok = await client.clarify_resolve("cl_1", "Play jazz")
    assert sent["method"] == "agent.clarify.resolve"
    assert sent["params"] == {"id": "cl_1", "answer": "Play jazz"}
    assert ok is True


@pytest.mark.asyncio
async def test_clarify_resolve_reports_gone():
    client, sent = _client_capturing({"error": {"code": "NOT_FOUND"}})
    assert await client.clarify_resolve("cl_1", "x") is False


@pytest.mark.asyncio
async def test_clarify_list_params():
    client, sent = _client_capturing({
        "clarifies": [{"id": "cl_1", "question": "Q", "choices": ["A"]}]
    })
    items = await client.clarify_list()
    assert sent["method"] == "agent.clarify.list"
    assert sent["params"] == {}
    assert items[0]["id"] == "cl_1"


# ── the inline tray ─────────────────────────────────────────────────────


class _Host(App):
    def compose(self) -> ComposeResult:
        yield ClarifyPrompt(id="composer-clarify")


def _answers(app_cls):
    """Run a host app that records Answered/Dismissed messages."""
    answers: list[tuple[str, str]] = []
    dismissed: list[bool] = []

    class _Catcher(app_cls):
        def on_clarify_prompt_answered(self, event: ClarifyPrompt.Answered) -> None:
            answers.append((event.clarify_id, event.answer))

        def on_clarify_prompt_dismissed(self, event: ClarifyPrompt.Dismissed) -> None:
            dismissed.append(True)

    return _Catcher, answers, dismissed


@pytest.mark.asyncio
async def test_tray_renders_question_and_choices():
    app = _Host()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request())
        await pilot.pause()
        visible = [r for r in app.query(_ClarifyOptionRow) if r.display]
        # two choices + the always-appended "type my own answer" row
        assert len(visible) == 3
        labels = " ".join(r.renderable_text for r in visible)
        assert "Play jazz" in labels and "Play rock" in labels
        # Hostile bracket content survives verbatim (never parsed as markup).
        assert "[/dim]" in tray.question_text


@pytest.mark.asyncio
async def test_digit_key_answers_with_choice_text():
    cls, answers, _ = _answers(_Host)
    app = cls()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request())
        await pilot.pause()
        assert tray.route_editor_key("2") is True
        await pilot.pause()
    assert answers == [("cl_1", "Play rock")]


@pytest.mark.asyncio
async def test_arrow_then_enter_answers():
    cls, answers, _ = _answers(_Host)
    app = cls()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request())
        await pilot.pause()
        tray.route_editor_key("down")
        tray.route_editor_key("enter")
        await pilot.pause()
    assert answers == [("cl_1", "Play rock")]


@pytest.mark.asyncio
async def test_own_answer_row_opens_input_and_submits():
    cls, answers, _ = _answers(_Host)
    app = cls()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request())
        await pilot.pause()
        assert tray.route_editor_key("3") is True  # "type my own answer"
        await pilot.pause()
        assert tray.answer_visible is True
        field = tray.query_one("#clarify-answer", Input)
        field.value = "something else entirely"
        field.post_message(Input.Submitted(field, field.value))
        await pilot.pause()
    assert answers == [("cl_1", "something else entirely")]


@pytest.mark.asyncio
async def test_open_ended_question_focuses_the_input():
    cls, answers, _ = _answers(_Host)
    app = cls()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request(choices=()))
        await pilot.pause()
        # No choice rows at all — straight into free text.
        assert [r for r in app.query(_ClarifyOptionRow) if r.display] == []
        assert tray.answer_visible is True
        field = tray.query_one("#clarify-answer", Input)
        field.value = "the blue one"
        field.post_message(Input.Submitted(field, field.value))
        await pilot.pause()
    assert answers == [("cl_1", "the blue one")]


@pytest.mark.asyncio
async def test_empty_answer_is_ignored():
    cls, answers, _ = _answers(_Host)
    app = cls()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request(choices=()))
        await pilot.pause()
        field = tray.query_one("#clarify-answer", Input)
        field.value = "   "
        field.post_message(Input.Submitted(field, field.value))
        await pilot.pause()
    assert answers == []


@pytest.mark.asyncio
async def test_escape_dismisses_without_answering():
    cls, answers, dismissed = _answers(_Host)
    app = cls()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request())
        await pilot.pause()
        assert tray.route_editor_key("escape") is True
        await pilot.pause()
    assert dismissed == [True]
    assert answers == []


@pytest.mark.asyncio
async def test_clear_request_drops_state():
    app = _Host()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request())
        await pilot.pause()
        tray.clear_request()
        await pilot.pause()
        assert tray.request is None
        assert [r for r in app.query(_ClarifyOptionRow) if r.display] == []


@pytest.mark.asyncio
async def test_choices_are_capped_at_max():
    """MAX_CHOICES is the contract; a rogue/legacy payload must not overflow
    the fixed slot rows."""
    app = _Host()
    async with app.run_test() as pilot:
        tray = app.query_one(ClarifyPrompt)
        tray.set_request(_request(choices=tuple(f"opt{i}" for i in range(9))))
        await pilot.pause()
        visible = [r for r in app.query(_ClarifyOptionRow) if r.display]
        from flowly.clarify.types import MAX_CHOICES

        assert len(visible) == MAX_CHOICES + 1  # + "type my own answer"


# ── composer wiring ─────────────────────────────────────────────────────


class _ComposerHost(App):
    def compose(self) -> ComposeResult:
        yield Composer()


@pytest.mark.asyncio
async def test_composer_opens_and_closes_the_tray():
    app = _ComposerHost()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        composer.show_clarify(_request())
        await pilot.pause()
        assert composer.has_class("clarify-open")
        assert (composer.clarify_request() or ClarifyPromptRequest("", "")).id == "cl_1"
        # The tray owns the keyboard while it's up.
        assert composer.route_clarify_key("down") is True
        composer.clear_clarify()
        await pilot.pause()
        assert not composer.has_class("clarify-open")
        assert composer.clarify_request() is None


@pytest.mark.asyncio
async def test_an_exec_approval_takes_the_surface_from_the_tray():
    """Both trays live in the composer; showing one must retire the other or
    two prompts stack on the same rows."""
    from flowly.tui.panes.composer import ApprovalPromptRequest

    app = _ComposerHost()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        composer.show_clarify(_request())
        await pilot.pause()
        composer.show_approval(
            ApprovalPromptRequest(request_id="a1", command="git push", reasons=[])
        )
        await pilot.pause()
        assert composer.has_class("approval-open")
        assert not composer.has_class("clarify-open")


# ── app wiring: the gap that made the tray never appear ─────────────────


class _FakeComposer:
    def __init__(self) -> None:
        self.shown: list[ClarifyPromptRequest] = []
        self.cleared = 0

    def show_clarify(self, request: ClarifyPromptRequest) -> None:
        self.shown.append(request)

    def clear_clarify(self) -> None:
        self.cleared += 1


class _FakeTranscript:
    def __init__(self) -> None:
        self.system: list[str] = []
        self.errors: list[str] = []

    def add_system(self, text: str) -> None:
        self.system.append(text)

    def add_error(self, text: str) -> None:
        self.errors.append(text)


def _app_with(session_key: str = "cli:1"):
    """A bare FlowlyTUI carrying only what the clarify handlers touch."""
    from flowly.tui.app import FlowlyTUI
    from flowly.tui.panes.composer import Composer as _Composer
    from flowly.tui.panes.transcript import TranscriptPane

    app = FlowlyTUI.__new__(FlowlyTUI)
    app._session_key = session_key
    app._pending_clarify = None
    app._clarify_resolve_busy = False
    app._approval_active = False
    app._current_bubble = None
    composer, transcript = _FakeComposer(), _FakeTranscript()

    def _query_one(selector, kind=None):
        if selector is _Composer:
            return composer
        if selector is TranscriptPane:
            return transcript
        raise AssertionError(f"unexpected query_one({selector!r})")

    app.query_one = _query_one  # type: ignore[method-assign]
    return app, composer, transcript


def _event(session_key: str = "cli:1", clarify_id: str = "cl_1") -> ClarifyRequested:
    return ClarifyRequested(
        clarify_id=clarify_id,
        question="Which station?",
        choices=("A", "B"),
        session_key=session_key,
        expires_at=9e12,
    )


def test_requested_event_opens_the_tray():
    app, composer, _ = _app_with()
    app._on_clarify_requested(_event())
    assert [r.id for r in composer.shown] == ["cl_1"]
    assert app._pending_clarify is not None


def test_requested_event_for_another_session_is_ignored():
    app, composer, _ = _app_with(session_key="cli:1")
    app._on_clarify_requested(_event(session_key="web:9"))
    assert composer.shown == []
    assert app._pending_clarify is None


def test_requested_event_without_session_key_is_ours():
    """A run with no session binding still has a human in front of it."""
    app, composer, _ = _app_with()
    app._on_clarify_requested(_event(session_key=""))
    assert len(composer.shown) == 1


def test_requested_event_is_deferred_behind_an_exec_approval():
    app, composer, _ = _app_with()
    app._approval_active = True
    app._on_clarify_requested(_event())
    assert composer.shown == []          # not drawn over the approval…
    assert app._pending_clarify is not None  # …but remembered for the drain tail


def test_closed_event_retires_the_matching_tray():
    app, composer, transcript = _app_with()
    app._on_clarify_requested(_event())
    app._on_clarify_closed(ClarifyClosed("cl_1", "timeout", "cli:1"))
    assert composer.cleared == 1
    assert app._pending_clarify is None
    assert any("timed out" in s for s in transcript.system)


def test_closed_event_for_a_different_question_leaves_the_tray():
    app, composer, _ = _app_with()
    app._on_clarify_requested(_event())
    app._on_clarify_closed(ClarifyClosed("cl_other", "answered", "cli:1"))
    assert composer.cleared == 0
    assert app._pending_clarify is not None


@pytest.mark.asyncio
async def test_answering_resolves_over_rpc_and_clears():
    app, composer, transcript = _app_with()
    app._on_clarify_requested(_event())
    sent: list[tuple[str, str]] = []

    class _Client:
        async def clarify_resolve(self, clarify_id: str, answer: str) -> bool:
            sent.append((clarify_id, answer))
            return True

    app._client = _Client()
    await app._on_clarify_answered(_StopMessage("cl_1", "A"))
    assert sent == [("cl_1", "A")]
    assert composer.cleared == 1
    assert app._pending_clarify is None


@pytest.mark.asyncio
async def test_our_own_answer_is_not_reported_as_someone_elses():
    """``closed`` races the resolve reply on the same socket. Landing first
    must not turn our own answer into "answered on another device"."""
    app, composer, transcript = _app_with()
    app._on_clarify_requested(_event())

    class _Client:
        async def clarify_resolve(self, clarify_id: str, answer: str) -> bool:
            app._on_clarify_closed(ClarifyClosed(clarify_id, "answered", "cli:1"))
            return True

    app._client = _Client()
    await app._on_clarify_answered(_StopMessage("cl_1", "A"))
    assert not any("another device" in s for s in transcript.system)
    assert transcript.system == ["↩ A"]


@pytest.mark.asyncio
async def test_a_failed_delivery_keeps_the_question_pending():
    app, composer, transcript = _app_with()
    app._on_clarify_requested(_event())

    class _Client:
        async def clarify_resolve(self, clarify_id: str, answer: str) -> bool:
            raise RuntimeError("socket died")

    app._client = _Client()
    await app._on_clarify_answered(_StopMessage("cl_1", "A"))
    assert transcript.errors and "socket died" in transcript.errors[0]
    # /clarify can still bring it back — the agent is stuck until it expires.
    assert app._pending_clarify is not None


@pytest.mark.asyncio
async def test_answering_a_dead_question_says_so():
    app, composer, transcript = _app_with()
    app._on_clarify_requested(_event())

    class _Client:
        async def clarify_resolve(self, clarify_id: str, answer: str) -> bool:
            return False

    app._client = _Client()
    await app._on_clarify_answered(_StopMessage("cl_1", "A"))
    assert composer.cleared == 1
    assert any("no longer waiting" in s for s in transcript.system)


def test_an_aborted_turn_retires_the_tray():
    """Abort cancels the agent coroutine, which can skip the ``closed``
    broadcast — the tray must not survive the turn that asked."""
    app, composer, _ = _app_with()
    app._on_clarify_requested(_event())
    app._drop_pending_clarify()
    assert app._pending_clarify is None
    assert composer.cleared == 1


@pytest.mark.asyncio
async def test_restore_reopens_a_question_asked_while_we_were_away():
    app, composer, _ = _app_with()

    class _Client:
        async def clarify_list(self):
            return [{
                "id": "cl_1", "question": "Which station?",
                "choices": ["A", "B"], "sessionKey": "cli:1", "expiresAt": 9e12,
            }]

    app._client = _Client()
    await app._restore_clarify_state()
    assert [r.id for r in composer.shown] == ["cl_1"]


@pytest.mark.asyncio
async def test_restore_retires_another_session_s_question():
    """Switching sessions re-runs the restore; the previous session's tray
    must not survive the switch."""
    app, composer, _ = _app_with()
    app._on_clarify_requested(_event())

    class _Client:
        async def clarify_list(self):
            return [{"id": "cl_9", "question": "?", "sessionKey": "web:9"}]

    app._client = _Client()
    await app._restore_clarify_state()
    assert app._pending_clarify is None
    assert composer.cleared == 1


@pytest.mark.asyncio
async def test_restore_leaves_the_tray_alone_when_the_list_fails():
    app, composer, _ = _app_with()
    app._on_clarify_requested(_event())

    class _Client:
        async def clarify_list(self):
            raise RuntimeError("socket died")

    app._client = _Client()
    await app._restore_clarify_state()
    assert app._pending_clarify is not None
    assert composer.cleared == 0


class _StopMessage:
    """Stands in for ClarifyPrompt.Answered — the handler only reads the two
    fields and calls ``stop()``."""

    def __init__(self, clarify_id: str, answer: str) -> None:
        self.clarify_id = clarify_id
        self.answer = answer

    def stop(self) -> None:
        pass
