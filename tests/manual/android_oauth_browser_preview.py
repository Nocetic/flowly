"""Create only an unsigned OAuth preview on a running local gateway.

Never submits an OAuth callback or saves permissions. Cancels its own unique
draft on exit. The protected temporary snapshot is for Android browser UI tests,
not a substitute for testing the Android gateway/relay transport.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import uuid

from aiohttp import ClientSession


async def main():
    from flowly.config.loader import load_config
    from flowly.mcp.oauth_handoff import ANDROID_OAUTH_REDIRECT_URI

    token = load_config().gateway.token
    origin = "http://127.0.0.1:18790"
    async with ClientSession() as session:
        async with session.post(origin + "/api/auth/ws-ticket", headers={"X-Flowly-Token": token}) as response:
            if response.status == 404:
                ticket = None  # Local-only gateway exposes no ticket route.
            else:
                response.raise_for_status()
                ticket = (await response.json())["ticket"]
        query = {"clientId": str(uuid.uuid4())}
        if ticket:
            query["ticket"] = ticket
        async with session.ws_connect(origin + "/ws", params=query, headers={"X-Flowly-Token": token}) as ws:
            async def rpc(method, params=None):
                request_id = str(uuid.uuid4())
                await ws.send_json({"type": "rpc", "id": request_id, "method": method, "params": params or {}})
                async with asyncio.timeout(45):
                    async for event in ws:
                        data = json.loads(event.data)
                        if data.get("type") == "rpc" and data.get("id") == request_id:
                            if data.get("error"):
                                raise RuntimeError("Gateway rejected preview: " + data["error"].get("code", "UNKNOWN"))
                            return data["result"]
                raise RuntimeError("Gateway closed before reply")

            caps = await rpc("mcp.capabilities")
            assert caps["oauthRedirectUris"].get("android") == ANDROID_OAUTH_REDIRECT_URI
            print("Verified running gateway: Android OAuth supported", flush=True)
            name = "android_browser_preview_" + uuid.uuid4().hex[:12]
            operation_id = None
            with tempfile.TemporaryDirectory(prefix="flowly-android-oauth-preview-") as directory:
                try:
                    snapshot = await rpc("mcp.setup.begin", {
                        "name": name, "requestId": str(uuid.uuid4()),
                        "config": {"url": "https://mcp.linear.app/mcp", "auth": "oauth"},
                        "redirectUri": ANDROID_OAUTH_REDIRECT_URI,
                    })
                    operation_id = snapshot["id"]
                    async with asyncio.timeout(90):
                        while snapshot["phase"] != "awaiting_authorization":
                            if snapshot["phase"] in {"failed", "complete", "cancelled", "expired"}:
                                raise RuntimeError("Unexpected preview phase: " + snapshot["phase"])
                            await asyncio.sleep(.25)
                            snapshot = await rpc("mcp.setup.status", {"id": operation_id})
                    path = Path(directory) / "preview.json"
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w") as out:
                        json.dump(snapshot, out)
                    print(json.dumps({"snapshotPath": str(path), "phase": snapshot["phase"], "saved": snapshot["saved"]}), flush=True)
                    stop = asyncio.Event()
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
                    # Keep consuming replies instead of leaving the gateway's
                    # owner socket idle while the human examines the browser.
                    for _ in range(27):
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=20)
                            break
                        except TimeoutError:
                            await rpc("mcp.setup.status", {"id": operation_id})
                finally:
                    if operation_id:
                        result = await rpc("mcp.setup.cancel", {"id": operation_id})
                        rows = (await rpc("mcp.connections.list"))["servers"]
                        assert not any(row["name"] == name for row in rows)
                        print(json.dumps({"cleanupPhase": result["phase"], "saved": result["saved"], "configured": False}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
