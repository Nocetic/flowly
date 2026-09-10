"""Gmail setup stays on the selected runtime; no real account/network is used."""

import json
import stat
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

ID = "a" * 32
SECRET = "b" * 43


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    state = {"status": "pending", "requests": [], "disconnected": False, "unavailable": False}

    def request(req):
        state["requests"].append(req)
        if state["unavailable"]:
            return httpx.Response(503, json={"error": {"code": "UNAVAILABLE", "details": "secret"}})
        if str(req.url) == BROKER_API:
            body = json.loads(req.content)
            return httpx.Response(200, json={
                "requestId": ID, "secret": SECRET, "verificationCode": "AABBCCDD",
                "expiresAt": (time.time() + 900) * 1000,
                "authorizationUrl": f"{BROKER_ORIGIN}/{body['locale']}/gmail/connect?request={ID}",
            })
        if req.url.host == "gmail.googleapis.com":
            assert req.headers["Authorization"] == "Bearer short-access"
            return httpx.Response(200, json={"emailAddress": "me@example.test"})
        assert req.headers["Authorization"] == f"Bearer {SECRET}"
        if req.method == "GET":
            return httpx.Response(200, json={"status": state["status"]})
        body = json.loads(req.content)
        if body["action"] == "disconnect":
            state["disconnected"] = True
            return httpx.Response(200, json={"status": "revoked"})
        return httpx.Response(200, json={"accessToken": "short-access", "expiresAt": (time.time() + 3600) * 1000, "email": "me@example.test", "scope": "gmail"})

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        service = GmailConnection(client=client)
        yield service, state


def test_begin_redacts_secrets_and_resumes_after_restart(setup):
    service, state = setup
    public = service.begin(locale="tr", label="My agent")
    assert public["verificationCode"] == "AABBCCDD"
    assert SECRET not in json.dumps(public)
    assert "grant_secret" not in public
    assert json.loads(service.pending.read_text())["grant_secret"] == SECRET
    assert stat.S_IMODE(service.pending.stat().st_mode) == 0o600
    restarted = GmailConnection(client=service._client)
    assert restarted.begin(locale="en") == public
    assert len(state["requests"]) == 1


def test_success_checks_gmail_before_connected_and_preserves_unrelated_config(setup):
    service, state = setup
    original = {"agents": {"defaults": {"model": "my-existing-model"}}, "channels": {"email": {"pollInterval": 333}}, "extraUserSetting": {"keep": True}}
    (service.home / "config.json").write_text(json.dumps(original))
    service.begin()
    state["status"] = "authorized"
    result = service.setup_status(ID)
    assert result["connected"] is True
    assert result["email"] == "me@example.test"
    assert "short-access" not in json.dumps(result)
    assert not service.pending.exists()
    config = json.loads((service.home / "config.json").read_text())
    assert config["agents"] == original["agents"]
    assert config["extraUserSetting"] == original["extraUserSetting"]
    assert config["channels"]["email"]["pollInterval"] == 333
    assert config["channels"]["email"]["enabled"] is True
    assert service.setup_status(ID)["connected"] is True


def test_does_not_overwrite_existing_credentials(setup):
    service, state = setup
    atomic_private_json(service.credentials, {"refresh_token": "legacy-refresh", "email": "existing@example.test"})
    with pytest.raises(GmailConnectionError, match="ALREADY_CONFIGURED"):
        service.begin()
    assert not state["requests"]
    assert json.loads(service.credentials.read_text())["refresh_token"] == "legacy-refresh"


def test_expired_setup_never_claims_google_grant(setup):
    service, state = setup
    service.begin()
    grant = json.loads(service.pending.read_text())
    grant["expires_at"] = 1
    atomic_private_json(service.pending, grant)
    assert service.setup_status(ID)["status"] == "expired"
    assert len(state["requests"]) == 1
    assert not service.credentials.exists()


def test_wrong_request_cannot_cancel_another_setup(setup):
    service, state = setup
    service.begin()
    with pytest.raises(GmailConnectionError, match="SETUP_NOT_FOUND"):
        service.cancel("c" * 32)
    assert not state["disconnected"]
    service.cancel(ID)
    assert state["disconnected"]
    assert not service.pending.exists()


def test_disconnect_outage_stops_local_access_and_can_retry(setup):
    service, state = setup
    service.begin()
    state["status"] = "authorized"
    service.setup_status(ID)
    state["unavailable"] = True
    assert service.disconnect(ID)["status"] == "disconnect_pending"
    assert service.access_token() == (None, None)
    assert service.status()["connected"] is False
    state["unavailable"] = False
    assert service.disconnect(ID)["status"] == "not_configured"
    assert state["disconnected"]
    assert not service.credentials.exists()


def test_stale_disconnect_does_not_remove_current_connection(setup):
    service, state = setup
    service.begin()
    state["status"] = "authorized"
    service.setup_status(ID)
    with pytest.raises(GmailConnectionError, match="CONNECTION_CHANGED"):
        service.disconnect("c" * 32)
    assert service.credentials.exists()
    assert not state["disconnected"]


def test_status_does_not_infer_connected_from_a_file(setup):
    service, state = setup
    service.begin()
    state["status"] = "authorized"
    service.setup_status(ID)
    assert service.status(verify=False)["status"] == "saved"
    state["unavailable"] = True
    assert service.status()["connected"] is False
    assert service.status()["error"] == {"code": "UNAVAILABLE"}


def test_profiles_do_not_share_pending_setup(setup, tmp_path):
    service, _ = setup
    service.begin()
    other = GmailConnection(home=tmp_path / "other", client=service._client)
    assert other.pending_setup() is None
    assert other.status()["status"] == "not_configured"


def test_server_cannot_redirect_tokens_or_browser_to_another_host(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    def request(req):
        return httpx.Response(200, json={
            "requestId": ID, "secret": SECRET, "verificationCode": "AABBCCDD", "expiresAt": (time.time() + 900) * 1000,
            "authorizationUrl": f"https://attacker.test/en/gmail/connect?request={ID}",
        })
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        service = GmailConnection(client=client)
        with pytest.raises(GmailConnectionError, match="INVALID_RESPONSE"):
            service.begin()
        assert not service.pending.exists()


def test_atomic_storage_rejects_symlink(tmp_path):
    target = tmp_path / "original.json"
    target.write_text("unchanged")
    link = tmp_path / "gmail.json"
    link.symlink_to(target)
    with pytest.raises(GmailConnectionError, match="SECURE_STORAGE_UNAVAILABLE"):
        atomic_private_json(link, {"secret": "new"})
    assert target.read_text() == "unchanged"


def test_partial_save_can_finish_after_setup_expiry(setup, monkeypatch):
    service, state = setup
    service.begin()
    state["status"] = "authorized"
    enable = service._enable_email
    monkeypatch.setattr(service, "_enable_email", lambda: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(OSError):
        service.setup_status(ID)
    assert service.credentials.exists() and service.pending.exists()
    grant = json.loads(service.pending.read_text())
    grant["expires_at"] = 1
    atomic_private_json(service.pending, grant)
    state["status"] = "active"
    monkeypatch.setattr(service, "_enable_email", enable)
    assert service.pending_setup()["status"] == "pending"
    assert service.begin()["requestId"] == ID
    assert service.setup_status(ID)["connected"] is True
    assert not service.pending.exists()


def test_cancelling_partial_save_stops_local_access_even_during_outage(setup, monkeypatch):
    service, state = setup
    service.begin()
    state["status"] = "authorized"
    monkeypatch.setattr(service, "_enable_email", lambda: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(OSError):
        service.setup_status(ID)
    state["unavailable"] = True
    with pytest.raises(GmailConnectionError):
        service.cancel(ID)
    assert service.access_token() == (None, None)
    assert service.setup_status(ID)["status"] == "cancelled"
    state["unavailable"] = False
    service.cancel(ID)
    assert not service.credentials.exists() and not service.pending.exists()


@pytest.mark.asyncio
async def test_token_refresh_runs_outside_gateway_event_loop(monkeypatch):
    import threading

    from flowly.agent.tools.email import EmailTool

    loop_thread = threading.get_ident()
    def token():
        assert threading.get_ident() != loop_thread
        return None, None
    monkeypatch.setattr("flowly.channels.gmail_auth.get_valid_access_token", token)
    assert "flowly gmail connect" in await EmailTool().execute("inbox")
