"""Independent Google grants, shared credential migration and service boundaries."""

import asyncio
import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from flowly.integrations.gmail_connection import (
    BROKER_API,
    BROKER_ORIGIN,
    MANAGED_MODE,
    GmailConnection,
    GmailConnectionError,
    atomic_private_json,
)
from flowly.integrations.google_chat import GoogleChatRequests
from flowly.integrations.google_permissions import CONNECTION_SERVICES, SERVICE_SCOPES


@pytest.fixture
def google(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    state = {"grants": {}, "calls": [], "revoked": [], "offline": False, "subject": "owner-123"}

    def request(req):
        state["calls"].append(req)
        if req.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": "owner@example.test", "email_verified": True, "sub": state["subject"]})
        if req.url.host == "gmail.googleapis.com":
            return httpx.Response(200, json={"emailAddress": "owner@example.test"})
        if str(req.url) == BROKER_API:
            body = json.loads(req.content)
            grant_id = f"{len(state['grants']) + 1:032x}"
            state["grants"][grant_id] = body["services"]
            return httpx.Response(200, json={"requestId": grant_id, "secret": "s" * 43,
                "authorizationUrl": f"{BROKER_ORIGIN}/{body['locale']}/gmail/connect?request={grant_id}",
                "verificationCode": "ABCDEF12", "expiresAt": (time.time() + 900) * 1000})
        grant_id = req.url.path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(200, json={"status": "authorized"})
        action = json.loads(req.content)["action"]
        if action == "disconnect":
            if state["offline"]:
                return httpx.Response(503, json={"error": {"code": "UNAVAILABLE"}})
            state["revoked"].append(grant_id)
            return httpx.Response(200, json={"status": "revoked"})
        services = state["grants"][grant_id]
        return httpx.Response(200, json={"accessToken": "access-" + grant_id,
            "email": "owner@example.test", "subject": "owner-123", "expiresAt": (time.time() + 3600) * 1000,
            "scope": " ".join(scope for service in services for scope in SERVICE_SCOPES[service])})

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        yield lambda service: GmailConnection(client=client, service=service), state


@pytest.mark.parametrize("service", ["calendar", "drive", "contacts", "tasks"])
def test_connect_without_gmail_and_without_enabling_email(google, service):
    create, state = google
    connection = create(service)
    setup = connection.begin()
    assert setup["service"] == service
    assert setup["services"] == [service]
    result = connection.setup_status(setup["requestId"])
    assert result["connected"] is True
    assert result["services"] == [service]
    assert not (connection.home / "config.json").exists()
    assert all(req.url.host != "gmail.googleapis.com" for req in state["calls"])
    assert stat.S_IMODE(connection.credentials.stat().st_mode) == 0o600
    assert "access-" not in json.dumps(result)
    assert connection.status()["connected"] is True


def test_simultaneous_setups_and_disconnect_are_service_scoped(google):
    create, state = google
    drive, tasks = create("drive"), create("tasks")
    first, second = drive.begin(), tasks.begin()
    assert drive.pending != tasks.pending
    with pytest.raises(GmailConnectionError, match="SETUP_NOT_FOUND"):
        drive.cancel(second["requestId"])
    drive.setup_status(first["requestId"])
    tasks.setup_status(second["requestId"])
    state["offline"] = True
    assert drive.disconnect(first["requestId"])["status"] == "disconnect_pending"
    assert drive.access_token() == (None, None)
    assert tasks.access_token()[0]
    state["offline"] = False
    assert drive.disconnect(first["requestId"])["status"] == "not_configured"
    assert state["revoked"] == [first["requestId"]]
    assert tasks.status()["connected"]


def shared(create, state, *, native=False, unknown=False):
    connection = create("drive")
    data = {"email": "owner@example.test", "refresh_token": "legacy-refresh"}
    if not unknown:
        data["scopes"] = " ".join(SERVICE_SCOPES["gmail"] + SERVICE_SCOPES["drive"] + SERVICE_SCOPES["tasks"])
    if not native:
        data.update(mode=MANAGED_MODE, issuer=BROKER_ORIGIN, grant_id="a" * 32, grant_secret="s" * 43)
        state["grants"]["a" * 32] = ["gmail", "drive", "tasks"]
    path = connection.home / "credentials" / "gmail.json"
    atomic_private_json(path, data)
    return data, path


def test_legacy_detach_only_revokes_the_last_service_and_retries(google):
    create, state = google
    data, path = shared(create, state)
    grant_id = GmailConnection.connection_id(data)
    for service in ["drive", "gmail"]:
        assert create(service).disconnect(grant_id)["status"] == "not_configured"
        assert create(service)._read_credentials() is None
        assert path.exists()
        assert state["revoked"] == []
    state["offline"] = True
    assert create("tasks").disconnect(grant_id)["status"] == "disconnect_pending"
    assert create("tasks").access_token() == (None, None)
    state["offline"] = False
    assert create("tasks").disconnect(grant_id)["status"] == "not_configured"
    assert not path.exists()


@pytest.mark.parametrize("native", [True, False])
def test_upgrading_shared_grant_cannot_resurrect_it_after_disconnect(google, native):
    create, state = google
    data, path = shared(create, state, native=native)
    connection = create("drive")
    setup = connection.begin(connection_id=GmailConnection.connection_id(data))
    connection.setup_status(setup["requestId"])
    assert "drive" in json.loads(path.read_text())["disabled_services"]
    connection.disconnect(setup["requestId"])
    assert create("drive")._read_credentials() is None
    assert create("gmail")._read_credentials()
    assert create("tasks")._read_credentials()


def test_unknown_legacy_scopes_preserve_every_other_service(google):
    create, state = google
    data, path = shared(create, state, native=True, unknown=True)
    for service in CONNECTION_SERVICES:
        assert create(service)._read_credentials()
        create(service).disconnect(GmailConnection.connection_id(data))
        assert create(service)._read_credentials() is None
    assert not path.exists()


def test_identity_and_permission_checks_precede_commit(google):
    create, state = google
    connection = create("drive")
    setup = connection.begin()
    state["subject"] = "different-subject"
    with pytest.raises(GmailConnectionError, match="GMAIL_ACCOUNT_MISMATCH"):
        connection.setup_status(setup["requestId"])
    assert not connection.credentials.exists()
    state["subject"] = "owner-123"
    state["grants"][setup["requestId"]] = ["tasks"]
    with pytest.raises(GmailConnectionError, match="PERMISSION_REQUIRED"):
        connection.setup_status(setup["requestId"])
    assert not connection.credentials.exists()


def test_native_refresh_cannot_restore_a_concurrently_detached_service(google, monkeypatch):
    from flowly.channels.gmail_auth import get_valid_access_token

    create, state = google
    data, path = shared(create, state, native=True)
    data.update(client_id="fixture-client", client_secret="fixture-secret", expiry="2000-01-01T00:00:00+00:00")
    atomic_private_json(path, data)
    refreshing, release = threading.Event(), threading.Event()

    def refresh(*args, **kwargs):
        refreshing.set()
        assert release.wait(3)
        return httpx.Response(200, json={"access_token": "renewed", "expires_in": 3600})

    monkeypatch.setattr("flowly.channels.gmail_auth.httpx.post", refresh)
    with ThreadPoolExecutor(max_workers=2) as workers:
        token = workers.submit(get_valid_access_token, "drive")
        assert refreshing.wait(3)
        disconnected = workers.submit(create("drive").disconnect, GmailConnection.connection_id(data))
        release.set()
        assert token.result(timeout=3)[0] == "renewed"
        assert disconnected.result(timeout=3)["status"] == "not_configured"
    assert "drive" in json.loads(path.read_text())["disabled_services"]
    assert get_valid_access_token("drive") == (None, None)
    assert get_valid_access_token("tasks")[0] == "renewed"


@pytest.mark.parametrize("service", ["../gmail", "gmail_manage", "", "unknown"])
def test_unknown_service_never_selects_a_credentials_path(service):
    with pytest.raises(GmailConnectionError, match="INVALID_SERVICE"):
        GmailConnection(service=service)


async def test_chat_request_cannot_be_switched_to_another_service():
    manager = GoogleChatRequests()
    waiting = asyncio.create_task(manager.request("desktop:conversation", "Read the file", ["drive"], service="drive"))
    await asyncio.sleep(0)
    req = manager.pending("desktop:conversation")["requests"][0]
    assert req["service"] == "drive"
    with pytest.raises(GmailConnectionError, match="INVALID_SERVICE"):
        await manager.begin(req["id"], req["sessionKey"], service="gmail")
    await manager.cancel(req["id"], req["sessionKey"])
    assert (await waiting)["status"] == "cancelled"
