"""Choosing an agent's provider from an app.

Before: the new apps could not connect a provider at all. ``provider.set_key``
took the key in the clear, which every call through the Flowly server would
have let the server read, and the ChatGPT subscription signed in only from a
terminal (``flowly codex login``). Now a key arrives sealed to the agent's
secrets key, the server channel refuses one in the clear, the ChatGPT
subscription signs in with a device code over RPC, and ``provider.list`` says
what kind each provider is and how it signs in.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from flowly.auth import openai_codex, provider_login
from flowly.channels import feature_rpc
from flowly.channels.flowly_server import FlowlyServerChannel, _plain_provider_key
from flowly.config.loader import load_config
from flowly.secrets import envelope
from flowly.secrets import vault as vault_mod
from tests.test_secrets_vault import FakeKeyring, keychain  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _no_reload(monkeypatch):
    async def reloaded():
        return True

    monkeypatch.setattr(feature_rpc, "_live_provider_reload_succeeded", reloaded)


@pytest.fixture(autouse=True)
def _forget_logins():
    provider_login._logins.clear()
    yield
    for login in provider_login._logins.values():
        login.cancel.set()
    provider_login._logins.clear()


# ── provider.list ─────────────────────────────────────────────────────────


def test_every_provider_says_what_it_is_and_how_it_signs_in(keychain):  # noqa: F811
    rows = {row["key"]: row for row in feature_rpc.provider_list()["providers"]}
    assert (rows["flowly"]["kind"], rows["flowly"]["login"]) == ("account", None)
    assert (rows["openai_codex"]["kind"], rows["openai_codex"]["login"]) == ("subscription", "device")
    # Grok signs in with a device code too: from a phone, away from the agent's machine.
    assert (rows["xai_oauth"]["kind"], rows["xai_oauth"]["login"]) == ("subscription", "device")
    assert rows["anthropic"]["kind"] == "key" and rows["anthropic"]["login"] is None
    # Model servers: ``url`` as the self-hosted row always was, so every app
    # shows them only once set up or in use (provider.local lists the models).
    assert rows["vllm"]["kind"] == "url" and rows["ollama"]["kind"] == "url"
    assert all("apiKey" not in row and "value" not in row for row in rows.values())


# ── sealed keys ───────────────────────────────────────────────────────────


def _sealed(key: str, value: str, *, for_provider: str | None = None, kid: str | None = None) -> dict:
    data = vault_mod.get_vault().load_or_create()
    sealed = envelope.seal({feature_rpc.PROVIDER_KEY_NAME: value}, data.public_key, kid=data.kid,
                           request_id=feature_rpc.provider_key_request(for_provider or key))
    return {"key": key, "kid": kid or data.kid, "sealed": sealed}


@pytest.mark.asyncio
async def test_a_sealed_key_is_opened_and_saved(keychain):  # noqa: F811
    result = await feature_rpc.provider_set_key(_sealed("anthropic", " sk-ant-test "))
    assert result["hasKey"] is True and "value" not in result
    assert load_config().providers.anthropic.api_key == "sk-ant-test"


@pytest.mark.asyncio
async def test_a_key_sealed_for_another_provider_or_key_is_refused(keychain):  # noqa: F811
    with pytest.raises(feature_rpc.FeatureRpcError) as other_provider:
        await feature_rpc.provider_set_key(_sealed("anthropic", "sk", for_provider="openai"))
    assert other_provider.value.code == "DECRYPT_FAILED"
    with pytest.raises(feature_rpc.FeatureRpcError) as old_key:
        await feature_rpc.provider_set_key(_sealed("anthropic", "sk", kid="0000000000000000"))
    assert old_key.value.code == "KEY_CHANGED"
    assert load_config().providers.anthropic.api_key == ""


@pytest.mark.asyncio
async def test_a_local_caller_still_sends_a_plain_key(keychain):  # noqa: F811
    await feature_rpc.provider_set_key({"key": "openrouter", "value": "sk-or-1"})
    assert load_config().providers.openrouter.api_key == "sk-or-1"


def test_the_server_channel_knows_a_plain_key():
    assert _plain_provider_key({"key": "anthropic", "value": "sk-ant"})
    assert not _plain_provider_key({"key": "anthropic", "value": ""})  # clearing
    assert not _plain_provider_key({"key": "anthropic", "kid": "k", "sealed": "x"})


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_the_server_channel_refuses_a_plain_key_before_it_reaches_the_agent(monkeypatch):
    reached: list[dict] = []

    async def parent(self, ws, msg):
        reached.append(msg)

    monkeypatch.setattr("flowly.channels.web.WebChannel._dispatch_rpc", parent)
    channel = FlowlyServerChannel.__new__(FlowlyServerChannel)
    ws = _Socket()
    await channel._dispatch_rpc(ws, {"id": "1", "sessionId": "s", "method": "provider.set_key",
                                     "params": {"key": "anthropic", "value": "sk-ant-secret"}})
    assert ws.sent[0]["error"]["code"] == "KEY_NOT_SEALED"
    assert "sk-ant-secret" not in json.dumps(ws.sent) and reached == []
    await channel._dispatch_rpc(ws, {"id": "2", "sessionId": "s", "method": "provider.set_key",
                                     "params": {"key": "anthropic", "kid": "k", "sealed": "x"}})
    assert len(reached) == 1


# ── ChatGPT sign-in ───────────────────────────────────────────────────────


class _DeviceLogin:
    """Stands in for OpenAI's device-code flow: shows a code, then waits for
    ``approve`` or a cancel, as ``openai_codex.login_with_device_code`` does."""

    def __init__(self) -> None:
        self.approve = threading.Event()
        self.saved = False

    def __call__(self, *, client_id, on_user_code, timeout_seconds, cancelled):
        on_user_code("ABCD-1234", "https://auth.openai.com/codex/device")
        while not self.approve.wait(0.01):
            if cancelled():
                raise openai_codex.CodexAuthError("Device login cancelled")
        self.saved = True
        return object()


async def _until(check, seconds: float = 2.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


@pytest.mark.asyncio
async def test_chatgpt_signs_in_with_a_code_and_the_provider_reloads(monkeypatch):
    device = _DeviceLogin()
    monkeypatch.setattr(openai_codex, "login_with_device_code", device)
    reloads: list[bool] = []

    async def reloaded():
        reloads.append(True)
        return True

    monkeypatch.setattr(feature_rpc, "_live_provider_reload_succeeded", reloaded)
    started = await feature_rpc.provider_login_start({"key": "openai_codex"})
    assert (started["state"], started["userCode"]) == ("pending", "ABCD-1234")
    assert started["verificationUrl"].startswith("https://auth.openai.com/") and started["expiresIn"] > 0
    assert feature_rpc.provider_login_status({"loginId": started["loginId"]})["state"] == "pending"

    device.approve.set()
    await _until(lambda: reloads)
    done = feature_rpc.provider_login_status({"loginId": started["loginId"]})
    assert done["state"] == "done" and "userCode" not in done and device.saved


@pytest.mark.asyncio
async def test_grok_signs_in_with_a_code_from_any_device(monkeypatch):
    from flowly.auth import xai_oauth

    device = _DeviceLogin()

    def grok(**kwargs):
        on_user_code = kwargs["on_user_code"]
        kwargs["on_user_code"] = lambda code, _url: on_user_code(code, "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234")
        return device(**kwargs)

    monkeypatch.setattr(xai_oauth, "login_with_device_code", grok)
    monkeypatch.setattr(openai_codex, "login_with_device_code", lambda **_: pytest.fail("ChatGPT's sign-in ran for Grok"))
    reloads: list[bool] = []

    async def reloaded():
        reloads.append(True)
        return True

    monkeypatch.setattr(feature_rpc, "_live_provider_reload_succeeded", reloaded)
    started = await feature_rpc.provider_login_start({"key": "xai_oauth"})
    assert (started["key"], started["state"], started["userCode"]) == ("xai_oauth", "pending", "ABCD-1234")
    assert started["verificationUrl"].startswith("https://accounts.x.ai/")
    device.approve.set()
    await _until(lambda: reloads)
    assert feature_rpc.provider_login_status({"loginId": started["loginId"]})["state"] == "done" and device.saved


@pytest.mark.asyncio
async def test_a_sign_in_can_be_cancelled_and_a_new_one_replaces_the_old(monkeypatch):
    monkeypatch.setattr(openai_codex, "login_with_device_code", _DeviceLogin())
    first = await feature_rpc.provider_login_start({"key": "openai_codex"})
    second = await feature_rpc.provider_login_start({"key": "openai_codex"})
    assert feature_rpc.provider_login_status({"loginId": first["loginId"]})["state"] == "cancelled"
    assert feature_rpc.provider_login_cancel({"loginId": second["loginId"]})["state"] == "cancelled"
    await _until(lambda: not any(t.name.startswith("provider-login") for t in threading.enumerate()))


@pytest.mark.asyncio
async def test_sign_in_refuses_what_it_cannot_do(monkeypatch):
    with pytest.raises(feature_rpc.FeatureRpcError) as unsupported:
        await feature_rpc.provider_login_start({"key": "anthropic"})
    assert unsupported.value.code == "LOGIN_UNSUPPORTED"
    with pytest.raises(feature_rpc.FeatureRpcError) as unknown:
        feature_rpc.provider_login_status({"loginId": "nope"})
    assert unknown.value.code == "LOGIN_NOT_FOUND"

    def broken(**_):
        raise openai_codex.CodexAuthError("Device login could not start: HTTP 500")

    monkeypatch.setattr(openai_codex, "login_with_device_code", broken)
    with pytest.raises(feature_rpc.FeatureRpcError) as failed:
        await feature_rpc.provider_login_start({"key": "openai_codex"})
    assert failed.value.code == "LOGIN_FAILED"
