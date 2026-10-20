from dataclasses import replace

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.tickets import StreamScope, StreamTickets
from flowly.desktop_runtime.viewers import ViewerCredentials

SCOPE = StreamScope("owner", "host", "default", "generation")


def test_resume_survives_worker_restart_and_id_is_not_a_capability(tmp_path):
    created = ViewerCredentials(StreamTickets(tmp_path)).create(SCOPE, "bot")
    registry = ViewerCredentials(StreamTickets(tmp_path))
    assert registry.authenticate(created["viewerToken"], SCOPE, "bot") == created["viewerId"]
    with pytest.raises(ScreenError, match="VIEWER_EXPIRED"):
        registry.authenticate(created["viewerId"], SCOPE, "bot")
    assert created["viewerToken"].encode() not in (tmp_path / "tickets.sqlite3").read_bytes()
    assert registry.active(created["viewerId"], SCOPE, "bot")


@pytest.mark.parametrize("change", [
    {"owner": "other"}, {"host_id": "other"}, {"profile_id": "writer"},
    {"generation": "other"}, {"role": "agent"},
])
def test_capability_is_bound_to_authenticated_scope(tmp_path, change):
    registry = ViewerCredentials(StreamTickets(tmp_path))
    created = registry.create(SCOPE, "bot")
    with pytest.raises(ScreenError, match="VIEWER_EXPIRED"):
        registry.authenticate(created["viewerToken"], replace(SCOPE, **change), "bot")
    assert not registry.active(created["viewerId"], replace(SCOPE, **change), "bot")
    assert registry.authenticate(created["viewerToken"], SCOPE, "bot") == created["viewerId"]


def test_recreated_bot_cannot_adopt_previous_viewer(tmp_path):
    registry = ViewerCredentials(StreamTickets(tmp_path))
    created = registry.create(SCOPE, "bot")
    with pytest.raises(ScreenError, match="VIEWER_EXPIRED"):
        registry.authenticate(created["viewerToken"], SCOPE, "replacement")


def test_expiry_capacity_and_explicit_close_revoke_unspent_tickets(tmp_path):
    now = [100]
    tickets = StreamTickets(tmp_path, clock=lambda: now[0], capacity=1)
    registry = ViewerCredentials(tickets)
    created = registry.create(SCOPE, "bot")
    ticket = tickets.issue(SCOPE, created["viewerId"])
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        registry.create(SCOPE, "bot")
    registry.revoke(created["viewerId"])
    assert not registry.active(created["viewerId"], SCOPE, "bot")
    with pytest.raises(ScreenError, match="TICKET_EXPIRED"):
        tickets.consume(ticket, SCOPE)
    second = registry.create(SCOPE, "bot")
    now[0] += registry.TTL
    with pytest.raises(ScreenError, match="VIEWER_EXPIRED"):
        registry.authenticate(second["viewerToken"], SCOPE, "bot")
    assert not registry.active(second["viewerId"], SCOPE, "bot")
    registry.create(SCOPE, "bot")


def test_close_intent_revokes_capability_but_same_close_can_resume_after_restart(tmp_path):
    tickets = StreamTickets(tmp_path)
    registry = ViewerCredentials(tickets)
    created = registry.create(SCOPE, "bot")
    viewer, token = created["viewerId"], created["viewerToken"]
    ticket = tickets.issue(SCOPE, viewer)
    registry.begin_close(viewer, "close-op", 7)
    registry = ViewerCredentials(StreamTickets(tmp_path))
    assert not registry.available(viewer, SCOPE.generation)
    assert not registry.active(viewer, SCOPE, "bot")
    with pytest.raises(ScreenError, match="VIEWER_REVOKED"):
        registry.authenticate(token, SCOPE, "bot")
    with pytest.raises(ScreenError, match="OPERATION_CONFLICT"):
        registry.authenticate(token, SCOPE, "bot", close=("other", 7))
    with pytest.raises(ScreenError, match="VIEWER_EXPIRED"):
        registry.authenticate(token, SCOPE, "other", close=("close-op", 7))
    assert registry.authenticate(token, SCOPE, "bot", close=("close-op", 7)) == viewer
    assert registry.close_record(viewer, SCOPE.generation, "close-op", 7) == {"result": None}
    with pytest.raises(ScreenError, match="TICKET_EXPIRED"):
        tickets.consume(ticket, SCOPE)
    registry.complete_close(viewer, "close-op", 7, {"epoch": 8, "control": "agent"})
    assert ViewerCredentials(StreamTickets(tmp_path)).close_record(viewer, SCOPE.generation, "close-op", 7) == {
        "result": {"epoch": 8, "control": "agent"},
    }


def test_closed_receipts_are_bounded_and_do_not_consume_active_viewer_capacity(tmp_path):
    tickets = StreamTickets(tmp_path, capacity=1)
    registry = ViewerCredentials(tickets)
    created = []
    for index in range(8):
        item = registry.create(SCOPE, "bot")
        created.append(item)
        registry.begin_close(item["viewerId"], f"close-{index}", index)
        registry.complete_close(item["viewerId"], f"close-{index}", index, {"epoch": index + 1})
    with tickets._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM viewers").fetchone()[0] == 4
    with pytest.raises(ScreenError, match="VIEWER_EXPIRED"):
        registry.authenticate(created[0]["viewerToken"], SCOPE, "bot", close=("close-0", 0))
    assert registry.authenticate(created[-1]["viewerToken"], SCOPE, "bot", close=("close-7", 7))
    registry.create(SCOPE, "bot")


def test_legacy_viewer_database_migration_preserves_existing_capability(tmp_path):
    import hashlib

    tickets = StreamTickets(tmp_path)
    with tickets._connection() as connection:
        connection.execute("""CREATE TABLE viewers (
            digest TEXT PRIMARY KEY, owner TEXT NOT NULL, host TEXT NOT NULL,
            profile TEXT NOT NULL, generation TEXT NOT NULL, role TEXT NOT NULL,
            bot TEXT NOT NULL, viewer TEXT UNIQUE NOT NULL, expires REAL NOT NULL
        )""")
        connection.execute("INSERT INTO viewers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (hashlib.sha256(b"legacy-token").hexdigest(), *tickets._validate(SCOPE),
                            "bot", "legacy-viewer", tickets._clock() + 3600))
    registry = ViewerCredentials(tickets)
    assert registry.authenticate("legacy-token", SCOPE, "bot") == "legacy-viewer"
    registry.begin_close("legacy-viewer", "close", 1)
    assert registry.close_record("legacy-viewer", SCOPE.generation, "close", 1) == {"result": None}
