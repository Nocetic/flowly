"""Invoked by the web checkout's test-gmail-runtime-contract.ts fixture only."""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from flowly.channels.gmail_auth import get_valid_access_token, load_credentials
from flowly.integrations.gmail_connection import BROKER_ORIGIN, GmailConnection


def main():
    origin = sys.argv[1]
    parsed = urlparse(origin)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
    root = Path(os.environ["FLOWLY_HOME"])
    home_a, home_b = root / "agent-a", root / "agent-b"
    home_a.mkdir()
    home_b.mkdir()
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as fixture:
        def request(req: httpx.Request):
            assert req.url.host in {"useflowlyapp.com", "gmail.googleapis.com"}
            # These fixture credentials can only reach the explicit localhost test server.
            return fixture.request(req.method, origin + req.url.raw_path.decode(), headers=dict(req.headers), content=req.content)

        with httpx.Client(transport=httpx.MockTransport(request)) as client:
            os.environ["FLOWLY_HOME"] = str(home_a)
            first = GmailConnection(client=client)
            setup = first.begin(locale="tr", label="First agent")
            assert setup["authorizationUrl"].startswith(BROKER_ORIGIN)
            assert "secret" not in setup and "accessToken" not in setup
            assert first.setup_status(setup["requestId"])["status"] == "pending"
            # Process/UI re-entry uses the same persisted request.
            first = GmailConnection(client=client)
            assert first.begin()["requestId"] == setup["requestId"]
            fixture.post(origin + "/fixture/approve", json=setup).raise_for_status()
            connected = first.setup_status(setup["requestId"])
            assert connected["connected"] and connected["email"] == "fixture@example.test"
            assert get_valid_access_token() == ("fixture-google-access", "fixture@example.test")
            credentials = load_credentials()
            assert credentials and "client_secret" not in credentials and "refresh_token" not in credentials
            assert "grant_secret" in credentials
            assert json.loads((home_a / "config.json").read_text())["channels"]["email"]["enabled"]
            assert first.setup_status(setup["requestId"])["connected"]

            os.environ["FLOWLY_HOME"] = str(home_b)
            second = GmailConnection(client=client)
            assert second.status()["status"] == "not_configured"
            other = second.begin(label="Second agent")
            fixture.post(origin + "/fixture/approve", json=other).raise_for_status()
            assert second.setup_status(other["requestId"])["connected"]
            os.environ["FLOWLY_HOME"] = str(home_a)
            assert first.disconnect(setup["requestId"])["status"] == "not_configured"
            assert get_valid_access_token() == (None, None)
            os.environ["FLOWLY_HOME"] = str(home_b)
            assert second.status()["connected"]
    print("PASS: create, resume, authorize, claim, Gmail validation, token use, disconnect and profile isolation")


if __name__ == "__main__":
    main()
