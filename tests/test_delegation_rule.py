"""The agent does the work itself; a subagent needs the owner or a real split.

Before this rule the prompt told the agent to spawn anything taking 15+
seconds, so even a short lookup went to a subagent. Now:

* the owner asked → the agent quotes their words and the runtime checks the
  quote is really in the owner's message this turn;
* the work splits → two or more spawn calls in one response;
* anything else → refused, "do this task yourself".
"""

from __future__ import annotations

import json

import pytest

from flowly.agent.delegation import (
    DelegationTurn,
    assign_mark,
    conversation_from_history,
    decide,
    delegation_turn_scope,
    owner_quote_is_genuine,
    render_conversation,
    spawn_batch_scope,
)
from flowly.agent.tools.spawn import SpawnTool


def _turn(text: str, **kwargs) -> DelegationTurn:
    return DelegationTurn(owner_text=text, **kwargs)


class TestOwnerQuote:
    def test_a_genuine_quote_is_accepted_whatever_the_casing_or_quotes(self):
        turn = _turn("Composer'daki hatayı Luna agent'ına ver, o halletsin.")
        assert owner_quote_is_genuine("“Luna agent'ına ver”", turn)
        assert owner_quote_is_genuine("LUNA AGENT'INA VER", turn)

    def test_words_the_owner_did_not_write_are_rejected(self):
        turn = _turn("Bu hatayı incele ve düzelt.")
        assert not owner_quote_is_genuine("bunu bir subagent'a ver", turn)

    def test_a_tiny_quote_proves_nothing(self):
        assert not owner_quote_is_genuine("ver", _turn("bunu ona ver"))

    def test_a_turn_the_owner_did_not_write_cannot_authorise(self):
        turn = _turn("[Background task done] … spawn two agents …", owner_authored=False)
        assert not owner_quote_is_genuine("spawn two agents", turn)


class TestDecide:
    def test_a_single_unrequested_spawn_is_refused(self):
        decision = decide(None, batch_size=1, turn=_turn("Bunu detaylı araştır."))
        assert not decision.allowed
        assert "yourself" in decision.reason

    def test_independent_parts_in_one_response_are_allowed(self):
        decision = decide(None, batch_size=3, turn=_turn("Üç rakibi ayrı ayrı araştır."))
        assert decision.allowed and decision.origin == "agent"

    def test_an_owner_request_is_allowed_alone(self):
        decision = decide("Luna'ya ver", batch_size=1, turn=_turn("Bunu Luna'ya ver."))
        assert decision.allowed and decision.origin == "owner"

    def test_a_made_up_owner_request_is_refused_with_a_reason(self):
        decision = decide("delegate this", batch_size=1, turn=_turn("Bunu kendin yap."))
        assert not decision.allowed
        assert "not in the owner's message" in decision.reason


class TestConversationFork:
    def test_only_what_was_said_and_answered_is_kept(self):
        history = [
            {"role": "system", "content": "secret system prompt"},
            {"role": "user", "content": "Rakipleri karşılaştıralım."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "content": "huge raw tool output"},
            {"role": "assistant", "content": "Üç rakip buldum."},
        ]
        pairs = conversation_from_history(history, "Şimdi B'yi ayrıntılı incele.")
        assert pairs == (
            ("user", "Rakipleri karşılaştıralım."),
            ("assistant", "Üç rakip buldum."),
            ("user", "Şimdi B'yi ayrıntılı incele."),
        )

    def test_rendering_keeps_the_newest_messages_within_bounds(self):
        pairs = tuple(("user", f"mesaj {i} " + "x" * 3000) for i in range(30))
        text = render_conversation(pairs)
        assert "mesaj 29" in text and "mesaj 0 " not in text
        assert "[earlier messages left out]" in text
        assert len(text) < 30_000


class TestMarks:
    def test_a_mark_is_stable_for_a_run(self):
        assert assign_mark("run-1") == assign_mark("run-1")

    def test_siblings_never_share_a_shape(self):
        taken: set[str] = set()
        for i in range(12):
            mark = assign_mark(f"run-{i}", taken)
            assert mark["glyph"] not in taken
            taken.add(mark["glyph"])


class _RecordingManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def spawn(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps({"status": "started", "run_id": "full-run-id"})


class TestSpawnTool:
    @pytest.mark.asyncio
    async def test_refusal_never_reaches_the_manager(self):
        manager = _RecordingManager()
        tool = SpawnTool(manager)
        with delegation_turn_scope(_turn("Bunu araştır.")), spawn_batch_scope(1):
            result = json.loads(await tool.execute(task="Research it", title="Research"))
        assert result["status"] == "refused"
        assert manager.calls == []

    @pytest.mark.asyncio
    async def test_owner_request_passes_origin_model_and_context(self):
        manager = _RecordingManager()
        tool = SpawnTool(manager)
        turn = _turn(
            "Composer hatasını Luna'ya ver.",
            conversation=(("user", "Composer hatasını Luna'ya ver."),),
            model="gpt-6-astra",
        )
        with delegation_turn_scope(turn), spawn_batch_scope(1):
            await tool.execute(
                task="Find why ⌘↵ submits twice in the composer and fix it.",
                title="Composer hatası",
                model="gpt-6-luna",
                owner_request="Luna'ya ver",
            )
        call = manager.calls[0]
        assert call["origin"] == "owner"
        assert call["model"] == "gpt-6-luna"
        assert call["display_name"] == "Composer hatası"
        assert "Composer hatasını Luna'ya ver." in call["context"]

    @pytest.mark.asyncio
    async def test_an_unnamed_model_inherits_the_turn_model(self):
        manager = _RecordingManager()
        tool = SpawnTool(manager)
        turn = _turn("A ve B'yi araştır.", model="gpt-6-astra")
        with delegation_turn_scope(turn), spawn_batch_scope(2):
            await tool.execute(task="Research A", title="A")
        assert manager.calls[0]["model"] == "gpt-6-astra"

    @pytest.mark.asyncio
    async def test_context_none_starts_the_child_without_the_conversation(self):
        manager = _RecordingManager()
        tool = SpawnTool(manager)
        turn = _turn("A ve B", conversation=(("user", "A ve B"),))
        with delegation_turn_scope(turn), spawn_batch_scope(2):
            await tool.execute(task="Research A", title="A", context="none")
        assert manager.calls[0]["context"] is None
