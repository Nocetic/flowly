"""A call's recall finds what was said in past conversations, at once.

The agent's chat turns could search every past conversation; a call's recall
searched only memory, so "what did we say about the trip?" became a separate
task and the owner waited. Recall now searches the same conversation index.
"""
from __future__ import annotations

import json

import pytest

from flowly.live_voice import conversation_recall
from flowly.live_voice.authority import RequestOwner, request_owner_scope
from flowly.live_voice.context import VoiceContext
from flowly.live_voice.conversation_recall import search_conversations
from flowly.memory.governance import GovernanceStore
from flowly.session.indexer import SessionIndexer


def say(role, content, ts):
    return {"role": role, "content": content, "timestamp": ts}


@pytest.fixture
def home(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndexer(tmp_path / "session_index.sqlite")

    def conversation(key, rows, metadata=None):
        path = sessions / (key.replace(":", "_") + ".jsonl")
        lines = [{"_type": "metadata", "session_key": key, "metadata": metadata or {}}, *rows]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n")
        index.index_session(key, rows)
        return path

    yield tmp_path, sessions, conversation
    index.close()


def test_the_matching_moment_comes_with_the_messages_around_it(home):
    root, sessions, conversation = home
    conversation("telegram:1", [
        say("user", "Can you book something for the weekend?", 1_000),
        say("assistant", "Sure, where to?", 1_001),
        say("user", "The İzmir trip, Friday morning train please.", 1_002),
        say("assistant", "Booked the 9:40 train to İzmir.", 1_003),
        say("user", "Great, thanks.", 1_004),
        say("assistant", "Anything else?", 1_005),
    ], metadata={"title": "Weekend plans"})
    conversation("web:other", [say("user", "Unrelated talk about coffee", 2_000)])

    [hit] = search_conversations(root / "session_index.sqlite", sessions, "İzmir train")
    assert hit.key == "telegram:1" and hit.title == "Weekend plans" and not hit.is_call
    assert hit.text.splitlines() == [
        "User: Can you book something for the weekend?", "Agent: Sure, where to?",
        "User: The İzmir trip, Friday morning train please.",
        "Agent: Booked the 9:40 train to İzmir.", "User: Great, thanks."]


def test_a_spoken_question_finds_it_by_any_of_its_words(home):
    root, sessions, conversation = home
    # Everyday words are everywhere, as in any real history.
    for n in range(20):
        conversation(f"web:{n}", [say("user", f"what did we say last week about the plan {n}", 1_000 + n)])
    conversation("desktop:voice:call-1", [say("user", "Let's talk about the penicillin correction", 1_000),
                                          say("assistant", "Noted, you are not allergic.", 1_001)],
                 metadata={"title": "Penicillin", "kind": "voice"})
    [hit] = search_conversations(root / "session_index.sqlite", sessions,
                                 "what did we say about penicillin last week")
    assert hit.is_call and "not allergic" in hit.text


def test_only_existing_owner_conversations_the_requester_may_see(home):
    root, sessions, conversation = home
    for key in ("desktop:voice-work:c_1", "cron:nightly", "desktop:group:room"):
        conversation(key, [say("user", "secret plan alpha", 1_000)])
    conversation("web:theirs", [say("user", "secret plan alpha", 1_000)],
                 metadata={"voiceOwner": {"kind": "account", "uid": "account-b"}})
    gone = conversation("ios:deleted", [say("user", "secret plan alpha", 1_000)])
    gone.unlink()  # deleted, still in the index
    conversation("android:mine", [say("user", "secret plan alpha", 1_000)])
    with request_owner_scope(RequestOwner("account-a")):
        hits = search_conversations(root / "session_index.sqlite", sessions, "secret plan")
    assert [hit.key for hit in hits] == ["android:mine"]


def test_at_most_three_conversations_and_long_messages_are_cut(home):
    root, sessions, conversation = home
    for n in range(6):
        conversation(f"web:{n}", [say("user", "budget review " + "word " * 200, 1_000 + n)])
    hits = search_conversations(root / "session_index.sqlite", sessions, "budget review")
    assert len(hits) == conversation_recall.RESULT_CONVERSATIONS
    assert all(len(line) <= conversation_recall.LINE_CHARS + 10 for hit in hits for line in hit.text.splitlines())


def test_no_index_or_no_words_finds_nothing(home, tmp_path):
    root, sessions, _ = home
    assert search_conversations(tmp_path / "absent.sqlite", sessions, "anything") == []
    assert search_conversations(root / "session_index.sqlite", sessions, "") == []
    assert search_conversations(root / "session_index.sqlite", sessions, '"(') == []


@pytest.mark.asyncio
async def test_recall_returns_them_with_memory_and_governance_still_applies(home):
    root, sessions, conversation = home
    workspace = root / "workspace"
    (workspace / "memory").mkdir(parents=True)
    store = GovernanceStore(root / "memory_governance.sqlite3")
    store.add_item(kind="preference", text="Prefers the window seat on trains.", status="active")
    store.add_item(kind="preference", text="Hakan is allergic to penicillin.", status="rejected")
    conversation("telegram:1", [say("user", "Book the train to İzmir, window seat.", "2026-10-01T10:00:00"),
                                say("assistant", "Booked the 9:40 train, seat 12A.", "2026-10-01T10:00:05")],
                 metadata={"title": "Trip"})
    conversation("web:2", [say("user", "Hakan is allergic to penicillin. On the train too.", 1_002)])
    reader = VoiceContext(workspace, state_db=lambda name: root / name, profile=lambda: ("default", "bot-1"),
                          conversations=lambda: (root / "session_index.sqlite", sessions))

    result = await reader.search({"query": "train"})
    texts = [fact["text"] for fact in result["facts"]]
    found = [fact for fact in result["facts"] if fact.get("kind") == "conversation"]
    assert len(found) == 1 and found[0]["text"].startswith("Conversation: Trip (2026-10-01)\n")
    assert "seat 12A" in found[0]["text"] and found[0]["sourceRef"].startswith("memory://conversation/")
    assert "telegram" not in found[0]["sourceRef"]  # no chat id leaves the agent
    assert any("window seat" in text for text in texts)  # memory is still there
    assert not any("allergic" in text for text in texts)  # a withheld item's words never leave
    assert result["sources"]["conversations"] == {"status": "ok"}

    # The startup recall (no query) does not search conversations.
    assert "conversations" not in (await reader.search({}))["sources"]
    store.close()


def test_common_words_alone_never_bring_back_a_conversation(home):
    root, sessions, conversation = home
    for n in range(20):
        conversation(f"web:{n}", [say("user", f"what did we do last week, about the usual things {n}", 1_000 + n)])
    conversation("telegram:1", [say("user", "Turns out the penicillin note was wrong.", 2_000)],
                 metadata={"title": "Penicillin"})
    hits = search_conversations(root / "session_index.sqlite", sessions, "what about last week penicillin")
    assert [hit.key for hit in hits] == ["telegram:1"]
    assert search_conversations(root / "session_index.sqlite", sessions, "what about last week") != []  # every word matched
    # The topic asked about is in no conversation: nothing comes back.
    assert search_conversations(root / "session_index.sqlite", sessions, "what about last week tennis") == []
