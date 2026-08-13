"""Byte-range media streaming on the direct gateway.

Video is why this endpoint exists. ``/api/media`` answers with a base64 data
URL, which is fine for an image and useless for a clip: a player seeks, and you
cannot seek inside a JSON string. So playback gets ``/api/media/stream``, which
serves real bytes with ``Range`` support.

The credential model is the interesting part. A ``<video>`` element and AVPlayer
fetch by URL and cannot set ``X-Flowly-Token``, so the stream route
authenticates with a ticket in the query string instead — minted by a
token-authenticated call, scoped to ONE media id, short-lived, and reusable
because one clip produces many Range requests.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from flowly.gateway.auth import TOKEN_HEADER, MediaTicketStore
from flowly.gateway.server import GatewayServer

TOKEN = "test-gateway-token-abcdefghijklmnop"


@pytest.fixture
def media_home(tmp_path, monkeypatch):
    """Point the gateway's media dir at a temp dir and return it."""
    home = tmp_path / "flowly-home"
    (home / "media").mkdir(parents=True)
    monkeypatch.setattr("flowly.gateway.server.get_flowly_home", lambda: home)
    return home / "media"


@pytest.fixture
async def aiohttp_client():
    """Minimal stand-in for pytest-aiohttp's fixture.

    Range/206/416 behaviour only means something over a real HTTP round-trip,
    but the plugin that normally provides this is not a Flowly dependency and
    a streaming test is a poor reason to add one. ``aiohttp.test_utils`` ships
    with aiohttp itself and does the same job.
    """
    clients: list[TestClient] = []

    async def _make(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        await client.close()


def _video_bytes(size: int = 4096) -> bytes:
    return bytes(range(256)) * (size // 256)


async def _client(aiohttp_client, *, host: str = "0.0.0.0", token: str = TOKEN):
    """A test client for a gateway configured like a remote/self-hosted one."""
    server = GatewayServer(host=host, port=0, auth_token=token)
    app = server._create_app()
    client = await aiohttp_client(app)
    return server, client


# ── ticket minting ───────────────────────────────────────────────────────────


async def test_ticket_requires_the_static_token(aiohttp_client, media_home):
    (media_home / "clip.mp4").write_bytes(_video_bytes())
    _server, client = await _client(aiohttp_client)

    resp = await client.post("/api/media/tickets", json={"id": "clip.mp4"})
    assert resp.status == 401


async def test_ticket_mint_returns_a_scoped_playback_url(aiohttp_client, media_home):
    (media_home / "clip.mp4").write_bytes(_video_bytes())
    _server, client = await _client(aiohttp_client)

    resp = await client.post(
        "/api/media/tickets", json={"id": "clip.mp4"}, headers={TOKEN_HEADER: TOKEN}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ticket"]
    assert body["mimeType"] == "video/mp4"
    assert body["ttlSeconds"] > 0
    assert body["url"].startswith("/api/media/stream?id=clip.mp4&ticket=")
    # The long-lived gateway token must never be what travels in the URL.
    assert TOKEN not in body["url"]


async def test_ticket_for_missing_media_fails_at_mint_time(aiohttp_client, media_home):
    _server, client = await _client(aiohttp_client)
    resp = await client.post(
        "/api/media/tickets", json={"id": "nope.mp4"}, headers={TOKEN_HEADER: TOKEN}
    )
    assert resp.status == 404


async def test_ticket_rejects_traversal_ids(aiohttp_client, media_home):
    _server, client = await _client(aiohttp_client)
    for bad in ("../secret.mp4", "a/b.mp4", ".hidden.mp4", ""):
        resp = await client.post(
            "/api/media/tickets", json={"id": bad}, headers={TOKEN_HEADER: TOKEN}
        )
        assert resp.status in (400, 404), bad


async def test_ticket_refuses_non_media_files(aiohttp_client, media_home):
    (media_home / "notes.txt").write_text("hello")
    _server, client = await _client(aiohttp_client)
    resp = await client.post(
        "/api/media/tickets", json={"id": "notes.txt"}, headers={TOKEN_HEADER: TOKEN}
    )
    assert resp.status == 415


# ── streaming ────────────────────────────────────────────────────────────────


async def _mint(client, media_id: str) -> str:
    resp = await client.post(
        "/api/media/tickets", json={"id": media_id}, headers={TOKEN_HEADER: TOKEN}
    )
    assert resp.status == 200
    return (await resp.json())["ticket"]


async def test_stream_serves_the_whole_file(aiohttp_client, media_home):
    data = _video_bytes()
    (media_home / "clip.mp4").write_bytes(data)
    _server, client = await _client(aiohttp_client)
    ticket = await _mint(client, "clip.mp4")

    resp = await client.get(f"/api/media/stream?id=clip.mp4&ticket={ticket}")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "video/mp4"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert int(resp.headers["Content-Length"]) == len(data)
    assert await resp.read() == data


async def test_stream_honours_a_range_request(aiohttp_client, media_home):
    data = _video_bytes()
    (media_home / "clip.mp4").write_bytes(data)
    _server, client = await _client(aiohttp_client)
    ticket = await _mint(client, "clip.mp4")

    resp = await client.get(
        f"/api/media/stream?id=clip.mp4&ticket={ticket}", headers={"Range": "bytes=100-199"}
    )
    assert resp.status == 206
    assert resp.headers["Content-Range"] == f"bytes 100-199/{len(data)}"
    assert int(resp.headers["Content-Length"]) == 100
    assert await resp.read() == data[100:200]


async def test_stream_rejects_an_unsatisfiable_range(aiohttp_client, media_home):
    data = _video_bytes(1024)
    (media_home / "clip.mp4").write_bytes(data)
    _server, client = await _client(aiohttp_client)
    ticket = await _mint(client, "clip.mp4")

    resp = await client.get(
        f"/api/media/stream?id=clip.mp4&ticket={ticket}",
        headers={"Range": f"bytes={len(data) + 500}-"},
    )
    assert resp.status == 416


async def test_stream_supports_head_for_metadata_probes(aiohttp_client, media_home):
    data = _video_bytes()
    (media_home / "clip.mp4").write_bytes(data)
    _server, client = await _client(aiohttp_client)
    ticket = await _mint(client, "clip.mp4")

    resp = await client.head(f"/api/media/stream?id=clip.mp4&ticket={ticket}")
    assert resp.status == 200
    assert int(resp.headers["Content-Length"]) == len(data)
    assert await resp.read() == b""


async def test_stream_without_a_ticket_is_unauthorized(aiohttp_client, media_home):
    (media_home / "clip.mp4").write_bytes(_video_bytes())
    _server, client = await _client(aiohttp_client)

    resp = await client.get("/api/media/stream?id=clip.mp4")
    assert resp.status == 401


async def test_stream_rejects_the_raw_gateway_token_in_the_url(aiohttp_client, media_home):
    """The whole point of tickets: the long-lived token is not a URL credential."""
    (media_home / "clip.mp4").write_bytes(_video_bytes())
    _server, client = await _client(aiohttp_client)

    resp = await client.get(f"/api/media/stream?id=clip.mp4&ticket={TOKEN}")
    assert resp.status == 401


async def test_ticket_is_scoped_to_one_media_id(aiohttp_client, media_home):
    (media_home / "clip.mp4").write_bytes(_video_bytes())
    (media_home / "other.mp4").write_bytes(_video_bytes())
    _server, client = await _client(aiohttp_client)
    ticket = await _mint(client, "clip.mp4")

    resp = await client.get(f"/api/media/stream?id=other.mp4&ticket={ticket}")
    assert resp.status == 401


async def test_ticket_survives_repeated_range_requests(aiohttp_client, media_home):
    """Single-use (like the WS ticket) would break playback on the first seek."""
    data = _video_bytes()
    (media_home / "clip.mp4").write_bytes(data)
    _server, client = await _client(aiohttp_client)
    ticket = await _mint(client, "clip.mp4")

    for start in (0, 100, 300):
        resp = await client.get(
            f"/api/media/stream?id=clip.mp4&ticket={ticket}",
            headers={"Range": f"bytes={start}-{start + 49}"},
        )
        assert resp.status == 206
        assert await resp.read() == data[start:start + 50]


async def test_stream_rejects_traversal_even_with_a_valid_ticket(aiohttp_client, media_home, tmp_path):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"top secret")
    _server, client = await _client(aiohttp_client)

    server = _server
    forged = server._media_ticket_store.mint("../secret.mp4")
    resp = await client.get(f"/api/media/stream?id=../secret.mp4&ticket={forged}")
    assert resp.status == 400


async def test_stream_refuses_a_symlink_escaping_the_media_dir(aiohttp_client, media_home, tmp_path):
    secret = tmp_path / "outside.mp4"
    secret.write_bytes(b"top secret")
    (media_home / "link.mp4").symlink_to(secret)
    _server, client = await _client(aiohttp_client)

    server = _server
    ticket = server._media_ticket_store.mint("link.mp4")
    resp = await client.get(f"/api/media/stream?id=link.mp4&ticket={ticket}")
    assert resp.status == 403


async def test_loopback_gateway_streams_without_a_ticket(aiohttp_client, media_home):
    """No token configured means no credential boundary — same as every route."""
    data = _video_bytes()
    (media_home / "clip.mp4").write_bytes(data)
    _server, client = await _client(aiohttp_client, host="127.0.0.1", token="")

    resp = await client.get("/api/media/stream?id=clip.mp4")
    assert resp.status == 200
    assert await resp.read() == data


async def test_legacy_image_endpoint_still_returns_a_data_url(aiohttp_client, media_home):
    """Regression guard: the image path desktop/iOS already use must not move."""
    from PIL import Image

    Image.new("RGB", (8, 8), (1, 2, 3)).save(media_home / "img.png")
    _server, client = await _client(aiohttp_client)

    resp = await client.get("/api/media?id=img.png", headers={TOKEN_HEADER: TOKEN})
    assert resp.status == 200
    body = await resp.json()
    assert body["dataUrl"].startswith("data:image/png;base64,")
    assert body["fileName"] == "img.png"


async def test_legacy_image_endpoint_still_refuses_video(aiohttp_client, media_home):
    """Video must go through the streaming route, not get base64'd into JSON."""
    (media_home / "clip.mp4").write_bytes(_video_bytes())
    _server, client = await _client(aiohttp_client)

    resp = await client.get("/api/media?id=clip.mp4", headers={TOKEN_HEADER: TOKEN})
    assert resp.status == 415


# ── ticket store unit behaviour ──────────────────────────────────────────────


def test_ticket_store_expires_tickets():
    store = MediaTicketStore(ttl_seconds=0)
    ticket = store.mint("clip.mp4")
    assert store.allows(ticket, "clip.mp4") is False


def test_ticket_store_scopes_and_revokes():
    store = MediaTicketStore()
    ticket = store.mint("clip.mp4")
    assert store.resolve(ticket) == "clip.mp4"
    assert store.allows(ticket, "clip.mp4") is True
    assert store.allows(ticket, "other.mp4") is False
    assert store.allows(None, "clip.mp4") is False
    assert store.allows("nonsense", "clip.mp4") is False
    store.revoke(ticket)
    assert store.resolve(ticket) is None


def test_ticket_store_is_bounded():
    store = MediaTicketStore(max_entries=4)
    tickets = [store.mint(f"clip{i}.mp4") for i in range(10)]
    live = [t for t in tickets if store.resolve(t) is not None]
    assert len(live) <= 5  # bounded; the newest survive
    assert store.resolve(tickets[-1]) == "clip9.mp4"


def test_ticket_store_requires_a_media_id():
    with pytest.raises(ValueError):
        MediaTicketStore().mint("")
