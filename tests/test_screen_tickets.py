from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.tickets import StreamScope, StreamTickets

SCOPE = StreamScope("owner-a", "host-a", "default", "generation-a")


def _redeem(directory, ticket):
    try:
        return StreamTickets(directory).consume(ticket, SCOPE)
    except ScreenError as exc:
        return exc.code


def test_single_use_survives_registry_restart(tmp_path):
    ticket = StreamTickets(tmp_path).issue(SCOPE, "viewer-a")
    restarted = StreamTickets(tmp_path)
    assert restarted.consume(ticket, SCOPE) == "viewer-a"
    with pytest.raises(ScreenError, match="TICKET_EXPIRED"):
        restarted.consume(ticket, SCOPE)


@pytest.mark.parametrize("changes", [
    {"owner": "owner-b"}, {"host_id": "host-b"}, {"profile_id": "research"},
    {"generation": "generation-b"}, {"role": "agent"},
])
def test_wrong_owner_host_profile_generation_or_role_cannot_redeem(tmp_path, changes):
    registry = StreamTickets(tmp_path)
    ticket = registry.issue(SCOPE, "viewer-a")
    with pytest.raises(ScreenError, match="TICKET_EXPIRED"):
        registry.consume(ticket, replace(SCOPE, **changes))
    with pytest.raises(ScreenError, match="TICKET_EXPIRED"):
        registry.consume(ticket, SCOPE)


def test_expiry_and_capacity_are_bounded(tmp_path):
    now = [100.0]
    registry = StreamTickets(tmp_path, clock=lambda: now[0], capacity=1)
    ticket = registry.issue(SCOPE, "viewer-a")
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        registry.issue(SCOPE, "viewer-b")
    now[0] = 130
    replacement = registry.issue(SCOPE, "viewer-b")
    with pytest.raises(ScreenError, match="TICKET_EXPIRED"):
        registry.consume(ticket, SCOPE)
    assert registry.consume(replacement, SCOPE) == "viewer-b"


def test_generation_revocation_preserves_other_generations(tmp_path):
    registry = StreamTickets(tmp_path)
    old = registry.issue(SCOPE, "viewer-a")
    scope = replace(SCOPE, generation="generation-b")
    new = registry.issue(scope, "viewer-b")
    registry.revoke_generation(SCOPE.generation)
    with pytest.raises(ScreenError, match="TICKET_EXPIRED"):
        registry.consume(old, SCOPE)
    assert registry.consume(new, scope) == "viewer-b"


def test_only_one_process_can_spend_ticket(tmp_path):
    ticket = StreamTickets(tmp_path).issue(SCOPE, "viewer-a")
    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_redeem, [tmp_path] * 2, [ticket] * 2))
    assert sorted(outcomes) == ["TICKET_EXPIRED", "viewer-a"]


def test_raw_ticket_not_stored(tmp_path):
    ticket = StreamTickets(tmp_path).issue(SCOPE, "viewer-a")
    assert ticket.encode() not in (tmp_path / "tickets.sqlite3").read_bytes()


@pytest.mark.parametrize("ttl", [0, -1, 31, True, 1.5])
def test_invalid_ttl_is_rejected(tmp_path, ttl):
    with pytest.raises(ScreenError, match="INVALID_PARAMS"):
        StreamTickets(tmp_path).issue(SCOPE, "viewer-a", ttl=ttl)
