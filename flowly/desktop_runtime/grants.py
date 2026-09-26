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
