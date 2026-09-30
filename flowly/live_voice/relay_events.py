"""Original queued event authority and per-browser relay recipient leases."""
from __future__ import annotations

from dataclasses import dataclass, field

from flowly.live_voice.access import VoicePrincipal
from flowly.live_voice.events import EventAccess, EventRecipients
from flowly.live_voice.relay_transport import RelayPrincipal


@dataclass(eq=False)
class RelayRecipient:
    principal: RelayPrincipal


@dataclass
class RelayOutbound:
    payload: str
    access: EventAccess
    delivered: set[tuple[str, str]] = field(default_factory=set)


class RelayRecipients:
    def __init__(self):
        self.leases = EventRecipients()
        self.browsers: dict[str, RelayRecipient] = {}

    def synchronize(self, identities: dict[str, RelayPrincipal]) -> None:
        for key, recipient in list(self.browsers.items()):
            current = identities.get(key)
            old = recipient.principal
            if current is None or (current.uid, current.link_id) != (old.uid, old.link_id):
                self.leases.retire(recipient)
                self.browsers.pop(key)
            else:
                recipient.principal = current

    def observe(self, principal: RelayPrincipal) -> RelayRecipient:
        recipient = self.browsers.get(principal.session_id)
        if recipient is None:
            recipient = RelayRecipient(principal)
            self.browsers[principal.session_id] = recipient
        return recipient

    def begin(self, principal: RelayPrincipal) -> tuple[RelayRecipient, int]:
        recipient = self.observe(principal)
        return recipient, self.leases.begin(recipient)

    async def bind(self, binding: tuple[RelayRecipient, int], certificate: VoicePrincipal | None) -> bool:
        recipient, sequence = binding
        if certificate is not None and certificate.uid != recipient.principal.uid:
            return False
        return await self.leases.bind(recipient, sequence, certificate)

    def clear(self) -> None:
        self.synchronize({})
