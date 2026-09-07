import hashlib
import json
import secrets

import pytest

from flowly.mcp.external_access import ExternalAccessError, ExternalAccessStore


@pytest.fixture
def state(tmp_path):
    now = [1000.0]
    store = ExternalAccessStore(tmp_path / "mcp-access.json", clock=lambda: now[0])
    token = "flm_" + secrets.token_hex(32)
    params = {
        "id": "request-1234567890", "label": "Work agent", "sessionKey": "web:owner",
        "tools": ["messages_read"], "tokenDigest": hashlib.sha256(token.encode()).hexdigest(), "ttlSeconds": 60,
    }
    return store, token, params, now


def test_private_digest_only_idempotent_creation(state):
    store, token, params, now = state
    first = store.create(params, available={"messages_read"})
    now[0] += 5
    assert store.create(params, available={"messages_read"}) == first
    assert store.authorize(token, "messages_read")["id"] == first["id"]
    assert token not in store.path.read_text()
    assert "digest" not in json.dumps(store.list()).lower()
    assert store.path.stat().st_mode & 0o077 == 0
    with pytest.raises(ExternalAccessError):
        store.create({**params, "label": "Changed"}, available={"messages_read"})


def test_revocation_is_durable_and_does_not_recreate_or_widen(state):
    store, token, params, _ = state
    store.create(params, available={"messages_read"})
    with pytest.raises(ExternalAccessError):
        store.authorize(token, "messages_send")
    result = store.revoke(params["id"])
    assert result["status"] == "revoked"
    assert store.revoke(params["id"]) == result
    assert store.create(params, available={"messages_read"}) == result
    with pytest.raises(ExternalAccessError):
        ExternalAccessStore(store.path).authorize(token)


def test_expiry_and_profile_isolation(state, tmp_path):
    store, token, params, now = state
    store.create(params, available={"messages_read"})
    with pytest.raises(ExternalAccessError):
        ExternalAccessStore(tmp_path / "other.json").authorize(token)
    now[0] += 60
    assert store.list()[0]["status"] == "expired"
    with pytest.raises(ExternalAccessError):
        store.authorize(token)


@pytest.mark.parametrize("override", [
    {"tools": []}, {"tools": ["messages_send"]}, {"tools": ["messages_read", "messages_read"]},
    {"tools": "messages_read"}, {"ttlSeconds": True}, {"ttlSeconds": 0}, {"ttlSeconds": 90 * 86400 + 1},
    {"tokenDigest": "plaintext"}, {"sessionKey": ""}, {"label": "\n"}, {"id": "../secret"},
])
def test_invalid_owner_requests_never_publish(state, override):
    store, _, params, _ = state
    with pytest.raises(ExternalAccessError):
        store.create({**params, **override}, available={"messages_read"})
    assert not store.path.exists()


def test_corrupt_store_fails_closed_without_overwrite(state):
    store, token, params, _ = state
    store.path.write_text('{"version":1,"credentials":[{}]}')
    original = store.path.read_bytes()
    for operation in (lambda: store.authorize(token), store.list, lambda: store.create(params, available={"messages_read"})):
        with pytest.raises(ExternalAccessError):
            operation()
    assert store.path.read_bytes() == original


def test_capacity_preserves_revocation_records_and_fails_closed(state, monkeypatch):
    store, token, params, _ = state
    monkeypatch.setattr("flowly.mcp.external_access.MAX_KEYS", 1)
    store.create(params, available={"messages_read"})
    store.revoke(params["id"])
    with pytest.raises(ExternalAccessError, match="limit reached"):
        store.create({**params, "id": "request-other-123456", "tokenDigest": "b" * 64}, available={"messages_read"})
    assert store.create(params, available={"messages_read"})["status"] == "revoked"
    with pytest.raises(ExternalAccessError):
        store.authorize(token)
