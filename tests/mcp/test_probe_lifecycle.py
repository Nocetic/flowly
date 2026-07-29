"""Regression tests for MCP connect/probe lifecycle and error reporting."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

import pytest

from flowly.mcp import client, probe, stderr_log
from flowly.mcp.stderr_log import summarize_stderr_excerpt


@pytest.mark.asyncio
async def test_fast_transport_failure_is_not_reported_as_timeout(monkeypatch):
    task = client.MCPServerTask("broken")

    async def fail_stdio() -> None:
        raise RuntimeError("manifest is not an MCP JSON-RPC endpoint")

    monkeypatch.setattr(task, "_run_stdio", fail_stdio)

    with pytest.raises(RuntimeError, match="manifest is not an MCP"):
        await task.start({"command": "ignored", "connect_timeout": 300})

    assert isinstance(task.error, RuntimeError)


@pytest.mark.asyncio
async def test_clean_exit_before_ready_is_a_lifecycle_error(monkeypatch):
    task = client.MCPServerTask("empty")

    async def exit_stdio() -> None:
        return None

    monkeypatch.setattr(task, "_run_stdio", exit_stdio)

    with pytest.raises(RuntimeError, match="exited before initialization"):
        await task.start({"command": "ignored", "connect_timeout": 300})


@pytest.mark.asyncio
async def test_non_finite_deadlines_are_rejected_before_transport_start(monkeypatch):
    task = client.MCPServerTask("invalid")
    started = False

    async def start_stdio() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(task, "_run_stdio", start_stdio)

    with pytest.raises(ValueError, match="connect_timeout must be"):
        await task.start({"command": "ignored", "connect_timeout": float("nan")})
    assert started is False


@pytest.mark.asyncio
async def test_real_connect_timeout_cancels_and_awaits_transport(monkeypatch):
    task = client.MCPServerTask("slow")
    cancelled = asyncio.Event()

    async def hang_stdio() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(task, "_run_stdio", hang_stdio)

    with pytest.raises(asyncio.TimeoutError, match="timed out after 0s"):
        await task.start({"command": "ignored", "connect_timeout": 0.01})

    assert cancelled.is_set()
    assert task._task is not None
    assert task._task.done()


@pytest.mark.asyncio
async def test_async_probe_does_not_block_callers_event_loop(monkeypatch):
    submitted: Future[list[str]] = Future()

    monkeypatch.setattr(
        probe,
        "_submit_probe",
        lambda *args, **kwargs: (submitted, 1.0, None),
    )

    probe_task = asyncio.create_task(
        probe.probe_tool_names_async("slow", {"command": "ignored"}),
    )
    await asyncio.sleep(0)

    # If probe_tool_names_async blocked like future.result(), this line would
    # never run until the probe completed.
    assert not probe_task.done()
    submitted.set_result(["search"])

    assert await probe_task == (True, ["search"], "")


@pytest.mark.asyncio
async def test_async_probe_cancellation_reaches_mcp_future(monkeypatch):
    submitted: Future[list[str]] = Future()

    monkeypatch.setattr(
        probe,
        "_submit_probe",
        lambda *args, **kwargs: (submitted, 30.0, None),
    )

    probe_task = asyncio.create_task(
        probe.probe_tool_names_async("slow", {"command": "ignored"}),
    )
    await asyncio.sleep(0)
    probe_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await probe_task
    assert submitted.cancelled()


def test_exception_groups_surface_leaf_errors():
    exc = ExceptionGroup(
        "transport failed",
        [RuntimeError("invalid JSON-RPC response"), OSError("connection reset")],
    )

    assert probe._exception_detail(exc) == ("invalid JSON-RPC response; connection reset")


def test_manifest_stderr_gets_actionable_endpoint_error(tmp_path, monkeypatch):
    log_path = tmp_path / "mcp-stderr.log"
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    monkeypatch.setattr(stderr_log, "_log_fh", log_handle)
    offset = stderr_log.write_stderr_log_header("test")

    stderr = """
    Connection error: ZodError: [
      {"message":"Invalid input","keys":["$schema","remotes","authentication"]}
    ]
    """
    log_handle.write(stderr)
    log_handle.flush()

    excerpt = stderr_log.read_stderr_excerpt(offset)
    assert summarize_stderr_excerpt(excerpt) == (
        "the URL returned an MCP manifest instead of JSON-RPC; use the endpoint in remotes[].url"
    )
    log_handle.close()
