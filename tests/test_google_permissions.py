import json
import time

import httpx
import pytest

from flowly.integrations.gmail_connection import (
    BROKER_API,
    BROKER_ORIGIN,
    GmailConnection,
    GmailConnectionError,
    atomic_private_json,
)
from flowly.integrations.google_permissions import PREFIX, granted_services, normalize_services


@pytest.mark.parametrize("value", [[], ["drive"], ["gmail", "gmail"], ["gmail", "admin"], "gmail", [None]])
def test_invalid_service_selection(value):
    with pytest.raises(ValueError):
        normalize_services(value)


def test_actual_scopes_and_requested_services_both_bound_access():
    credentials = {"services": ["gmail", "gmail_manage", "tasks"], "scopes": PREFIX + "gmail.modify " + PREFIX + "drive"}
    assert granted_services(credentials) == ["gmail", "gmail_manage"]
    credentials["scopes"] = PREFIX + "gmail.readonly " + PREFIX + "gmail.send"
    assert granted_services(credentials) == ["gmail"]


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    old_id, new_id = "a" * 32, "c" * 32
    state = {"email": "me@example.test", "outage": False, "retire_outage": False, "revoked": [], "bodies": []}
    def request(req):
        if state["outage"]:
            return httpx.Response(503, json={})
        if str(req.url) == BROKER_API:
            state["bodies"].append(json.loads(req.content))
            return httpx.Response(200, json={"requestId": new_id, "secret": "d" * 43, "verificationCode": "AABBCCDD",
                "expiresAt": (time.time() + 900) * 1000, "authorizationUrl": f"{BROKER_ORIGIN}/en/gmail/connect?request={new_id}"})
        if req.url.host == "gmail.googleapis.com":
            return httpx.Response(200, json={"emailAddress": state["email"]})
        if req.method == "GET":
            return httpx.Response(200, json={"status": "authorized"})
        if json.loads(req.content)["action"] == "disconnect":
            if state["retire_outage"] and req.url.path.endswith(old_id):
                return httpx.Response(503, json={})
            state["revoked"].append(req.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"status": "revoked"})
        return httpx.Response(200, json={"accessToken": "new-access", "expiresAt": (time.time() + 3600) * 1000,
            "email": state["email"], "scope": PREFIX + "gmail.modify"})
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        service = GmailConnection(client=client)
        old = {"mode": "flowly_broker", "issuer": BROKER_ORIGIN, "grant_id": old_id, "grant_secret": "b" * 43,
            "email": state["email"], "access_token": "old-access", "scopes": PREFIX + "gmail.readonly " + PREFIX + "gmail.send"}
        atomic_private_json(service.credentials, old)
        yield service, state, old, new_id


def test_upgrade_preserves_old_until_verified_and_reports_partial_permissions(upgrade):
    service, state, old, new_id = upgrade
    setup = service.begin(services=["gmail", "gmail_manage", "tasks"], connection_id=old["grant_id"])
    assert json.loads(service.credentials.read_text()) == old
    assert "superseded" not in setup and "grant_secret" not in json.dumps(setup)
    assert state["bodies"][0]["services"] == ["gmail", "gmail_manage", "tasks"]
    result = service.setup_status(new_id)
    assert result["services"] == ["gmail", "gmail_manage"]
    assert result["requestedServices"] == ["gmail", "gmail_manage", "tasks"]
    assert state["revoked"] == [old["grant_id"]]
    assert "superseded" not in json.loads(service.credentials.read_text())


def test_wrong_account_never_replaces_old_connection(upgrade):
    service, state, old, new_id = upgrade
    service.begin(services=["gmail", "gmail_manage"], connection_id=old["grant_id"])
    state["email"] = "different@example.test"
    with pytest.raises(GmailConnectionError, match="GMAIL_ACCOUNT_MISMATCH"):
        service.setup_status(new_id)
    assert json.loads(service.credentials.read_text()) == old
    service.cancel(new_id)
    assert state["revoked"] == [new_id]
    assert json.loads(service.credentials.read_text()) == old


def test_stale_upgrade_and_changed_selection_fail_without_new_grants(upgrade):
    service, state, old, new_id = upgrade
    with pytest.raises(GmailConnectionError, match="CONNECTION_CHANGED"):
        service.begin(connection_id="e" * 32)
    service.begin(services=["gmail", "gmail_manage"], connection_id=old["grant_id"])
    with pytest.raises(GmailConnectionError, match="SETUP_IN_PROGRESS"):
        service.begin(services=["gmail", "tasks"], connection_id=old["grant_id"])
    assert len(state["bodies"]) == 1


def test_disconnect_outage_blocks_both_old_access_and_pending_upgrade(upgrade):
    service, state, old, new_id = upgrade
    service.begin(connection_id=old["grant_id"])
    state["outage"] = True
    assert service.disconnect(old["grant_id"])["status"] == "disconnect_pending"
    assert service.access_token() == (None, None)
    state["outage"] = False
    with pytest.raises(GmailConnectionError, match="CONNECTION_CHANGED"):
        service.setup_status(new_id)
    assert service.disconnect(old["grant_id"])["status"] == "not_configured"
    assert not service.pending.exists() and not service.credentials.exists()


def test_superseded_revocation_retries_after_commit_and_cancel_cannot_erase_new(upgrade):
    service, state, old, new_id = upgrade
    service.begin(connection_id=old["grant_id"])
    state["retire_outage"] = True
    assert service.setup_status(new_id)["cleanupPending"] is True
    assert service.pending.exists()
    with pytest.raises(GmailConnectionError, match="SETUP_NOT_FOUND"):
        service.cancel(new_id)
    state["retire_outage"] = False
    assert service.setup_status(new_id)["cleanupPending"] is False
    assert not service.pending.exists()


def test_permission_gates_update_without_restart(tmp_path, monkeypatch):
    from flowly.channels.gmail_auth import google_tool_ready
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"channels": {"email": {"enabled": True}}}))
    path = tmp_path / "credentials/gmail.json"
    creds = {"mode": "flowly_broker", "issuer": BROKER_ORIGIN, "grant_id": "a" * 32, "grant_secret": "b" * 43,
        "services": ["gmail", "tasks"], "scopes": PREFIX + "gmail.modify"}
    atomic_private_json(path, creds)
    assert not google_tool_ready("tasks")
    creds["scopes"] += " " + PREFIX + "tasks"
    atomic_private_json(path, creds)
    assert google_tool_ready("tasks")
    creds["disconnect_pending"] = True
    atomic_private_json(path, creds)
    assert not google_tool_ready("tasks")
