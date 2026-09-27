"""Closed wire shape for an explicit host browser grant, never an endpoint."""

from typing import Any

from flowly.desktop_runtime.authority import ScreenError


def validate_browser_grant(value: Any) -> dict[str, str]:
    fields = {"providerId", "hostId", "botId", "profileId", "screenId", "generation"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ScreenError("INVALID_PARAMS")
    for item in value.values():
        if (
            not isinstance(item, str) or not item or len(item) > 256
            or item != item.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
        ):
            raise ScreenError("INVALID_PARAMS")
    if value["providerId"] != "host-desktop-browser" or value["screenId"] != "browser":
        raise ScreenError("INVALID_PARAMS")
    return dict(value)


def resolve_host_browser_grant(gateway, raw_access):
    """Resolve an already authenticated caller's grant against this gateway only."""
    try:
        grant = validate_browser_grant(raw_access)
    except ScreenError as exc:
        raise ScreenError("INVALID_REQUEST") from exc
    try:
        identity, provider = gateway._get_host_screen_browser()
        identity = {**identity, **gateway._host_screen_checkpoint_identity()}
        state = provider.runtime.status()
    except (ScreenError, OSError) as exc:
        raise ScreenError("CONTROL_UNAVAILABLE") from exc
    if any(grant.get(key) != value for key, value in identity.items()):
        raise ScreenError("BROWSER_PROVIDER_NOT_OWNED")
    if not state or state["phase"] != "running":
        raise ScreenError("SCREEN_NOT_RUNNING")
    if grant["generation"] != state["generation"]:
        raise ScreenError("STALE_GENERATION")
    return grant, identity, provider
