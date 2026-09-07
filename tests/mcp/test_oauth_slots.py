"""A failed new setup cannot overwrite the working connection's credentials."""
import json

import pytest
from mcp.shared.auth import OAuthToken

from flowly.mcp.oauth import FlowlyTokenStorage, clear_all_tokens, oauth_login, token_storage_for


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_new_slot_is_inactive_until_config_switch(home):
    old = FlowlyTokenStorage("demo", "https://old.example/mcp")
    await old.set_tokens(OAuthToken(access_token="working", token_type="Bearer"))
    original = old._path.read_bytes()
    slot = "a" * 32
    with oauth_login("demo", "https://new.example/mcp", credential_id=slot) as login:
        staged = token_storage_for("demo", "https://new.example/mcp", credential_id=slot)
        await staged.set_tokens(OAuthToken(access_token="new", token_type="Bearer"))
        login.commit()
        # A crash or config-save failure at this point leaves the old slot live.
        assert (await FlowlyTokenStorage("demo").get_tokens()).access_token == "working"
    assert old._path.read_bytes() == original
    (home / "config.json").write_text(json.dumps({"mcpServers": {"demo": {
        "url": "https://new.example/mcp", "auth": "oauth", "oauthCredentialId": slot,
    }}}))
    assert (await FlowlyTokenStorage("demo", "https://new.example/mcp").get_tokens()).access_token == "new"
    assert await FlowlyTokenStorage("demo", "https://other.example/mcp").get_tokens() is None


@pytest.mark.asyncio
async def test_cancelled_slot_is_never_published(home):
    slot = "b" * 32
    with oauth_login("demo", "https://mcp.example", credential_id=slot):
        await token_storage_for("demo", "https://mcp.example", credential_id=slot).set_tokens(
            OAuthToken(access_token="partial", token_type="Bearer"),
        )
    assert await FlowlyTokenStorage("demo", credential_id=slot).get_tokens() is None
    assert list((home / "mcp-tokens").glob(".login-*")) == []


@pytest.mark.asyncio
async def test_removal_clears_only_exact_connections_slots():
    for name, slot in [("demo", ""), ("demo", "a" * 32), ("demo-other", "b" * 32)]:
        await FlowlyTokenStorage(name, credential_id=slot).set_tokens(
            OAuthToken(access_token="test", token_type="Bearer"),
        )
    assert clear_all_tokens("demo") is True
    assert await FlowlyTokenStorage("demo", credential_id="").get_tokens() is None
    assert await FlowlyTokenStorage("demo", credential_id="a" * 32).get_tokens() is None
    assert await FlowlyTokenStorage("demo-other", credential_id="b" * 32).get_tokens() is not None


@pytest.mark.parametrize("slot", ["../other", "a/b", "-" * 32, "A" * 32, "a" * 33])
def test_slot_cannot_choose_an_arbitrary_file(slot):
    with pytest.raises(ValueError):
        FlowlyTokenStorage("demo", credential_id=slot)
