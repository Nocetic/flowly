"""Bounded real-HTTP saturation and recovery; not a production soak benchmark."""

import asyncio
import hashlib
import os
import secrets
import statistics
import time
from contextlib import AsyncExitStack

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.mcp import test_external_mcp_http as support

peer = support.peer


async def session(stack, url, token):
    http = await stack.enter_async_context(httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}))
    read, write = await stack.enter_async_context(streamable_http_client(url, http_client=http))
    client = await stack.enter_async_context(ClientSession(read, write))
    await client.initialize()
    return client


async def test_saturated_key_is_bounded_other_key_survives_and_capacity_recovers(peer, record_property):
    service, token, row, url, calls, _, _ = peer
    other = "flm_" + secrets.token_hex(32)
    await service.owner("create", {
        "id": secrets.token_hex(16), "label": "Independent load peer", "sessionKey": "web:test", "tools": ["echo"],
        "tokenDigest": hashlib.sha256(other.encode()).hexdigest(), "ttlSeconds": 3600,
    })
    async with AsyncExitStack() as stack:
        blocked = await session(stack, url, token)
        healthy = await session(stack, url, other)
        waiting = [asyncio.create_task(blocked.call_tool("wait_forever", {})) for _ in range(4)]
        try:
            async with asyncio.timeout(5):
                while len(service._calls) != 4:
                    await asyncio.sleep(0.01)
            overflow = await blocked.call_tool("echo", {"value": "must-not-run"})
            assert overflow.is_error and "capacity" in str(overflow.content)
            assert "must-not-run" not in calls and len(service._calls) == 4
            result = await healthy.call_tool("echo", {"value": "other-key-still-works"})
            assert not result.is_error
            await service.owner("revoke", {"id": row["id"]})
            results = await asyncio.wait_for(asyncio.gather(*waiting), 5)
            assert all(result.is_error for result in results)
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)

        durations = []
        limit = asyncio.Semaphore(4)

        async def echo(index):
            async with limit:
                started = time.monotonic()
                result = await healthy.call_tool("echo", {"value": f"load-{index}"})
                durations.append(time.monotonic() - started)
                assert not result.is_error
                assert result.structured_content == {"echo": f"load-{index}"}

        started = time.monotonic()
        await asyncio.wait_for(asyncio.gather(*(echo(index) for index in range(400))), 45)
        elapsed = time.monotonic() - started
        assert len(durations) == 400
        assert len([value for value in calls if value.startswith("load-")]) == 400
        assert not service._calls
        # Emit only synthetic aggregate measurements, never response data/keys.
        record_property("successful_calls", 400)
        record_property("concurrency", 4)
        record_property("elapsed_seconds", round(elapsed, 3))
        record_property("p95_seconds", round(statistics.quantiles(durations, n=100)[94], 3))
        print({"successful_calls": 400, "concurrency": 4, "elapsed_seconds": round(elapsed, 3),
               "p95_seconds": round(statistics.quantiles(durations, n=100)[94], 3)})


async def test_bounded_transport_soak_leaves_no_inflight_calls_or_growing_tasks(peer, record_property):
    service, token, _, url, calls, _, _ = peer
    duration = float(os.environ.get("FLOWLY_MCP_SOAK_SECONDS", "2"))
    assert 1 <= duration <= 120
    latencies = []
    async with AsyncExitStack() as stack:
        client = await session(stack, url, token)
        # Establish the same four-connection pool used by the measured load.
        # Warming only one connection counts three legitimate server keepalive
        # tasks as leaks when concurrency later increases to four.
        warmed = await asyncio.gather(*(
            client.call_tool("echo", {"value": f"warmup-{index}"}) for index in range(4)
        ))
        assert all(not result.is_error for result in warmed)
        await asyncio.sleep(0.05)
        baseline_tasks = len(asyncio.all_tasks())

        async def invoke(index):
            began = time.monotonic()
            result = await client.call_tool("echo", {"value": f"soak-{index}"})
            latencies.append(time.monotonic() - began)
            assert not result.is_error
            assert result.structured_content == {"echo": f"soak-{index}"}

        started = time.monotonic()
        count = 0
        async with asyncio.timeout(duration + 10):
            while time.monotonic() - started < duration:
                await asyncio.gather(*(invoke(count + offset) for offset in range(4)))
                count += 4
                assert not service._calls
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
        elapsed = time.monotonic() - started
        assert len(calls) == count + 4 and len(latencies) == count
        assert len(asyncio.all_tasks()) <= baseline_tasks + 2
        assert not service._calls
        summary = {"successful_calls": count, "elapsed_seconds": round(elapsed, 3), "concurrency": 4,
                   "p95_seconds": round(statistics.quantiles(latencies, n=100)[94], 3),
                   "baseline_tasks": baseline_tasks, "final_tasks": len(asyncio.all_tasks())}
        for key, value in summary.items():
            record_property(key, value)
        print(summary)
