"""The model learns the date with each message, outside the cached prompt."""
import datetime as dt
from zoneinfo import ZoneInfo

import flowly.cron.timezone as host_clock
from flowly.agent.prompt_blocks import MANDATORY_TOOL_USE_BLOCK
from flowly.agent.turn_clock import turn_clock


def test_it_names_the_local_date_weekday_time_and_zone(monkeypatch):
    istanbul = ZoneInfo("Europe/Istanbul")
    monkeypatch.setattr(host_clock, "schedule_timezone", lambda name=None: istanbul)
    monkeypatch.setattr(host_clock, "host_timezone_metadata", lambda: {"id": "Europe/Istanbul"})
    line = turn_clock(dt.datetime(2026, 10, 4, 12, 14, tzinfo=dt.timezone.utc))
    assert line == ("<turn_time>Sunday 2026-10-04 15:14 (Europe/Istanbul, UTC+03:00) on the agent's computer, "
                    "when this message arrived. Use this date for anything you date or record.</turn_time>")


def test_a_host_without_a_known_zone_still_gets_a_date(monkeypatch):
    def unknown(name=None):
        raise ValueError("Unable to determine the execution host timezone")
    monkeypatch.setattr(host_clock, "schedule_timezone", unknown)
    assert "2026-10-04" in turn_clock(dt.datetime(2026, 10, 4, 0, 30, tzinfo=dt.timezone.utc))
    assert "(UTC, UTC+00:00)" in turn_clock(dt.datetime(2026, 10, 4, 0, 30, tzinfo=dt.timezone.utc))


def test_the_prompt_sends_the_model_to_the_turn_time_not_to_a_guess():
    assert "<turn_time>" in MANDATORY_TOOL_USE_BLOCK and "never guess a date" in MANDATORY_TOOL_USE_BLOCK


async def test_a_turn_tells_the_model_the_date_and_the_transcript_stays_the_owners(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.bus.events import InboundMessage
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse

    home = tmp_path / ".flowly"
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.requests = []

        def get_default_model(self):
            return "test/model"

        async def chat(self, messages, tools=None, **kwargs):
            self.requests.append(messages)
            return LLMResponse(content="Noted.")

    provider = Provider()
    config = Config()
    config.tools.routing.discovery.enabled = False
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=home / "workspace",
                      main_config=config, max_iterations=2, soft_warn_at_iteration=0)
    monkeypatch.setattr(agent, "_schedule_post_turn_compaction", lambda msg: None)
    try:
        await agent._process_message(InboundMessage(channel="telegram", sender_id="owner", chat_id="1",
                                                    content="I'm not allergic to penicillin."))
    finally:
        agent.stop()

    sent = next(request for request in provider.requests if any(
        "penicillin" in str(m.get("content")) for m in request if m.get("role") == "user"))
    user = [m for m in sent if m.get("role") == "user"][-1]["content"]
    assert "<turn_time>" in user and user.rstrip().endswith("I'm not allergic to penicillin.")
    # The system prompt stays the cacheable prefix; the clock is not in it.
    import re
    assert not re.search(r"<turn_time>\w+day \d{4}-\d{2}-\d{2}", sent[0]["content"])
    stored = agent.sessions.get_or_create("telegram:1").messages
    assert [m["content"] for m in stored if m["role"] == "user"] == ["I'm not allergic to penicillin."]
