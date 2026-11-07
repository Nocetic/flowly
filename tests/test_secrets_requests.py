"""Secret requests, the agent's tools, the RPCs and a command's view of the vault."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.secrets import ListSecretsTool, RequestSecretTool
from flowly.secrets import envelope, requests as secret_requests, service, vault as vault_mod

from tests.test_secrets_vault import FakeKeyring, keychain  # noqa: F401  (fixture)

SESSION = "web:conversation-1"


class Surfaces:
    """What the gateway wires: the cards shown and the turns continued."""

    def __init__(self, manager: secret_requests.SecretRequests) -> None:
        self.requested: list[dict] = []
        self.closed: list[dict] = []
        self.continued: list[tuple[str, str, str, str]] = []

        async def on_requested(request):
            self.requested.append(request.to_wire())

        async def on_closed(request, reason):
            self.closed.append(secret_requests.closed_to_wire(request, reason))

        async def continuation(*args):
            self.continued.append(args)

        manager.add_requested_callback(on_requested)
        manager.add_closed_callback(on_closed)
        manager.set_continuation(continuation)


@pytest.fixture
def surfaces(keychain):  # noqa: F811
    return Surfaces(secret_requests.get_secret_requests())


async def _ask(fields, *, session=SESSION, replace=False):
    tool = RequestSecretTool()
    tool.set_context("web", "conversation-1")
    registry = ToolRegistry()
    registry.register(tool)
    return json.loads(await registry.execute(
        "request_secret",
        {"title": "OpenRouter API key", "description": "To try models on OpenRouter.", "fields": fields,
         "replace": replace},
        session_key=session,
    ))


def _sealed(values, request_id):
    key = service.status()["key"]
    return {"names": list(values), "kid": key["kid"], "requestId": request_id,
            "sealed": envelope.seal(values, envelope.unb64url(key["publicKey"]), kid=key["kid"],
                                    request_id=request_id)}


@pytest.mark.asyncio
async def test_a_request_is_shown_answered_and_continues_the_conversation(surfaces):
    result = await _ask([{"name": "OPENROUTER_API_KEY", "label": "API key"}])
    assert result["status"] == "requested"
    [card] = surfaces.requested
    assert card["sessionKey"] == SESSION and card["fields"] == [{"name": "OPENROUTER_API_KEY", "label": "API key"}]
    assert card["key"]["kid"] == service.status()["key"]["kid"]
    assert service.pending_requests()["requests"][0]["id"] == result["requestId"]

    saved = await service.submit(_sealed({"OPENROUTER_API_KEY": "sk-or-v1-person-own"}, result["requestId"]))
    assert saved == {"saved": ["OPENROUTER_API_KEY"]}
    assert surfaces.closed == [{"id": result["requestId"], "sessionKey": SESSION, "reason": "saved",
                                "names": ["OPENROUTER_API_KEY"]}]
    [(session, channel, chat_id, notice)] = surfaces.continued
    assert (session, channel, chat_id) == (SESSION, "web", "conversation-1")
    assert "$OPENROUTER_API_KEY" in notice and "sk-or-v1-person-own" not in notice
    assert service.pending_requests() == {"requests": []}
    # Answered once: a second answer finds nothing waiting.
    with pytest.raises(vault_mod.SecretsError) as error:
        await service.submit(_sealed({"OPENROUTER_API_KEY": "sk-or-v1-other"}, result["requestId"]))
    assert error.value.code == "REQUEST_NOT_FOUND"


@pytest.mark.asyncio
async def test_values_sealed_for_other_names_than_asked_are_refused(surfaces):
    result = await _ask([{"name": "OPENROUTER_API_KEY", "label": "API key"}])
    with pytest.raises(vault_mod.SecretsError) as error:
        await service.submit(_sealed({"OTHER_API_KEY": "sk-other-value"}, result["requestId"]))
    assert error.value.code == "NAMES_MISMATCH"
    assert service.saved_names() == []


@pytest.mark.asyncio
async def test_declining_continues_without_the_value(surfaces):
    result = await _ask([{"name": "FAL_KEY", "label": "Key"}])
    assert await service.decline({"requestId": result["requestId"]}) == {"declined": True}
    assert surfaces.closed[0]["reason"] == "declined"
    assert "declined" in surfaces.continued[0][3]
    with pytest.raises(vault_mod.SecretsError):
        await service.decline({"requestId": result["requestId"]})


@pytest.mark.asyncio
async def test_a_later_request_for_the_same_name_supersedes_without_a_turn(surfaces):
    first = await _ask([{"name": "FAL_KEY", "label": "Key"}])
    second = await _ask([{"name": "FAL_KEY", "label": "Key"}, {"name": "FAL_SECRET", "label": "Secret"}])
    assert surfaces.closed == [{"id": first["requestId"], "sessionKey": SESSION, "reason": "superseded",
                                "names": ["FAL_KEY"]}]
    assert surfaces.continued == []
    assert [r["id"] for r in service.pending_requests()["requests"]] == [second["requestId"]]


@pytest.mark.asyncio
async def test_a_request_expires_once_and_survives_a_restart(surfaces, monkeypatch):
    result = await _ask([{"name": "FAL_KEY", "label": "Key"}])
    secret_requests._reset_for_tests()  # a new process
    manager = secret_requests.get_secret_requests()
    later = Surfaces(manager)
    assert [r.id for r in manager.pending()] == [result["requestId"]]
    real_time = time.time
    monkeypatch.setattr(secret_requests.time, "time", lambda: real_time() + secret_requests.LIFETIME_SECONDS + 1)
    assert await manager.sweep() == 1
    assert await manager.sweep() == 0
    assert later.closed[0]["reason"] == "expired" and "expired" in later.continued[0][3]


@pytest.mark.asyncio
async def test_saving_from_settings_answers_a_waiting_request(surfaces):
    result = await _ask([{"name": "FAL_KEY", "label": "Key"}])
    await service.submit(_sealed({"FAL_KEY": "fal-secret-value-123"}, None))
    assert surfaces.closed[0] == {"id": result["requestId"], "sessionKey": SESSION, "reason": "saved",
                                  "names": ["FAL_KEY"]}


@pytest.mark.asyncio
async def test_a_saved_name_is_not_asked_again_unless_replaced(surfaces):
    service_vault = vault_mod.get_vault()
    service_vault.put({"FAL_KEY": "fal-secret-value-123"})
    assert (await _ask([{"name": "FAL_KEY", "label": "Key"}]))["status"] == "already_saved"
    assert surfaces.requested == []
    assert (await _ask([{"name": "FAL_KEY", "label": "Key"}], replace=True))["status"] == "requested"


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["desktop:profile-inbox:other:abc", ""])
async def test_nobody_to_answer_means_no_request(surfaces, session):
    tool = RequestSecretTool()
    fields = [{"name": "FAL_KEY", "label": "Key"}]
    if session:
        with tool_execution_scope(session):
            result = json.loads(await tool.execute(title="t", description="d", fields=fields))
    else:
        result = json.loads(await tool.execute(title="t", description="d", fields=fields))
    assert "error" in result and surfaces.requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [[], [{"name": "PATH", "label": "x"}],
                                    [{"name": "A_KEY", "label": "a"}, {"name": "A_KEY", "label": "b"}],
                                    [{"name": f"K_{i}", "label": "x"} for i in range(7)]])
async def test_bad_requests_are_refused(surfaces, fields):
    assert "error" in await _ask(fields)
    assert surfaces.requested == []


@pytest.mark.asyncio
async def test_list_secrets_shows_names_never_values(surfaces):
    vault_mod.get_vault().put({"FAL_KEY": "fal-secret-value-123"}, "Images")
    await _ask([{"name": "OPENROUTER_API_KEY", "label": "Key"}])
    listed = await ListSecretsTool().execute()
    assert "fal-secret-value-123" not in listed
    body = json.loads(listed)
    assert body["secrets"] == [{"name": "FAL_KEY", "description": "Images"}]
    assert body["waiting"][0]["names"] == ["OPENROUTER_API_KEY"]


# ── RPCs ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_rpcs_are_served_and_map_refusals(surfaces):
    from flowly.channels import feature_rpc

    status, _ = await feature_rpc.dispatch("secrets.status", {})
    assert set(status["key"]) == {"kid", "publicKey"} and status["count"] == 0
    saved, _ = await feature_rpc.dispatch("secrets.submit", _sealed({"FAL_KEY": "fal-secret-value-123"}, None))
    assert saved == {"saved": ["FAL_KEY"]}
    listed, _ = await feature_rpc.dispatch("secrets.list", {})
    assert [s["name"] for s in listed["secrets"]] == ["FAL_KEY"] and "fal-secret-value-123" not in json.dumps(listed)
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch("secrets.submit", {**_sealed({"FAL_KEY": "x" * 20}, None), "kid": "0" * 16})
    assert error.value.code == "KEY_CHANGED"
    deleted, _ = await feature_rpc.dispatch("secrets.delete", {"name": "FAL_KEY"})
    assert deleted == {"deleted": True}
    requests_, _ = await feature_rpc.dispatch("secrets.requests", {})
    assert requests_ == {"requests": []}


def test_a_named_agent_serves_the_rpcs_through_its_host():
    from flowly.profile_host_contract import validate_profile_rpc

    for method in ("secrets.requests", "secrets.status", "secrets.list", "secrets.submit", "secrets.decline",
                   "secrets.delete"):
        assert validate_profile_rpc(method, {})[0] == method


def test_the_cards_reach_the_server_for_a_named_agent():
    from flowly.channels.flowly_server import PROFILE_CHAT_EVENTS

    assert {"secret.requested", "secret.closed"} <= PROFILE_CHAT_EVENTS


# ── a command's view ───────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses /bin/sh subprocess")
async def test_a_command_gets_the_value_and_its_output_never_shows_it(keychain, tmp_path):  # noqa: F811
    from flowly.agent.tools.shell import SecureExecTool
    from flowly.exec.approvals import ExecApprovalStore
    from flowly.exec.types import ExecConfig

    store = ExecApprovalStore()
    policy = store.load()
    policy.security, policy.ask = "full", "off"
    store.save()
    vault_mod.get_vault().put({"OPENROUTER_API_KEY": "sk-or-v1-person-own-123"})
    registry = ToolRegistry()
    registry.register(SecureExecTool(ExecConfig(security="full"), working_dir=str(tmp_path)))
    output = await registry.execute(
        "exec", {"command": 'printenv OPENROUTER_API_KEY; test "$OPENROUTER_API_KEY" = sk-or-v1-person-own-123 && echo same'},
        session_key=SESSION)
    assert "sk-or-v1-person-own-123" not in output
    assert "[secret:OPENROUTER_API_KEY]" in output and "same" in output
