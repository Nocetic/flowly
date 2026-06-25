from __future__ import annotations

from flowly.integrations import get_card
from flowly.tui.app import FlowlyTUI, _inline_provider_key_field


def test_inline_provider_key_field_accepts_byok_api_key_provider() -> None:
    card = get_card("openai")
    assert card is not None

    field = _inline_provider_key_field(card)

    assert field is not None
    assert field.key == "api_key"


def test_inline_provider_key_field_skips_account_and_oauth_providers() -> None:
    flowly = get_card("flowly")
    xai_oauth = get_card("xai_oauth")
    assert flowly is not None
    assert xai_oauth is not None

    assert _inline_provider_key_field(flowly) is None
    assert _inline_provider_key_field(xai_oauth) is None


async def test_ctrl_c_cancels_inline_secret_before_exit() -> None:
    import asyncio

    app = FlowlyTUI(client=None)
    fut: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    app._inline_secret_future = fut
    app._current_run = None

    await app.action_abort_or_quit()

    assert fut.done()
    assert fut.result() is None


async def test_ctrl_c_cancels_inline_setup_before_exit() -> None:
    import asyncio

    app = FlowlyTUI(client=None)
    fut: asyncio.Future[dict[str, object] | None] = (
        asyncio.get_running_loop().create_future()
    )
    app._inline_setup_future = fut
    app._current_run = None

    await app.action_abort_or_quit()

    assert fut.done()
    assert fut.result() is None
