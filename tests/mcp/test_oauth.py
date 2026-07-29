"""Tests for OAuth support (Faz 2b).

We can't drive a real authorization server in a unit test, so we cover
the pieces Flowly owns:

* ``FlowlyTokenStorage`` round-trips client info + tokens to
  ``$FLOWLY_HOME/mcp-tokens/{server}.json`` with mode 0600.
* ``clear_tokens`` / ``has_tokens`` lifecycle.
* ``build_oauth_provider`` returns a provider when the SDK supports it,
  and its non-interactive handlers refuse to launch a browser.
* The localhost callback server captures ``code`` + ``state``.

Skipped if the SDK lacks the auth module.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from flowly.mcp import oauth

pytestmark = pytest.mark.skipif(
    not oauth.oauth_available(),
    reason="mcp SDK lacks OAuth support",
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


def test_token_storage_round_trip(isolated_home: Path):
    from mcp.shared.auth import OAuthToken

    storage = oauth.FlowlyTokenStorage("acme")
    assert asyncio.run(storage.get_tokens()) is None

    token = OAuthToken(access_token="abc123", token_type="Bearer", refresh_token="r1")
    asyncio.run(storage.set_tokens(token))

    loaded = asyncio.run(storage.get_tokens())
    assert loaded is not None
    assert loaded.access_token == "abc123"
    assert loaded.refresh_token == "r1"


def test_token_file_is_0600(isolated_home: Path):
    from mcp.shared.auth import OAuthToken

    storage = oauth.FlowlyTokenStorage("acme")
    asyncio.run(storage.set_tokens(OAuthToken(access_token="x", token_type="Bearer")))

    path = isolated_home / "mcp-tokens" / "acme.json"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_client_info_round_trip(isolated_home: Path):
    from mcp.shared.auth import OAuthClientInformationFull

    storage = oauth.FlowlyTokenStorage("acme")
    assert asyncio.run(storage.get_client_info()) is None

    info = OAuthClientInformationFull(
        client_id="cid-1",
        redirect_uris=["http://127.0.0.1:8765/callback"],
    )
    asyncio.run(storage.set_client_info(info))
    loaded = asyncio.run(storage.get_client_info())
    assert loaded is not None
    assert loaded.client_id == "cid-1"


def test_tokens_and_client_info_coexist(isolated_home: Path):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    storage = oauth.FlowlyTokenStorage("acme")
    asyncio.run(
        storage.set_client_info(
            OAuthClientInformationFull(
                client_id="cid", redirect_uris=["http://127.0.0.1:8765/callback"]
            )
        )
    )
    asyncio.run(storage.set_tokens(OAuthToken(access_token="tok", token_type="Bearer")))

    # Writing tokens must not clobber client info and vice versa.
    assert asyncio.run(storage.get_client_info()).client_id == "cid"
    assert asyncio.run(storage.get_tokens()).access_token == "tok"


def test_clear_and_has_tokens(isolated_home: Path):
    from mcp.shared.auth import OAuthToken

    storage = oauth.FlowlyTokenStorage("acme")
    asyncio.run(storage.set_tokens(OAuthToken(access_token="x", token_type="Bearer")))
    assert oauth.has_tokens("acme")
    assert oauth.clear_tokens("acme") is True
    assert not oauth.has_tokens("acme")
    # Clearing a non-existent file is a no-op returning False.
    assert oauth.clear_tokens("acme") is False


def test_token_backup_restore_is_atomic(isolated_home: Path):
    token_file = isolated_home / "mcp-tokens" / "acme.json"
    token_file.parent.mkdir(parents=True)
    original = b'{"tokens":{"access_token":"working"}}'
    token_file.write_bytes(original)

    backup = oauth.backup_tokens("acme")
    assert backup == (True, original)
    assert oauth.clear_tokens("acme") is True
    token_file.write_bytes(b'{"tokens":{"access_token":"partial"}}')

    assert oauth.restore_tokens("acme", backup) is True
    assert token_file.read_bytes() == original


def test_token_restore_removes_partial_file_when_none_existed(isolated_home: Path):
    backup = oauth.backup_tokens("new-server")
    assert backup == (False, b"")
    token_file = isolated_home / "mcp-tokens" / "new_server.json"
    token_file.write_bytes(b'{"partial":true}')

    assert oauth.restore_tokens("new-server", backup) is True
    assert not token_file.exists()


def test_sanitized_server_name_in_filename(isolated_home: Path):
    from mcp.shared.auth import OAuthToken

    storage = oauth.FlowlyTokenStorage("my server/v2")
    asyncio.run(storage.set_tokens(OAuthToken(access_token="x", token_type="Bearer")))
    files = list((isolated_home / "mcp-tokens").glob("*.json"))
    assert len(files) == 1
    # Name was sanitized to a filesystem-safe form.
    assert "/" not in files[0].name


def test_build_provider_returns_object(isolated_home: Path):
    provider = oauth.build_oauth_provider(
        "acme",
        "https://acme.example.com/mcp",
        interactive=False,
    )
    assert provider is not None


def test_callback_server_captures_code(isolated_home: Path, monkeypatch):
    import threading
    import time
    import urllib.request

    # Use a high, likely-free port to avoid clashing with anything.
    monkeypatch.setattr(oauth, "_CALLBACK_PORT", 8799)

    result = oauth._CallbackResult()
    server_thread = threading.Thread(
        target=oauth._run_callback_server,
        args=(result, 10.0),
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.3)  # let the server bind

    try:
        urllib.request.urlopen(
            "http://127.0.0.1:8799/callback?code=AUTHCODE&state=ST8",
            timeout=5,
        ).read()
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"callback request failed: {exc}")

    result.event.wait(timeout=5)
    assert result.code == "AUTHCODE"
    assert result.state == "ST8"


def test_callback_server_timeout_releases_thread(isolated_home: Path, monkeypatch):
    import threading
    import time

    closed = threading.Event()

    class _FakeHTTPServer:
        timeout = 0.01

        def __init__(self, *args, **kwargs):
            pass

        def handle_request(self):
            time.sleep(self.timeout)

        def server_close(self):
            closed.set()

    monkeypatch.setattr("http.server.HTTPServer", _FakeHTTPServer)
    result = oauth._CallbackResult()
    server_thread = threading.Thread(
        target=oauth._run_callback_server,
        args=(result, 0.05),
        daemon=True,
    )
    server_thread.start()
    server_thread.join(timeout=1.0)

    assert not server_thread.is_alive()
    assert result.event.is_set()
    assert result.error == "OAuth callback timed out without a code"
    assert closed.is_set()


def test_callback_server_bind_failure_is_immediate(isolated_home: Path, monkeypatch):
    class _FailingHTTPServer:
        def __init__(self, *args, **kwargs):
            raise OSError("address already in use")

    monkeypatch.setattr("http.server.HTTPServer", _FailingHTTPServer)
    result = oauth._CallbackResult()

    oauth._run_callback_server(result, 300)

    assert result.event.is_set()
    assert result.error is not None
    assert "could not bind localhost callback port" in result.error
    assert "address already in use" in result.error


@pytest.mark.asyncio
async def test_cancelled_callback_releases_server_thread(isolated_home: Path, monkeypatch):
    import threading

    observed: dict[str, oauth._CallbackResult] = {}
    closed = threading.Event()

    def _fake_server(result: oauth._CallbackResult, timeout: float) -> None:
        observed["result"] = result
        result.event.wait(timeout)
        closed.set()

    monkeypatch.setattr(oauth, "_run_callback_server", _fake_server)
    provider = oauth.build_oauth_provider(
        "acme",
        "https://acme.example.com/mcp",
        interactive=True,
    )
    callback_task = asyncio.create_task(provider.context.callback_handler())

    for _ in range(100):
        if "result" in observed:
            break
        await asyncio.sleep(0.01)
    assert "result" in observed

    callback_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await callback_task

    assert observed["result"].error == "OAuth callback cancelled"
    assert closed.wait(timeout=1.0)
