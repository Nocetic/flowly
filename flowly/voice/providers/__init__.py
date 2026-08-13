"""Voice generation providers.

Each module here speaks to one service DIRECTLY, with the key the user typed on
that service's own connection. They are siblings, not entries in a catalog:
adding a provider means adding a module and a card, and nothing else in the
system has to learn its name.

That is the whole point of the arrangement. A shared catalog can only offer
what it has indexed, and it indexes endpoints — not the voice you cloned this
morning, not the models your plan can actually run. Talking to the provider
means the picker shows YOUR library, because it is reading your account.

Distinct from :mod:`flowly.voice.tts`, which synthesises speech for a live
phone call: that path streams PCM into a call leg and is judged on latency.
This one produces a file to keep, and is judged on how it sounds.
"""

from __future__ import annotations
