"""Process-local, single-use admission carried from relay ingress to the agent bus.

A JSON metadata object is never a capability. Admission captures the owning
WebChannel/gateway and is revalidated when the queued agent turn actually starts.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.context import SCREEN_RUN_BINDING, ScreenRunBinding
from flowly.desktop_runtime.grants import resolve_host_browser_grant
from flowly.desktop_runtime.run_checkpoint import ScreenRunCheckpoint

METADATA_KEY = "_relay_screen_run"


@dataclass
class RelayScreenRun:
    gateway: object
    grant: dict
    session_key: str
    run_id: str
    current: Callable[[], bool]
    consumed: bool = False
    succeeded: bool = False


def admit_relay_screen_run(gateway, raw_access, session_key, run_id, current):
    if gateway is None or not current():
        raise ScreenError("BROWSER_ACCESS_NOT_GRANTED")
    if not all(isinstance(value, str) and 0 < len(value) <= 256
               and not any(ord(c) < 32 or ord(c) == 127 for c in value) for value in (session_key, run_id)):
        raise ScreenError("INVALID_REQUEST")
    grant, _, _ = resolve_host_browser_grant(gateway, raw_access)
    return RelayScreenRun(gateway, grant, session_key, run_id, current)


@asynccontextmanager
async def relay_screen_scope(admission, message):
    from flowly.gateway.server import _BROWSER_RUN_BINDING, _BrowserRunBinding

    if (type(admission) is not RelayScreenRun or admission.consumed or message.channel != "web"
            or message.session_key != admission.session_key or message.metadata.get("run_id") != admission.run_id
            or not admission.current()):
        raise ScreenError("BROWSER_ACCESS_NOT_GRANTED")
    admission.consumed = True
    grant, identity, provider = resolve_host_browser_grant(admission.gateway, admission.grant)
    checkpoint = None
    browser_token = _BROWSER_RUN_BINDING.set(_BrowserRunBinding(
        client_id="relay:" + admission.run_id, provider_id="host-desktop-browser",
        screen_generation=grant["generation"], screen_bot_id=identity["botId"]))
    screen_token = SCREEN_RUN_BINDING.set(None)
    try:
        checkpoint_identity = {key: identity[key] for key in ("hostId", "botId", "profileId")}
        checkpoint = await ScreenRunCheckpoint.start(provider, checkpoint_identity, admission.session_key,
                                                      admission.run_id, grant["generation"])
        SCREEN_RUN_BINDING.set(ScreenRunBinding(provider, grant["generation"], admission.run_id, checkpoint))
        yield admission
    finally:
        try:
            if checkpoint is not None:
                await checkpoint.finish(cancelled=not admission.succeeded)
        finally:
            SCREEN_RUN_BINDING.reset(screen_token)
            _BROWSER_RUN_BINDING.reset(browser_token)
