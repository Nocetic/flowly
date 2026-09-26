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
