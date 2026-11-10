"""The person's message keeps the run it started (2026-11-10).

An app draws its message under the id it sent it with, and a chat.send runs
under that idempotency key. History gives the person's row that run, so the
app's bubble and the stored row are one message: also on a device that saw it
sent from another, which drew it under the same id. Without it a phone showed
the message a second time under the reply in a home whose chats are read from
its agent.
"""

from __future__ import annotations

import pytest

import flowly.profile as profiles
from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse
from flowly.session.history_rows import history_row
from flowly.session.manager import SessionManager


class _Reply(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="Tamam knk.", finish_reason="stop")


@pytest.mark.asyncio
async def test_the_persons_row_names_the_run_it_started_and_history_gives_it(tmp_path, monkeypatch):
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("rhody")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    loop = AgentLoop(bus=MessageBus(), provider=_Reply(), workspace=home / "workspace",
                     main_config=Config(), max_iterations=2, soft_warn_at_iteration=0)
    try:
        await loop._process_message(InboundMessage(channel="web", sender_id="owner", chat_id="c1",
                                                   content="tamamsss", metadata={"run_id": "msg-7"}))
    finally:
        loop.stop()
    rows = SessionManager(home / "workspace").get_full_messages("web:c1")
    user = [m for m in rows if m.get("role") == "user"][-1]
    assert user["content"] == "tamamsss" and user["run_id"] == "msg-7"
    assert history_row(user)["runId"] == "msg-7"
    reply = [m for m in rows if m.get("role") == "assistant"][-1]
    assert reply.get("run_id") == "msg-7"
