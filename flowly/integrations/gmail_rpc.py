"""Small, secret-free Gmail management contract for every authenticated client."""

import asyncio

from flowly.integrations.gmail_connection import GmailConnection, GmailConnectionError

PARAMETERS = {
    "gmail.capabilities": set(),
    "gmail.status": set(),
    "gmail.setup.begin": {"locale", "label"},
    "gmail.setup.pending": set(),
    "gmail.setup.status": {"requestId"},
    "gmail.setup.cancel": {"requestId"},
    "gmail.disconnect": {"connectionId"},
}
METHODS = frozenset(PARAMETERS)


async def gmail_rpc(method: str, params: dict) -> dict:
    from flowly.channels.feature_rpc import FeatureRpcError
    if method not in METHODS or not isinstance(params, dict) or set(params) - PARAMETERS[method]:
        raise FeatureRpcError("INVALID_PARAMS", "Invalid Gmail request.")
    if any(not isinstance(value, str) or not value or len(value) > 100 for value in params.values()):
        raise FeatureRpcError("INVALID_PARAMS", "Invalid Gmail request.")
    if method == "gmail.capabilities":
        return {"version": 1, "methods": sorted(METHODS), "authorization": "browser", "scopes": ["gmail.readonly", "gmail.send"]}

    def run():
        service = GmailConnection()
        if method == "gmail.status":
            return service.status()
        if method == "gmail.setup.begin":
            return service.begin(locale=params.get("locale", "en"), label=params.get("label"))
        if method == "gmail.setup.pending":
            return {"setup": service.pending_setup()}
        if method == "gmail.setup.status" and params.get("requestId"):
            return service.setup_status(params["requestId"])
        if method == "gmail.setup.cancel" and params.get("requestId"):
            return service.cancel(params["requestId"])
        if method == "gmail.disconnect" and params.get("connectionId"):
            return service.disconnect(params["connectionId"])
        raise GmailConnectionError("INVALID_PARAMS")

    try:
        # HTTP and file locking must not block the gateway's chat/event loop.
        return await asyncio.to_thread(run)
    except GmailConnectionError as error:
        raise FeatureRpcError(error.code, "Gmail could not complete this request.") from None
    except Exception:
        raise FeatureRpcError("UNAVAILABLE", "Gmail is temporarily unavailable.") from None
