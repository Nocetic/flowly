"""fal's queue API — submit, poll, cancel.

Image generation can use the synchronous endpoint because it answers in
seconds. Video cannot: a clip takes minutes, and a request held open that long
dies to the first idle timeout between here and the provider. The queue API
exists for exactly this — submit returns immediately with a request id and the
URLs to poll and to cancel.

Polling follows the URLs the submit response hands back rather than rebuilding
them from the endpoint id. They are the provider's own answer to "where is this
job", and the endpoint id is not always enough to reconstruct them.

Cancellation matters as much as completion: a user who presses Stop should stop
paying for the clip, not just stop waiting for it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

_QUEUE_BASE = "https://queue.fal.run"
_UA = "flowly/media-fal-queue"

# Per-request timeout. The whole job is allowed to take much longer — that is
# what the polling loop is for — but no single HTTP call should hang.
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Poll cadence. Starts tight so a fast model returns promptly, then eases off so
# a three-minute render isn't a hundred requests.
_POLL_INITIAL = 2.0
_POLL_MAX = 10.0
_POLL_BACKOFF = 1.3

# Consecutive network failures tolerated before a job is called lost. Transient
# blips are normal on a long poll; an endless retry against a dead endpoint is
# not.
_MAX_POLL_ERRORS = 5


class FalQueueError(RuntimeError):
    """A queued job failed, was rejected, or could not be reached."""


class FalQueueCancelledError(FalQueueError):
    """The job was cancelled — by the user, or by the caller giving up."""


@dataclass(frozen=True, slots=True)
class QueuedJob:
    request_id: str
    status_url: str
    response_url: str
    cancel_url: str


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }


def _raise_for_status(response: httpx.Response, what: str) -> None:
    if response.status_code in (401, 403):
        raise FalQueueError("fal rejected the API key.")
    if response.status_code == 422:
        # The provider validated our payload and refused it. Its message is the
        # most useful thing we have, but it can echo the prompt — keep it short.
        raise FalQueueError(f"the model rejected the request ({response.text[:200]})")
    if response.status_code >= 400:
        raise FalQueueError(f"{what} failed with HTTP {response.status_code}")


async def submit(
    *,
    api_key: str,
    endpoint_id: str,
    payload: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> QueuedJob:
    """Enqueue a generation and return the handles needed to follow it."""

    async def _post(c: httpx.AsyncClient) -> QueuedJob:
        try:
            response = await c.post(
                f"{_QUEUE_BASE}/{endpoint_id}", headers=_headers(api_key), json=payload
            )
        except httpx.HTTPError as exc:
            raise FalQueueError(f"network error submitting the job: {exc}") from exc
        _raise_for_status(response, "submit")
        try:
            data = response.json()
        except ValueError as exc:
            raise FalQueueError(f"malformed submit response: {exc}") from exc

        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise FalQueueError("submit response carried no request id.")
        base = f"{_QUEUE_BASE}/{endpoint_id}/requests/{request_id}"
        return QueuedJob(
            request_id=request_id,
            status_url=str(data.get("status_url") or f"{base}/status"),
            response_url=str(data.get("response_url") or base),
            cancel_url=str(data.get("cancel_url") or f"{base}/cancel"),
        )

    if client is not None:
        return await _post(client)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as owned:
        return await _post(owned)


async def status(
    job: QueuedJob, *, api_key: str, client: httpx.AsyncClient
) -> dict[str, Any]:
    try:
        response = await client.get(job.status_url, headers=_headers(api_key))
    except httpx.HTTPError as exc:
        raise FalQueueError(f"network error polling the job: {exc}") from exc
    _raise_for_status(response, "status")
    try:
        data = response.json()
    except ValueError as exc:
        raise FalQueueError(f"malformed status response: {exc}") from exc
    return data if isinstance(data, dict) else {}


async def result(job: QueuedJob, *, api_key: str, client: httpx.AsyncClient) -> dict[str, Any]:
    try:
        response = await client.get(job.response_url, headers=_headers(api_key))
    except httpx.HTTPError as exc:
        raise FalQueueError(f"network error fetching the result: {exc}") from exc
    _raise_for_status(response, "result")
    try:
        data = response.json()
    except ValueError as exc:
        raise FalQueueError(f"malformed result response: {exc}") from exc
    return data if isinstance(data, dict) else {}


async def cancel(job: QueuedJob, *, api_key: str) -> bool:
    """Ask the provider to stop the job. Best effort — never raises.

    Called from cleanup paths where the caller is already giving up, so a
    failure here must not replace the real reason with a network error.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.put(job.cancel_url, headers=_headers(api_key))
        return response.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.debug("[media] cancel failed for {}: {}", job.request_id, exc)
        return False


async def wait_for_result(
    job: QueuedJob,
    *,
    api_key: str,
    timeout_seconds: float = 15 * 60,
    on_state: Any = None,
    should_cancel: Any = None,
) -> dict[str, Any]:
    """Poll until the job finishes, then return its result payload.

    ``on_state`` is called with each new provider state so a caller can surface
    progress. ``should_cancel`` is polled between requests; when it returns
    true the job is cancelled provider-side and :class:`FalQueueCancelledError` is
    raised — stopping the render, not merely stopping the wait.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    delay = _POLL_INITIAL
    errors = 0
    last_state = ""

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            if should_cancel is not None and should_cancel():
                await cancel(job, api_key=api_key)
                raise FalQueueCancelledError("generation was stopped.")
            if loop.time() > deadline:
                await cancel(job, api_key=api_key)
                raise FalQueueError(
                    f"the model did not finish within {int(timeout_seconds / 60)} minutes."
                )

            try:
                snapshot = await status(job, api_key=api_key, client=client)
                errors = 0
            except FalQueueCancelledError:
                raise
            except FalQueueError as exc:
                # A provider that answers 4xx has made a decision; retrying that
                # is pointless. Only transport blips are worth another attempt.
                if "network error" not in str(exc):
                    raise
                errors += 1
                if errors >= _MAX_POLL_ERRORS:
                    raise
                await asyncio.sleep(min(delay * errors, _POLL_MAX))
                continue

            state = str(snapshot.get("status") or "").upper()
            if state and state != last_state:
                last_state = state
                if on_state is not None:
                    try:
                        on_state(state)
                    except Exception:  # noqa: BLE001 - progress must not break the job
                        pass

            if state == "COMPLETED":
                return await result(job, api_key=api_key, client=client)
            if state in ("FAILED", "ERROR"):
                detail = snapshot.get("error") or snapshot.get("detail") or ""
                raise FalQueueError(f"the model failed to generate: {str(detail)[:200]}".strip())
            if state == "CANCELLED":
                raise FalQueueCancelledError("generation was cancelled.")

            await asyncio.sleep(delay)
            delay = min(delay * _POLL_BACKOFF, _POLL_MAX)
