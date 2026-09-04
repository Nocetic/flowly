"""A message the dispatcher will drop must not be reported as sent.

The outbound dispatcher looks the channel up long after the tool returns:
an unknown name is logged and the message thrown away, while the tool has
already said "Message sent". So a model that invents a plausible channel —
``agent``, to reach another bot — is told it worked, and the reader waits
for a reply that was never coming.
"""

from __future__ import annotations

import pytest

from flowly.agent.tools.message import MessageTool
from flowly.channels.manager import CHANNELS_WITHOUT_ADAPTER


@pytest.fixture
def sent():
    delivered = []

    async def send(msg):
        delivered.append(msg)

    return delivered, send


@pytest.mark.asyncio
async def test_an_invented_channel_is_refused_not_reported_sent(sent):
    delivered, send = sent
    tool = MessageTool(send_callback=send, known_channels=lambda: {"telegram", "cli"})

    result = await tool.execute(content="Merhaba EV!", channel="agent", chat_id="EV")

    assert "Error" in result
    assert "no 'agent' channel" in result
    # And it names what is real, so the next attempt can be right.
    assert "telegram" in result and "cli" in result
    assert delivered == []


@pytest.mark.asyncio
async def test_reaching_another_bot_is_pointed_at_the_right_tool(sent):
    _, send = sent
    tool = MessageTool(send_callback=send, known_channels=lambda: {"cli"})

    result = await tool.execute(content="hi", channel="agent", chat_id="EV")

    assert "message_profile" in result


@pytest.mark.asyncio
async def test_a_real_channel_still_sends(sent):
    delivered, send = sent
    tool = MessageTool(send_callback=send, known_channels=lambda: {"telegram"})

    result = await tool.execute(content="hi", channel="telegram", chat_id="42")

    assert "Message sent to telegram:42" in result
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_a_gateway_answered_channel_counts_as_real(sent):
    delivered, send = sent
    # cli / tui / desktop have no adapter but are answered over the gateway.
    tool = MessageTool(send_callback=send, known_channels=lambda: set(CHANNELS_WITHOUT_ADAPTER))

    result = await tool.execute(content="hi", channel="desktop", chat_id="s1")

    assert "Message sent" in result
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_an_unwired_tool_keeps_its_old_behaviour(sent):
    delivered, send = sent
    tool = MessageTool(send_callback=send)

    result = await tool.execute(content="hi", channel="anything", chat_id="x")

    assert "Message sent" in result
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_a_directory_that_cannot_be_read_refuses_nothing(sent):
    delivered, send = sent

    def broken():
        raise RuntimeError("channels not up yet")

    tool = MessageTool(send_callback=send, known_channels=broken)

    # Refusing every message because the directory is unavailable would be a
    # worse failure than the one this guards against.
    result = await tool.execute(content="hi", channel="telegram", chat_id="42")

    assert "Message sent" in result
    assert len(delivered) == 1
