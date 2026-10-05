#!/usr/bin/env python3
"""Ask the running local gateway for every agent's call memory, exactly as
the clients do, and print what each request got (shape only).

    .venv/bin/python scripts/voice_memory_probe.py [--port 18790] [--all]

The requests are the contract envelopes (tests/fixtures/
live_voice_client_requests.json): Desktop's and iOS's own shapes, for the
default agent and every running named agent (--all also tries stopped ones,
which may start them). iOS reaches the gateway through Relay, but inside the
gateway its request takes the same ProfileHost -> runtime path tested here.

Read-only: the snapshot and recall never write. The gateway token is read
from the local config and only sent to 127.0.0.1; nothing secret and no
memory content is printed. Exit status 1 when any request fails.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
from pathlib import Path
from urllib.parse import quote

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "tests" / "fixtures" / "live_voice_client_requests.json").read_text())


def gateway_settings(port: int | None) -> tuple[int, str]:
    sys.path.insert(0, str(ROOT))
    from flowly.profile import get_flowly_home

    config = json.loads((get_flowly_home() / "config.json").read_text())
    gateway = config.get("gateway") or {}
    return port or int(gateway.get("port") or 18790), str(gateway.get("token") or "")


class Gateway:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self.ws = ws
        self.ids = itertools.count(1)

    async def rpc(self, method: str, params: dict) -> dict:
        rpc_id = f"probe-{next(self.ids)}"
        await self.ws.send_json({"type": "rpc", "id": rpc_id, "method": method, "params": params})
        while True:
            message = await asyncio.wait_for(self.ws.receive_json(), timeout=30)
            if message.get("type") == "rpc" and message.get("id") == rpc_id:
                if "error" in message:
                    error = message["error"]
                    raise RuntimeError(error.get("code", "ERROR") if isinstance(error, dict) else str(error))
                return message.get("result") or {}


def shape(result: dict) -> str:
    if "sections" in result:
        kinds = ",".join(section.get("kind", "?") for section in result["sections"])
        size = sum(len(str(section.get("text", "")).encode()) for section in result["sections"])
        return f"snapshot sections={kinds or '-'} bytes={size} profileBytes={len(str(result.get('profile', '')).encode())}"
    if "facts" in result:
        return f"recall facts={len(result['facts'])}"
    return "empty result"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--all", action="store_true", help="also probe stopped agents (may start them)")
    args = parser.parse_args()
    port, token = gateway_settings(args.port)
    url = f"ws://127.0.0.1:{port}/ws" + (f"?token={quote(token)}" if token else "")
    failures = 0
    async with aiohttp.ClientSession() as session, session.ws_connect(url, max_msg_size=40 * 1024 * 1024) as ws:
        gateway = Gateway(ws)
        capabilities = await gateway.rpc("profiles.capabilities", {})
        host_id = capabilities["hostId"]
        advertised = "voice.memory.snapshot" in (capabilities.get("profileRpcMethods") or [])
        print(f"gateway 127.0.0.1:{port} host={host_id[:8]}… snapshot advertised={advertised}")
        statuses = {row["profile"]: row for row in (await gateway.rpc("profiles.statuses", {}))["statuses"]}
        for name, status in statuses.items():
            running = name == "default" or status.get("state") in {"connected", "running", "ready", "external"}
            if not running and not args.all:
                print(f"- {name}: skipped ({status.get('state')}; --all to include)")
                continue
            template = "default" if name == "default" else "named"
            for entry in CONTRACT["requests"]:
                if entry["id"].split(".")[-1] != template:
                    continue
                text = json.dumps(entry["envelope"]).replace('"writer"', json.dumps(name))
                text = text.replace("$hostId", host_id).replace("$botId", status["botId"])
                request = json.loads(text)
                try:
                    result = await gateway.rpc(request["method"], request["params"])
                    print(f"- {name} {entry['client']:<7} {request['params'].get('method', request['method']):<22} ok   {shape(result)}")
                except Exception as error:  # noqa: BLE001 - report every failure, keep probing
                    failures += 1
                    print(f"- {name} {entry['client']:<7} {request['params'].get('method', request['method']):<22} FAIL {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
