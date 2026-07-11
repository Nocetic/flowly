from __future__ import annotations

from flowly.integrations import Field, FieldType, IntegrationCard, ProbeResult, get_card
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
    zai_coding = get_card("zai_coding")
    assert flowly is not None
    assert xai_oauth is not None
    assert zai_coding is not None

    assert _inline_provider_key_field(flowly) is None
    assert _inline_provider_key_field(xai_oauth) is None
    assert _inline_provider_key_field(zai_coding) is None


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


async def test_advanced_provider_setup_uses_all_fields_inline(monkeypatch) -> None:
    import flowly.integrations as integrations

    async def probe(values):
        assert values == {"api_key": "new-key", "base_url": "https://new.test/v1"}
        return ProbeResult("ok", "connected")

    card = IntegrationCard(
        key="custom",
        label="Custom provider",
        category="provider",
        description="OpenAI-compatible provider",
        docs_url="",
        config_path="providers.custom",
        fields=[
            Field("api_key", "API key", FieldType.PASSWORD, required=True),
            Field("base_url", "Base URL", FieldType.TEXT, required=True),
        ],
        probe=probe,
        needs_gateway_restart=False,
    )
    requests = []
    persisted = []
    app = FlowlyTUI(client=None)

    async def show_setup(request):
        requests.append(request)
        return {"api_key": "new-key", "base_url": "https://new.test/v1"}

    async def reload_provider() -> str:
        return "gateway reloaded"

    monkeypatch.setattr(app, "_show_inline_setup", show_setup)
    monkeypatch.setattr(app, "_reload_gateway_provider", reload_provider)
    monkeypatch.setattr(
        integrations,
        "read_card_values",
        lambda _card: {"api_key": "old-key", "base_url": "https://old.test/v1"},
    )
    monkeypatch.setattr(
        integrations,
        "apply_card_values",
        lambda _card, values: persisted.append(values),
    )

    assert await app._configure_card_inline(card) is True
    assert [field.key for field in requests[0].fields] == ["api_key", "base_url"]
    assert persisted == [{"api_key": "new-key", "base_url": "https://new.test/v1"}]


async def test_advanced_provider_setup_requires_inline_override_after_failed_probe(
    monkeypatch,
) -> None:
    import flowly.integrations as integrations

    async def probe(_values):
        return ProbeResult("auth_failed", "token rejected")

    card = IntegrationCard(
        key="custom",
        label="Custom provider",
        category="provider",
        description="OpenAI-compatible provider",
        docs_url="",
        config_path="providers.custom",
        fields=[Field("api_key", "API key", FieldType.PASSWORD, required=True)],
        probe=probe,
        needs_gateway_restart=False,
    )
    requests = []
    persisted = []
    responses = iter([{"api_key": "bad-key"}, {"decision": "cancel"}])
    app = FlowlyTUI(client=None)

    async def show_setup(request):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(app, "_show_inline_setup", show_setup)
    monkeypatch.setattr(integrations, "read_card_values", lambda _card: {})
    monkeypatch.setattr(
        integrations,
        "apply_card_values",
        lambda _card, values: persisted.append(values),
    )

    assert await app._configure_card_inline(card) is False
    assert len(requests) == 2
    assert requests[1].fields[0].choices == [
        ("cancel", "Cancel without saving"),
        ("save", "Save anyway"),
    ]
    assert persisted == []
